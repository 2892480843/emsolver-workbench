"""Fault-injection acceptance of private backup filesystem/DB consistency."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import signal
import threading
import time
import zipfile

import pytest

from emsolver.errors import ServiceError
from emsolver.integrity import file_hash
from emsolver.service import Service


@pytest.fixture
def published(tmp_path):
    service = Service(tmp_path / 'source', start_runner=False)
    try:
        project = service.create_project('Backup consistency')
        matrix = service.generate_sample(project['id'], n=30)
        plan = service.create_plan(config={
            'problem': {'matrix_id': matrix['id']},
            'target': {'k': 3, 'sigma': .1},
            'resources': {'threads': 1},
        })
        job = service.submit_job(plan['id'])
        service._execute(job['id'])
        assert service.get_job(job['id'])['status'] == 'SUCCEEDED'
        yield service, project, matrix, job
    finally:
        service.close()


def test_running_import_prevents_backup_without_blocking_its_monitor(tmp_path, monkeypatch):
    import emsolver.imports as imports
    from emsolver.numerics import generate_sample

    service = Service(tmp_path / 'source', start_runner=False)
    manager = service.import_manager
    actual_spawn = imports.subprocess.Popen
    runner = None
    stopped = []

    def paused_worker(args, **kwargs):
        process = actual_spawn(args, **kwargs)
        # Pause an actual child before validation completes so the persisted
        # RUNNING state remains deterministic while backup checks its gate.
        os.kill(process.pid, signal.SIGSTOP)
        stopped.append(process)
        return process

    monkeypatch.setattr(imports.subprocess, 'Popen', paused_worker)
    try:
        project = service.create_project('Import in progress')
        source = service.root / 'inbox' / 'upload.npz'
        generate_sample(source, n=30)
        source.chmod(0o600)
        item = manager.submit(project['id'], source, 'matrix.npz')
        runner = threading.Thread(target=manager.run_once)
        runner.start()
        deadline = time.monotonic() + 5
        while manager._process is None and time.monotonic() < deadline:
            time.sleep(.005)
        assert manager._process is not None and manager._process.poll() is None
        assert manager.get(item['id'])['status'] == 'RUNNING'
        with pytest.raises(ServiceError) as error:
            service.backup()
        assert error.value.code == 'BACKUP_BUSY'
        assert list((service.root / 'exports').iterdir()) == []
    finally:
        for process in stopped:
            if process.poll() is None:
                os.kill(process.pid, signal.SIGCONT)
        if manager._active_id is not None:
            manager.cancel(manager._active_id)
        if runner is not None:
            runner.join(timeout=10)
        service.close()
        assert runner is None or not runner.is_alive()


def test_backup_recovers_interrupted_retention_before_snapshot_without_new_expiry(published, tmp_path):
    service, _, _, job = published
    current = service.store.get('jobs', job['id'])
    current.update(retained=False, finished_at=(datetime.now(timezone.utc) - timedelta(days=40)).isoformat())
    service.store.put('jobs', job['id'], current)
    relative = Path('runs') / job['id'] / 'artifacts' / 'eigenvectors.npy'
    original = service.root / relative
    expected_hash = file_hash(original)
    folder = service.root / '.retention-trash' / ('a' * 32)
    staged = folder / relative
    staged.parent.mkdir(parents=True, mode=0o700)
    original.replace(staged)
    (folder / 'journal.json').write_text(json.dumps({'files': [str(relative)], 'job_ids': [job['id']]}), encoding='utf-8')

    archive = service.backup()

    assert original.is_file() and file_hash(original) == expected_hash
    assert not folder.exists()
    assert service.get_job(job['id']).get('artifacts_state', 'AVAILABLE') == 'AVAILABLE'
    with zipfile.ZipFile(archive) as contents:
        assert str(relative) in contents.namelist()
    destination = tmp_path / 'restored'
    restored = service.restore_backup(archive, destination)
    assert restored['degraded'] is False and restored['missing_files'] == []
    assert file_hash(destination / relative) == expected_hash
    other = Service(destination, start_runner=False)
    try:
        assert other.get_job(job['id'])['status'] == 'SUCCEEDED'
        assert len(other.results(job['id'])['eigenvalues']) == 3
    finally:
        other.close()


def test_undeclared_missing_published_payload_is_rejected_or_explicitly_degraded(published, tmp_path):
    service, _, _, job = published
    original = service.backup()
    relative = f"runs/{job['id']}/artifacts/eigenvectors.npy"
    omitted = tmp_path / 'undeclared-omission.zip'
    with zipfile.ZipFile(original) as source, zipfile.ZipFile(omitted, 'w') as target:
        manifest = json.loads(source.read('backup-manifest.json'))
        assert relative in manifest['files']
        del manifest['files'][relative]
        for item in source.infolist():
            if item.filename == relative:
                continue
            content = json.dumps(manifest).encode('utf-8') if item.filename == 'backup-manifest.json' else source.read(item)
            target.writestr(item, content)

    strict_destination = tmp_path / 'strict'
    with pytest.raises(ServiceError):
        service.restore_backup(omitted, strict_destination)
    assert not strict_destination.exists()
    assert not any(path.name.startswith('.restore-') for path in tmp_path.iterdir())

    destination = tmp_path / 'degraded'
    restored = service.restore_backup(omitted, destination, allow_missing=True)
    assert restored['degraded'] is True
    assert relative in restored['missing_files']
    other = Service(destination, start_runner=False)
    try:
        saved = other.get_job(job['id'])
        assert saved['status'] == 'SUCCEEDED'
        assert saved['artifacts_state'] == 'MISSING'
        assert relative in saved['missing_artifacts']
        assert not (destination / relative).exists()
        assert (destination / relative).with_name('manifest.json').is_file()
        assert other.get_matrix(saved['matrix_id']).get('availability') != 'MISSING'
    finally:
        other.close()


def test_backup_refuses_an_unrecoverable_retention_journal(published):
    service, _, _, _ = published
    folder = service.root / '.retention-trash' / ('b' * 32)
    folder.mkdir(parents=True)
    (folder / 'journal.json').write_text('{"files": ["../outside"], "job_ids": ["absent"]}', encoding='utf-8')
    with pytest.raises(ServiceError) as error:
        service.backup()
    assert error.value.code == 'RETENTION_RECOVERY_PENDING'
    assert folder.is_dir()
    assert list((service.root / 'exports').iterdir()) == []


def test_intentionally_expired_payloads_restore_without_false_missing_status(published, tmp_path):
    service, _, _, job = published
    current = service.store.get('jobs', job['id'])
    current.update(retained=False, finished_at=(datetime.now(timezone.utc) - timedelta(days=40)).isoformat())
    service.store.put('jobs', job['id'], current)
    expired = service.lifecycle_cleanup(False)
    assert expired['expired'] == [job['id']]
    relative = Path('runs') / job['id'] / 'artifacts' / 'eigenvectors.npy'
    assert not (service.root / relative).exists()

    archive = service.backup()
    destination = tmp_path / 'expired-restored'
    restored = service.restore_backup(archive, destination)
    assert restored['degraded'] is False and restored['missing_files'] == []
    other = Service(destination, start_runner=False)
    try:
        saved = other.get_job(job['id'])
        assert saved['status'] == 'SUCCEEDED' and saved['artifacts_state'] == 'EXPIRED'
        assert saved['result']['validation']['numerical_status'] == 'PASSED'
        assert not (destination / relative).exists()
        assert (destination / relative).with_name('manifest.json').is_file()
    finally:
        other.close()
