import json
from pathlib import Path
import stat
import warnings
import zipfile

import numpy as np
import pytest

from emsolver.cluster import ClusterCoordinator
from emsolver.cluster_protocol import execution_fingerprint
from emsolver.cluster_results import ClusterResultReceiver
from emsolver.errors import ServiceError
from emsolver.integrity import file_hash
from emsolver.numerics import run_solver
from emsolver.service import Service


def write_bundle(path, artifacts, result, *, extra=None):
    with zipfile.ZipFile(path, 'w') as archive:
        for item in artifacts.iterdir():
            archive.write(item, item.name)
        archive.writestr('worker.result.json', json.dumps(result))
        if extra:
            for name, value in extra:
                archive.writestr(name, value)
    return path


@pytest.fixture
def remote_run(tmp_path):
    service = Service(tmp_path / 'server', start_runner=False)
    if not hasattr(service, 'cluster') or service.cluster is None:
        service.cluster = ClusterCoordinator(service)
    project = service.create_project('Remote results')
    matrix = service.generate_sample(project['id'], n=12)
    plan = service.create_plan(config={'problem': {'matrix_id': matrix['id']},
        'target': {'k': 2, 'sigma': .1},
        'resources': {'threads': 1, 'memory_limit_gib': 1, 'disk_limit_gib': .05, 'walltime_seconds': 20}})
    job = service.submit_job(plan['id'])
    resources = {'threads': 4, 'memory_gib': 8, 'disk_gib': 10, 'devices': ['cpu']}
    enrolled = service.cluster.enroll('node-results', [project['id']], resources, ['scipy'])
    worker_id = enrolled['worker']['id']
    lease = service.cluster.claim(worker_id, {**resources, 'backends': ['scipy'], 'execution': execution_fingerprint()})
    source = service.store.get('matrices', matrix['id'])['_path']
    artifacts = tmp_path / 'node-output'
    result = run_solver(source, job['config'], artifacts)
    bundle = write_bundle(tmp_path / 'result.zip', artifacts, result)
    yield service, worker_id, job['id'], lease, bundle, artifacts, result
    service.close()


def receive(remote_run, bundle=None):
    service, worker, job_id, lease, original, *_ = remote_run
    return ClusterResultReceiver(service).complete(worker, job_id, lease['lease_token'], lease['attempt'], bundle or original)


def published_path(remote_run):
    service, _, job_id, *_ = remote_run
    return service.root / 'runs' / job_id / 'artifacts'


def test_real_bundle_is_independently_verified_and_manifest_is_rebuilt(remote_run):
    finished = receive(remote_run)
    assert finished['status'] == 'SUCCEEDED'
    assert finished['result']['validation']['numerical_status'] == 'PASSED'
    assert finished['result']['validation']['target_certification'] == 'CERTIFIED'
    assert finished['metrics']['remote_worker'] is True
    assert finished['metrics']['remote_worker_observations']['trusted'] is False
    destination = published_path(remote_run)
    assert not (destination / 'worker.result.json').exists()
    manifest = json.loads((destination / 'manifest.json').read_text())
    assert manifest['verification_source'] == 'control_plane_independent'
    for name, digest in manifest['artifacts'].items():
        assert file_hash(destination / name) == digest
    assert json.loads((destination / 'validation.json').read_text()) == finished['result']['validation']
    assert not list(destination.parent.glob('.cluster-*'))


@pytest.mark.parametrize('entry', ['../escape.txt', '/absolute.npy', 'subdir/eigenvalues.npy',
                                  'worker.spec.json', 'eigenvalues.npy\\evil'])
def test_archive_paths_and_non_artifact_names_are_rejected(remote_run, tmp_path, entry):
    *_, artifacts, result = remote_run
    malicious = write_bundle(tmp_path / 'bad.zip', artifacts, result, extra=[(entry, 'bad')])
    with pytest.raises(ServiceError) as failure:
        receive(remote_run, malicious)
    assert failure.value.code == 'INVALID_RESULT_ARCHIVE'
    assert not published_path(remote_run).exists()
    assert not (tmp_path / 'escape.txt').exists()


