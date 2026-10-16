from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import threading

import pytest

from emsolver.access import Identity, identity_context, require_project
from emsolver.errors import ServiceError
from emsolver.retention import LifecycleManager
from emsolver.store import Store


NOW = datetime(2026, 10, 16, tzinfo=timezone.utc)


@pytest.fixture
def gateway(tmp_path):
    root = tmp_path / 'data'
    root.mkdir(mode=0o700)
    store = Store(root / 'metadata.sqlite3')
    project = {'id': 'p', 'policy': {'retention_days': 30}, 'members': [{'subject': 'viewer', 'role': 'viewer'}]}
    store.put('projects', 'p', project)
    def get_project(project_id, writable=False):
        item = store.get('projects', project_id)
        require_project(item, 'write' if writable else 'read')
        return item
    def add_bytes(project_id, *, outputs=0):
        item = store.get('project_storage', project_id)
        item['output_bytes'] += outputs
        assert item['output_bytes'] >= 0
        store.put('project_storage', project_id, item)
    item = SimpleNamespace(root=root, store=store, _mutex=threading.RLock(), _project=get_project,
                           _authorize=lambda project, permission: require_project(project, permission),
                           governance=SimpleNamespace(add_bytes=add_bytes))
    store.put('project_storage', 'p', {'id': 'p', 'project_id': 'p', 'input_bytes': 500, 'output_bytes': 10000})
    yield item
    store.close()


def job(gateway, identifier, **changes):
    item = {'id': identifier, 'project_id': 'p', 'retained': False, 'status': 'SUCCEEDED',
            'finished_at': (NOW - timedelta(days=40)).isoformat(), 'parent_job_id': None,
            'result': {'validation': {'numerical_status': 'PASSED', 'target_certification': 'CERTIFIED'}}}
    item.update(changes)
    gateway.store.put('jobs', identifier, item)
    directory = gateway.root / 'runs' / identifier
    (directory / 'artifacts').mkdir(parents=True)
    for name in ('manifest.json', 'config.resolved.yaml', 'validation.json', 'metrics.json', 'environment.json', 'events.jsonl'):
        (directory / 'artifacts' / name).write_text('metadata', encoding='utf-8')
    (directory / 'events.jsonl').write_text('canonical copy', encoding='utf-8')
    (directory / 'artifacts' / 'eigenvectors.npy').write_bytes(b'x' * 100)
    gateway.store.append_event(identifier, {'event': 'SUCCEEDED'})
    return item, directory


def test_only_explicitly_unretained_expired_terminal_outputs_are_planned(gateway):
    _, expired = job(gateway, 'expired')
    job(gateway, 'retained', retained=True)
    default, _ = job(gateway, 'default')
    del default['retained']
    gateway.store.put('jobs', 'default', default)
    job(gateway, 'active', status='RUNNING')
    job(gateway, 'recent', finished_at=(NOW - timedelta(days=2)).isoformat())
    job(gateway, 'invalid_time', finished_at='not a date')
    plan = LifecycleManager(gateway).plan(NOW)
    assert [item['job_id'] for item in plan['items']] == ['expired']
    assert plan['total_bytes'] == 100
    assert (expired / 'artifacts' / 'eigenvectors.npy').exists()
    assert gateway.store.get('jobs', 'expired').get('artifacts_state') is None


def test_run_preserves_metadata_events_inputs_and_numerical_status(gateway):
    original, directory = job(gateway, 'expired')
    matrix = gateway.root / 'input.npz'
    matrix.write_bytes(b'input')
    (directory / 'artifacts' / 'permutation.npy').write_bytes(b'row mapping metadata')
    (directory / 'artifacts' / 'input.npz').write_bytes(b'protected input copy')
    result = LifecycleManager(gateway).run(False, NOW)
    assert result['expired'] == ['expired'] and result['removed_bytes'] == 100
    assert not (directory / 'artifacts' / 'eigenvectors.npy').exists()
    assert (directory / 'artifacts' / 'manifest.json').is_file()
    assert (directory / 'artifacts' / 'config.resolved.yaml').is_file()
    assert (directory / 'events.jsonl').is_file()
    assert gateway.store.events('expired')[0]['event'] == 'SUCCEEDED'
    current = gateway.store.get('jobs', 'expired')
    assert current['artifacts_state'] == 'EXPIRED' and current['status'] == original['status']
    assert current['result'] == original['result'] and matrix.read_bytes() == b'input'
    assert (directory / 'artifacts' / 'permutation.npy').is_file()
    assert (directory / 'artifacts' / 'input.npz').is_file()
    assert gateway.store.get('project_storage', 'p')['output_bytes'] == 9900
    assert LifecycleManager(gateway).run(False, NOW)['expired'] == []


def test_retained_descendant_protects_transitive_parent_artifacts(gateway):
    job(gateway, 'grandparent')
    job(gateway, 'parent', parent_job_id='grandparent')
    job(gateway, 'retained_child', retained=True, parent_job_id='parent')
    assert LifecycleManager(gateway).plan(NOW)['items'] == []


