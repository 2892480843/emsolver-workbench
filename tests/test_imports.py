import copy
from pathlib import Path
import shutil
import threading
import time

import numpy as np
import pytest
from scipy import sparse

from emsolver.access import Identity, identity_context, require_project
from emsolver.errors import ServiceError
from emsolver.imports import ImportManager
from emsolver.integrity import file_hash
from emsolver.store import Store


class Gateway:
    def __init__(self, root):
        self.root = root
        root.mkdir(mode=0o700)
        (root / 'inbox').mkdir(mode=0o700)
        (root / 'matrices').mkdir(mode=0o700)
        self.store = Store(root / 'meta.sqlite3')
        self._mutex = threading.RLock()
        for project_id in ('p', 'other'):
            self.store.put('projects', project_id, {'id': project_id, 'members': [
                {'subject': 'analyst', 'role': 'analyst'}, {'subject': 'viewer', 'role': 'viewer'}] if project_id == 'p' else []})
        self.registrations = []

    def _project(self, project_id, writable=False):
        project = self.store.get('projects', project_id)
        if project is None: raise ServiceError('NOT_FOUND', status=404)
        require_project(project, 'write' if writable else 'read')
        return project

    def _get(self, kind, item_id):
        item = self.store.get(kind, item_id)
        if item is None: raise ServiceError('NOT_FOUND', status=404)
        self._project(item['project_id'])
        return item

    @staticmethod
    def _public(item): return {key: value for key, value in item.items() if not key.startswith('_')}

    @staticmethod
    def _validated_metadata(metadata):
        if not isinstance(metadata, dict): raise ServiceError('INVALID_METADATA')
        return copy.deepcopy(metadata)

    @staticmethod
    def _kill_tree(process):
        import psutil
        try:
            parent = psutil.Process(process.pid)
            for child in parent.children(recursive=True):
                try: child.kill()
                except psutil.Error: pass
            parent.kill()
        except psutil.Error: pass

    def _register_import_result(self, project_id, path, name, storage, metadata, diagnostic):
        from emsolver.access import current_identity
        self._project(project_id, True)
        self.registrations.append(current_identity().subject)
        digest = file_hash(path)
        target = self.root / 'matrices' / (digest + path.suffix)
        shutil.copyfile(path, target)
        target.chmod(0o400)
        item = {'id': 'm_' + str(len(self.registrations)), 'project_id': project_id, 'name': name,
                'hash': digest, 'storage': storage, 'metadata': metadata, 'diagnostics': diagnostic,
                '_path': str(target)}
        self.store.put('matrices', item['id'], item)
        return self._public(item)


@pytest.fixture
def gateway(tmp_path):
    item = Gateway(tmp_path / 'data')
    yield item
    item.store.close()


def staged(gateway, name='upload.npz', invalid=False):
    path = gateway.root / 'inbox' / name
    if invalid: path.write_bytes(b'not a numeric archive')
    else: sparse.save_npz(path, sparse.diags([-2., 0., 3., 4.], format='csr'))
    path.chmod(0o600)
    return path


def test_real_subprocess_validates_before_registration_and_keeps_owner_identity(gateway):
    manager = ImportManager(gateway)
    with identity_context(Identity('analyst')):
        item = manager.submit('p', staged(gateway), 'matrix.npz', metadata={'description': 'source'})
    assert item['status'] == 'QUEUED' and item['bytes_received'] == item['total_bytes'] > 0
    assert not any(key.startswith('_') for key in item)
    assert manager.run_once() is True
    result = manager.get(item['id'])
    assert result['status'] == 'SUCCEEDED', result
    assert result['matrix_id'] and gateway.registrations == ['analyst']
    assert result['worker_pid'] != __import__('os').getpid()
    assert list((gateway.root / 'inbox').iterdir()) == []
    assert manager.run_once() is False
    manager.close()


def test_malformed_input_fails_without_matrix_or_private_error_and_cleans_staging(gateway):
    manager = ImportManager(gateway)
    item = manager.submit('p', staged(gateway, invalid=True), 'matrix.npz')
    manager.run_once()
    result = manager.get(item['id'])
    assert result['status'] == 'FAILED' and result['error']['code']
    assert str(gateway.root) not in str(result)
    assert gateway.store.list('matrices') == []
    assert list((gateway.root / 'inbox').iterdir()) == []
    manager.close()