def test_duplicate_member_and_symlink_member_are_rejected(remote_run, tmp_path):
    *_, artifacts, result = remote_run
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        duplicate = write_bundle(tmp_path / 'duplicate.zip', artifacts, result,
                                 extra=[('worker.result.json', '{}')])
    with pytest.raises(ServiceError, match='INVALID_RESULT_ARCHIVE'):
        receive(remote_run, duplicate)
    symlink = zipfile.ZipInfo('eigenvalues.npy')
    symlink.create_system = 3
    symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(tmp_path / 'symlink.zip', 'w') as archive:
        archive.writestr(symlink, '/private/source')
        archive.writestr('worker.result.json', '{}')
    with pytest.raises(ServiceError, match='INVALID_RESULT_ARCHIVE'):
        receive(remote_run, tmp_path / 'symlink.zip')


@pytest.mark.parametrize('raw', ['{"status":"SUCCEEDED","status":"FAILED"}',
                                 '{"status":"SUCCEEDED","metric":NaN}',
                                 '{"status":"SUCCEEDED","metric":1e999}',
                                 'x' * (1024**2 + 1)])
def test_worker_result_json_is_strict_and_bounded(remote_run, tmp_path, raw):
    *_, artifacts, _ = remote_run
    with zipfile.ZipFile(tmp_path / 'invalid-json.zip', 'w') as archive:
        for item in artifacts.iterdir():
            archive.write(item, item.name)
        archive.writestr('worker.result.json', raw)
    with pytest.raises(ServiceError, match='INVALID_WORKER_RESULT'):
        receive(remote_run, tmp_path / 'invalid-json.zip')


@pytest.mark.parametrize('status', [{}, [], None, 1])
def test_worker_status_requires_exact_string_enum(remote_run, tmp_path, status):
    *_, artifacts, result = remote_run
    result['status'] = status
    archive = write_bundle(tmp_path / 'invalid-status.zip', artifacts, result)
    with pytest.raises(ServiceError, match='INVALID_WORKER_RESULT'):
        receive(remote_run, archive)


def test_rehashed_zero_vector_cannot_fabricate_success(remote_run, tmp_path):
    *_, artifacts, result = remote_run
    vectors = np.load(artifacts / 'eigenvectors.npy', allow_pickle=False)
    vectors[:, 0] = 0
    np.save(artifacts / 'eigenvectors.npy', vectors, allow_pickle=False)
    manifest = json.loads((artifacts / 'manifest.json').read_text())
    manifest['artifacts']['eigenvectors.npy'] = file_hash(artifacts / 'eigenvectors.npy')
    (artifacts / 'manifest.json').write_text(json.dumps(manifest))
    forged = write_bundle(tmp_path / 'forged.zip', artifacts, result)
    finished = receive(remote_run, forged)
    assert finished['status'] == 'FAILED'
    assert finished['error']['code'] == 'REMOTE_VALIDATION_FAILED'
    assert finished['result']['validation']['numerical_status'] == 'FAILED'
    assert not published_path(remote_run).exists()


def test_identical_retry_does_not_reverify_or_overwrite_terminal_data(remote_run, monkeypatch):
    finished = receive(remote_run)
    destination = published_path(remote_run)
    before = {path.name: file_hash(path) for path in destination.iterdir()}
    monkeypatch.setattr('emsolver.cluster_results.verify_bounded', lambda *_: pytest.fail('reverified completed attempt'))
    assert receive(remote_run) == finished
    assert {path.name: file_hash(path) for path in destination.iterdir()} == before


def test_conflicting_retry_is_rejected_without_overwriting_terminal_data(remote_run, tmp_path):
    receive(remote_run)
    *_, artifacts, result = remote_run
    result['metrics']['elapsed_seconds'] = 123
    changed = write_bundle(tmp_path / 'changed.zip', artifacts, result)
    with pytest.raises(ServiceError, match='different result digest'):
        receive(remote_run, changed)
    assert json.loads((published_path(remote_run) / 'metrics.json').read_text()).get('elapsed_seconds') != 123


def test_stale_lease_and_cancellation_cannot_publish(remote_run):
    service, _, job_id, *_ = remote_run
    service.cancel_job(job_id)
    finished = receive(remote_run)
    assert finished['status'] == 'CANCELLED'
    assert not published_path(remote_run).exists()