def test_symlinked_outputs_are_never_followed_or_deleted(gateway, tmp_path):
    _, directory = job(gateway, 'symlinked')
    outside = tmp_path / 'private.npy'
    outside.write_bytes(b'private')
    (directory / 'artifacts' / 'linked.npy').symlink_to(outside)
    assert LifecycleManager(gateway).run(False, NOW)['expired'] == []
    assert outside.read_bytes() == b'private'


@pytest.mark.parametrize('boundary', ['audit', 'projection', 'commit'])
def test_fault_injection_restores_files_and_database(gateway, monkeypatch, boundary):
    _, directory = job(gateway, 'expired')
    if boundary == 'audit':
        monkeypatch.setattr(gateway.store, 'audit', lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError('audit fail')))
    elif boundary == 'projection':
        actual = gateway.governance.add_bytes
        def fail(project_id, *, outputs=0):
            actual(project_id, outputs=outputs)
            if outputs < 0: raise RuntimeError('projection fail')
        monkeypatch.setattr(gateway.governance, 'add_bytes', fail)
    else:
        from contextlib import contextmanager
        actual = gateway.store.transaction
        @contextmanager
        def fail():
            with actual():
                yield
                raise RuntimeError('commit fail')
        monkeypatch.setattr(gateway.store, 'transaction', fail)
    with pytest.raises(ServiceError): LifecycleManager(gateway).run(False, NOW)
    assert (directory / 'artifacts' / 'eigenvectors.npy').read_bytes() == b'x' * 100
    assert gateway.store.get('jobs', 'expired').get('artifacts_state') is None
    assert gateway.store.get('project_storage', 'p')['output_bytes'] == 10000


def test_dry_run_and_authorization_do_not_modify_outputs(gateway):
    _, directory = job(gateway, 'expired')
    with identity_context(Identity('viewer')):
        assert LifecycleManager(gateway).run(True, NOW)['total_bytes'] == 100
        with pytest.raises(ServiceError) as exc: LifecycleManager(gateway).run(False, NOW)
        assert exc.value.code == 'FORBIDDEN'
    assert (directory / 'artifacts' / 'eigenvectors.npy').exists()


def test_output_projection_cannot_become_negative(gateway):
    job(gateway, 'expired')
    gateway.store.put('project_storage', 'p', {'id': 'p', 'output_bytes': 20, 'input_bytes': 500})
    LifecycleManager(gateway).run(False, NOW)
    assert gateway.store.get('project_storage', 'p')['output_bytes'] == 0


def test_interrupted_uncommitted_quarantine_is_restored_before_replanning(gateway):
    import json
    _, directory = job(gateway, 'expired')
    relative = 'runs/expired/artifacts/eigenvectors.npy'
    folder = gateway.root / '.retention-trash' / ('a' * 32)
    target = folder / relative
    target.parent.mkdir(parents=True)
    (directory / 'artifacts' / 'eigenvectors.npy').replace(target)
    (folder / 'journal.json').write_text(json.dumps({'files': [relative], 'job_ids': ['expired']}), encoding='utf-8')
    manager = LifecycleManager(gateway)
    assert manager.plan(NOW)['items'] == []
    result = manager.run(False, NOW)
    assert result['expired'] == ['expired']
    assert not target.exists()


def test_failed_physical_purge_remains_recoverable_and_does_not_claim_freed_disk(gateway, monkeypatch):
    import shutil
    job(gateway, 'expired')
    actual = shutil.rmtree
    def failed(*_args, **_kwargs): raise OSError('disk failure')
    monkeypatch.setattr('emsolver.retention.shutil.rmtree', failed)
    first = LifecycleManager(gateway).run(False, NOW)
    assert first['cleanup_pending'] and first['disk_reclaimed_bytes'] == 0
    assert gateway.store.get('jobs', 'expired')['artifacts_state'] == 'EXPIRED'
    monkeypatch.setattr('emsolver.retention.shutil.rmtree', actual)
    second = LifecycleManager(gateway).run(False, NOW)
    assert second['cleanup_pending'] == []
    assert not any((gateway.root / '.retention-trash').iterdir())


def test_partial_rename_failure_restores_all_original_outputs(gateway, monkeypatch):
    _, directory = job(gateway, 'expired')
    (directory / 'artifacts' / 'residuals.npy').write_bytes(b'residuals')
    actual = Path.replace
    calls = []
    def fail_second(self, target):
        target = Path(target)
        if '.retention-trash' in target.parts and target.suffix == '.npy':
            calls.append(target)
            if len(calls) == 2: raise OSError('rename failed')
        return actual(self, target)
    monkeypatch.setattr(Path, 'replace', fail_second)
    with pytest.raises(ServiceError): LifecycleManager(gateway).run(False, NOW)
    assert (directory / 'artifacts' / 'eigenvectors.npy').read_bytes() == b'x' * 100
    assert (directory / 'artifacts' / 'residuals.npy').read_bytes() == b'residuals'
    assert gateway.store.get('jobs', 'expired').get('artifacts_state') is None
    assert gateway.store.get('project_storage', 'p')['output_bytes'] == 10000