def test_queued_cancel_is_terminal_and_queue_is_fifo(gateway):
    manager = ImportManager(gateway)
    first = manager.submit('p', staged(gateway, 'one.npz'), 'one.npz')
    second = manager.submit('p', staged(gateway, 'two.npz'), 'two.npz')
    assert manager.cancel(first['id'])['status'] == 'CANCELLED'
    assert manager.cancel(first['id'])['status'] == 'CANCELLED'
    assert manager.run_once()
    assert manager.get(second['id'])['status'] == 'SUCCEEDED'
    assert manager.get(first['id'])['matrix_id'] is None
    assert len(gateway.registrations) == 1
    assert list((gateway.root / 'inbox').iterdir()) == []
    manager.close()


def test_running_cancel_kills_child_and_never_registers(gateway):
    manager = ImportManager(gateway)
    item = manager.submit('p', staged(gateway), 'matrix.npz')
    runner = threading.Thread(target=manager.run_once)
    runner.start()
    deadline = time.monotonic() + 5
    while manager._process is None and time.monotonic() < deadline: time.sleep(.005)
    assert manager._process is not None
    assert manager.cancel(item['id'])['status'] in {'CANCELLING', 'CANCELLED'}
    runner.join(timeout=5)
    assert not runner.is_alive()
    assert manager.get(item['id'])['status'] == 'CANCELLED'
    assert gateway.registrations == []
    assert list((gateway.root / 'inbox').iterdir()) == []
    manager.close()


def test_permissions_are_checked_at_submit_cancel_get_and_final_registration(gateway):
    manager = ImportManager(gateway)
    with identity_context(Identity('viewer')):
        path = staged(gateway)
        with pytest.raises(ServiceError) as exc: manager.submit('p', path, 'x.npz')
        assert exc.value.code == 'FORBIDDEN'
        assert not path.exists()
    with identity_context(Identity('analyst')):
        item = manager.submit('p', staged(gateway), 'x.npz')
    with identity_context(Identity('viewer')):
        assert manager.get(item['id'])['status'] == 'QUEUED'
        with pytest.raises(ServiceError): manager.cancel(item['id'])
    with identity_context(Identity('outsider')):
        with pytest.raises(ServiceError): manager.get(item['id'])
        assert manager.list() == []
    project = gateway.store.get('projects', 'p')
    project['members'] = []
    gateway.store.put('projects', 'p', project)
    manager.run_once()
    assert manager.get(item['id'])['status'] == 'FAILED'
    assert manager.get(item['id'])['error']['code'] == 'FORBIDDEN'
    assert gateway.registrations == []
    manager.close()


def test_failed_registration_and_bad_filename_clean_owned_input(gateway):
    manager = ImportManager(gateway)
    path = staged(gateway)
    with pytest.raises(ServiceError): manager.submit('p', path, '../secret.npz')
    assert not path.exists()
    item = manager.submit('p', staged(gateway), 'x.npz')
    def reject(*_): raise ServiceError('PROJECT_STORAGE_QUOTA', status=409)
    gateway._register_import_result = reject
    manager.run_once()
    assert manager.get(item['id'])['error']['code'] == 'PROJECT_STORAGE_QUOTA'
    assert list((gateway.root / 'inbox').iterdir()) == []
    manager.close()


def test_recovery_fails_lost_workers_and_missing_queued_files(gateway):
    manager = ImportManager(gateway)
    active = manager.submit('p', staged(gateway, 'active.npz'), 'active.npz')
    missing = manager.submit('p', staged(gateway, 'missing.npz'), 'missing.npz')
    raw = gateway.store.get('imports', active['id'])
    raw['status'] = 'RUNNING'
    gateway.store.put('imports', active['id'], raw)
    Path(gateway.store.get('imports', missing['id'])['_path']).unlink()
    manager.recover()
    assert all(manager.get(item['id'])['error']['code'] == 'WORKER_LOST' for item in (active, missing))
    assert list((gateway.root / 'inbox').iterdir()) == []
    manager.close()