def test_expired_lease_cannot_publish(remote_run):
    service, _, job_id, *_ = remote_run
    with service.store.transaction():
        service.store.db.execute('UPDATE cluster_job_attempts SET expires_at=0 WHERE job_id=?', (job_id,))
    with pytest.raises(ServiceError, match='Lease is no longer active'):
        receive(remote_run)
    assert not published_path(remote_run).exists()


def test_publication_rollback_removes_new_files_and_restores_orphan(remote_run, monkeypatch):
    service, _, job_id, *_ = remote_run
    destination = published_path(remote_run)
    destination.mkdir(parents=True)
    (destination / 'orphan.txt').write_text('old unpublished data')
    original_finish = service._finish
    def broken_finish(*args, **kwargs):
        original_finish(*args, **kwargs)
        raise RuntimeError('database failure')
    monkeypatch.setattr(service, '_finish', broken_finish)
    with pytest.raises(RuntimeError, match='database failure'):
        receive(remote_run)
    assert service.store.get('jobs', job_id)['status'] == 'RUNNING'
    assert {path.name for path in destination.iterdir()} == {'orphan.txt'}
    assert (destination / 'orphan.txt').read_text() == 'old unpublished data'
    assert not list(destination.parent.glob('.cluster-*'))


def test_worker_claims_and_messages_do_not_enter_authoritative_results(remote_run, tmp_path):
    *_, artifacts, result = remote_run
    result.update(status='PARTIAL', eigenvalues=[999.], residuals=[0.], n_accepted=999,
                  error_code='PRIVATE_MESSAGE', message='PRIVATE_MESSAGE',
                  attempts=[{'message': 'PRIVATE_MESSAGE'}])
    result['metrics']['path'] = 'PRIVATE_MESSAGE'
    changed = write_bundle(tmp_path / 'claims.zip', artifacts, result)
    finished = receive(remote_run, changed)
    assert finished['status'] == 'SUCCEEDED'
    assert finished['result']['n_accepted'] == 2
    assert finished['result']['eigenvalues'] != [999.]
    assert 'PRIVATE_MESSAGE' not in json.dumps(finished['result'])
    assert finished['result']['attempts'] == []


def test_unverified_exports_metadata_and_checkpoints_are_not_published(remote_run, tmp_path):
    service, _, job_id, _, _, artifacts, result = remote_run
    unverified = {'eigenvalues.csv', 'eigenvalues.scipy.npz', 'eigenvectors.scipy.npz',
                  'eigenvectors.npz', 'permutation.npy', 'strategy.json', 'environment.json',
                  'diagnostics.json', 'memory-estimate.json', 'subspace.checkpoint.npz',
                  'exact-block.checkpoint.npz'}
    for name in unverified:
        (artifacts / name).write_text('FORGED_EXPORT_999999')
    manifest = json.loads((artifacts / 'manifest.json').read_text())
    manifest['artifacts'] = {path.name: file_hash(path) for path in artifacts.iterdir() if path.name != 'manifest.json'}
    (artifacts / 'manifest.json').write_text(json.dumps(manifest))
    forged = write_bundle(tmp_path / 'forged-exports.zip', artifacts, result)
    finished = receive(remote_run, forged)
    assert finished['status'] == 'SUCCEEDED'
    canonical = json.loads((published_path(remote_run) / 'manifest.json').read_text())
    assert not unverified.intersection(canonical['artifacts'])
    assert unverified <= set(canonical['suppressed_remote_artifacts'])
    for name in unverified:
        with pytest.raises(ServiceError) as missing:
            service.download(job_id, name)
        assert missing.value.code == 'ARTIFACT_NOT_FOUND'


def test_cancellation_during_independent_verification_wins_publication(remote_run, monkeypatch):
    import emsolver.cluster_results as module
    service, _, job_id, *_ = remote_run
    actual = module.verify_bounded
    def cancel_after_verification(*args):
        validation = actual(*args)
        service.cancel_job(job_id)
        return validation
    monkeypatch.setattr(module, 'verify_bounded', cancel_after_verification)
    assert receive(remote_run)['status'] == 'CANCELLED'
    assert not published_path(remote_run).exists()
    assert not list(published_path(remote_run).parent.glob('.cluster-*'))