def test_cancellation_and_registration_are_atomic(gateway):
    manager = ImportManager(gateway)
    entered, release = threading.Event(), threading.Event()
    register = gateway._register_import_result
    def blocked(*args):
        entered.set()
        assert release.wait(5)
        return register(*args)
    gateway._register_import_result = blocked
    item = manager.submit('p', staged(gateway), 'x.npz')
    runner = threading.Thread(target=manager.run_once)
    runner.start()
    assert entered.wait(5)
    results = []
    canceller = threading.Thread(target=lambda: results.append(manager.cancel(item['id'])))
    canceller.start()
    release.set()
    runner.join(timeout=5)
    canceller.join(timeout=5)
    assert results[0]['status'] == 'SUCCEEDED'
    assert manager.get(item['id'])['status'] == 'SUCCEEDED'
    assert len(gateway.registrations) == 1
    manager.close()


def test_timeout_and_memory_monitor_terminate_without_registering(gateway, monkeypatch):
    manager = ImportManager(gateway)
    monkeypatch.setattr(manager, 'TIMEOUT_SECONDS', .001)
    item = manager.submit('p', staged(gateway), 'x.npz')
    manager.run_once()
    assert manager.get(item['id'])['error']['code'] == 'IMPORT_TIMEOUT'
    monkeypatch.setattr(manager, 'TIMEOUT_SECONDS', 120)
    monkeypatch.setattr(manager, 'RSS_LIMIT_BYTES', 1)
    item = manager.submit('p', staged(gateway), 'x.npz')
    manager.run_once()
    assert manager.get(item['id'])['error']['code'] == 'IMPORT_MEMORY_LIMIT'
    assert gateway.registrations == []
    manager.close()


def test_changed_input_and_pickle_are_rejected_by_the_real_worker(gateway):
    manager = ImportManager(gateway)
    changed = manager.submit('p', staged(gateway), 'x.npz')
    Path(gateway.store.get('imports', changed['id'])['_path']).write_bytes(b'changed after submission')
    manager.run_once()
    assert manager.get(changed['id'])['error']['code'] == 'IMPORT_INPUT_CHANGED'
    path = gateway.root / 'inbox' / 'object.npy'
    np.save(path, np.array([[{'payload': 'must never deserialize'}]], dtype=object))
    unsafe = manager.submit('p', path, 'object.npy')
    manager.run_once()
    assert manager.get(unsafe['id'])['status'] == 'FAILED'
    assert gateway.registrations == []
    assert list((gateway.root / 'inbox').iterdir()) == []
    manager.close()


def test_result_parser_rejects_oversize_nonfinite_and_duplicate_fields(gateway):
    manager = ImportManager(gateway)
    path = gateway.root / 'inbox' / 'test-result.json'
    for raw in ('{"schema_version":"1.0","schema_version":"1.0"}',
                '{"schema_version":"1.0","number":NaN}', ' ' * (manager.MAX_RESULT_BYTES + 1)):
        path.write_text(raw, encoding='utf-8')
        with pytest.raises(ValueError): manager._read_result(path)
    path.unlink()
    manager.close()


def test_background_worker_closes_owned_child_and_persists_pending_queue(gateway):
    manager = ImportManager(gateway, start_worker=True)
    first = manager.submit('p', staged(gateway, 'one.npz'), 'one.npz')
    deadline = time.monotonic() + 5
    while manager._process is None and time.monotonic() < deadline: time.sleep(.005)
    assert manager._process is not None
    second = manager.submit('p', staged(gateway, 'two.npz'), 'two.npz')
    manager.close()
    assert not manager._thread.is_alive()
    assert manager.get(first['id'])['status'] == 'FAILED'
    assert manager.get(first['id'])['error']['code'] == 'WORKER_LOST'
    assert manager.get(second['id'])['status'] == 'QUEUED'
    replacement = ImportManager(gateway)
    assert replacement.run_once()
    assert replacement.get(second['id'])['status'] == 'SUCCEEDED'
    replacement.close()


def test_unexpected_registration_failure_does_not_stall_the_queue(gateway):
    manager = ImportManager(gateway)
    first = manager.submit('p', staged(gateway, 'one.npz'), 'one.npz')
    second = manager.submit('p', staged(gateway, 'two.npz'), 'two.npz')
    register = gateway._register_import_result
    def fail_once(*args):
        gateway._register_import_result = register
        raise RuntimeError('private failure text must not be exposed')
    gateway._register_import_result = fail_once
    assert manager.run_once()
    assert manager.get(first['id'])['error']['code'] == 'IMPORT_WORKER_ERROR'
    assert manager.run_once()
    assert manager.get(second['id'])['status'] == 'SUCCEEDED'
    assert list((gateway.root / 'inbox').iterdir()) == []
    manager.close()