def test_unavailable_verification_never_publishes_worker_claimed_success(remote_run, monkeypatch):
    monkeypatch.setattr('emsolver.cluster_results.verify_bounded', lambda *_: None)
    finished = receive(remote_run)
    assert finished['status'] == 'FAILED'
    assert finished['error']['code'] == 'REMOTE_VERIFICATION_UNAVAILABLE'
    assert not published_path(remote_run).exists()
    service = remote_run[0]
    assert service.governance.usage(finished['project_id'])['output_bytes'] == 0


def test_expiry_during_independent_verification_cannot_publish(remote_run, monkeypatch):
    import emsolver.cluster_results as module
    service, _, job_id, *_ = remote_run
    actual = module.verify_bounded
    def expire_after_verification(*args):
        validation = actual(*args)
        with service.store.transaction():
            service.store.db.execute('UPDATE cluster_job_attempts SET expires_at=0 WHERE job_id=?', (job_id,))
        return validation
    monkeypatch.setattr(module, 'verify_bounded', expire_after_verification)
    with pytest.raises(ServiceError, match='Lease is no longer active'):
        receive(remote_run)
    assert not published_path(remote_run).exists()
    assert not list(published_path(remote_run).parent.glob('.cluster-*'))


def test_decompressed_byte_limit_precedes_numerical_verification(remote_run, tmp_path, monkeypatch):
    import emsolver.cluster_results as module
    *_, artifacts, result = remote_run
    (artifacts / 'events.jsonl').write_bytes(b'x' * 1024**2)
    with zipfile.ZipFile(tmp_path / 'compressed.zip', 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for item in artifacts.iterdir():
            archive.write(item, item.name)
        archive.writestr('worker.result.json', json.dumps(result))
    assert (tmp_path / 'compressed.zip').stat().st_size < 128 * 1024
    monkeypatch.setattr(module, 'MAX_ARCHIVE_BYTES', 128 * 1024)
    monkeypatch.setattr(module, 'verify_bounded', lambda *_: pytest.fail('verified oversized archive'))
    with pytest.raises(ServiceError, match='RESULT_ARCHIVE_TOO_LARGE'):
        receive(remote_run, tmp_path / 'compressed.zip')
    assert not published_path(remote_run).exists()


def test_input_identity_is_rechecked_under_publication_fence(remote_run, monkeypatch):
    service, _, job_id, *_ = remote_run
    job = service.store.get('jobs', job_id)
    source = Path(service.store.get('matrices', job['matrix_id'])['_path'])
    actual = service.cluster.finish
    def change_source_before_fence(*args, **kwargs):
        source.chmod(0o600)
        source.write_bytes(source.read_bytes() + b'changed-after-verification')
        source.chmod(0o400)
        return actual(*args, **kwargs)
    monkeypatch.setattr(service.cluster, 'finish', change_source_before_fence)
    with pytest.raises(ServiceError, match='INPUT_HASH_MISMATCH'):
        receive(remote_run)
    assert not published_path(remote_run).exists()
    assert service.store.get('jobs', job_id)['status'] == 'RUNNING'


def test_receiver_obeys_attempt_admission_before_staging_or_verification(remote_run, monkeypatch):
    from emsolver.cluster_admission import acquire_admission
    service, worker_id, job_id, lease, *_ = remote_run
    held = acquire_admission(service, worker_id, job_id, lease['attempt'])
    monkeypatch.setattr('emsolver.cluster_results.verify_bounded', lambda *_: pytest.fail('verified busy attempt'))
    try:
        with pytest.raises(ServiceError) as busy:
            receive(remote_run)
        assert busy.value.status == 503 and busy.value.code == 'CLUSTER_INTAKE_BUSY'
    finally:
        held.close()
    assert not list((service.root / 'runs' / job_id).glob('.cluster-*'))


def test_receiver_reuses_bound_api_admission_and_releases_direct_guard(remote_run):
    from emsolver.cluster_admission import acquire_admission
    service, worker_id, job_id, lease, *_ = remote_run
    held = acquire_admission(service, worker_id, job_id, lease['attempt'])
    try:
        with held.bind():
            assert receive(remote_run)['status'] == 'SUCCEEDED'
    finally:
        held.close()
    again = acquire_admission(service, worker_id, job_id, lease['attempt'])
    again.close()
