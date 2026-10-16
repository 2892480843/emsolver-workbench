import hashlib
import io
import json
import os
import threading
import time
import zipfile
from datetime import datetime, timedelta, timezone

import httpx
import numpy as np
import pytest
from scipy import sparse

from emsolver.contracts import resolve_config
from emsolver.remote_worker import RemoteWorker, WorkerError, load_token, validate_server_url


def expires(seconds=30):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def claim(data=b'matrix', *, seconds=30, backend='scipy'):
    config = resolve_config({'target': {'k': 2}, 'solver': {'backend': backend},
                             'resources': {'threads': 1, 'memory_limit_gib': 1,
                                           'disk_limit_gib': 0.05, 'walltime_seconds': 20}})
    return {'job': {'id': 'job-test', 'config': config},
            'lease': {'token': 'lease-secret', 'attempt': 1, 'expires_at': expires(seconds)},
            'input': {'filename': 'input.npz', 'bytes': len(data),
                      'sha256': hashlib.sha256(data).hexdigest(), 'url': '/input'}}


def worker(tmp_path, handler, **kwargs):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return RemoteWorker('http://127.0.0.1:8000', 'node-secret', tmp_path / 'work',
                        client=client, heartbeat_interval=0.05, poll_interval=0.01, **kwargs)


def test_token_requires_owned_private_regular_file(tmp_path):
    path = tmp_path / 'token'
    path.write_text('node-secret\n')
    path.chmod(0o600)
    assert load_token(path) == 'node-secret'
    path.chmod(0o644)
    with pytest.raises(WorkerError, match='TOKEN_FILE_UNSAFE'):
        load_token(path)
    path.chmod(0o600)
    alias = tmp_path / 'alias'
    alias.symlink_to(path)
    with pytest.raises(WorkerError, match='TOKEN_FILE_UNSAFE'):
        load_token(alias)
    path.write_text('secret\nAuthorization: injected')
    with pytest.raises(WorkerError, match='INVALID_TOKEN'):
        load_token(path)


@pytest.mark.parametrize('url', ['http://example.org', 'https://secret@example.org',
                                 'https://example.org/?token=secret', 'ftp://example.org'])
def test_remote_server_requires_secure_origin_without_embedded_credentials(url):
    with pytest.raises(WorkerError):
        validate_server_url(url)
    assert validate_server_url('http://localhost:8000') == 'http://localhost:8000'
    assert validate_server_url('http://example.org', allow_insecure_http=True) == 'http://example.org'


@pytest.mark.parametrize('interval', [float('nan'), float('inf'), 0., -1.])
def test_worker_rejects_invalid_polling_intervals(tmp_path, interval):
    with pytest.raises(WorkerError, match='INVALID_INTERVAL'):
        RemoteWorker('http://127.0.0.1:8000', 'node-secret', tmp_path / 'work',
                     heartbeat_interval=interval)


def test_input_download_rejects_hash_mismatch_and_does_not_publish(tmp_path):
    failures = []
    def handler(request):
        assert request.headers['Authorization'] == 'Bearer node-secret'
        if request.url.path.endswith('/claim'):
            return httpx.Response(200, json=claim())
        if request.url.path == '/input':
            assert request.headers['X-EMSolver-Lease'] == 'lease-secret'
            assert request.headers['X-EMSolver-Attempt'] == '1'
            return httpx.Response(200, content=b'broken')
        if request.url.path.endswith('/failure'):
            failures.append(json.loads(request.content))
            return httpx.Response(200, json={})
        if request.url.path.endswith('/heartbeat'):
            return httpx.Response(200, json={'expires_at': expires(), 'cancel_requested': False})
        raise AssertionError(request.url)
    assert worker(tmp_path, handler).run_once() is True
    assert failures[0]['code'] == 'INPUT_HASH_MISMATCH'
    assert not list((tmp_path / 'work').glob('*/worker.spec.json'))


def test_cross_origin_input_is_rejected_before_sending_credentials(tmp_path):
    calls = []
    def handler(request):
        calls.append(str(request.url))
        if request.url.path.endswith('/claim'):
            response = claim()
            response['input']['url'] = 'https://attacker.invalid/input'
            return httpx.Response(200, json=response)
        if request.url.path.endswith('/failure'):
            assert json.loads(request.content)['code'] == 'UNSAFE_DOWNLOAD_URL'
            return httpx.Response(200, json={})
        if request.url.path.endswith('/heartbeat'):
            return httpx.Response(200, json={'expires_at': expires(), 'cancel_requested': False})
        raise AssertionError(request.url)
    worker(tmp_path, handler).run_once()
    assert not any('attacker' in url for url in calls)


def test_expired_claim_never_starts_or_publishes(tmp_path, monkeypatch):
    def handler(request):
        assert request.url.path.endswith('/claim')
        return httpx.Response(200, json=claim(seconds=-1))
    monkeypatch.setattr('subprocess.Popen', lambda *a, **k: pytest.fail('started stale work'))
    assert worker(tmp_path, handler).run_once() is True


@pytest.mark.parametrize('clock_skew', [-12 * 3600, 12 * 3600])
def test_relative_lease_ttl_ignores_server_clock_skew(tmp_path, clock_skew):
    seen = []
    def handler(request):
        if request.url.path.endswith('/claim'):
            response = claim()
            response['lease'].update(expires_at=time.time() + clock_skew, lease_seconds=2.)
            return httpx.Response(200, json=response)
        assert request.url.path.endswith('/heartbeat')
        return httpx.Response(200, json={'expires_at': time.time() + clock_skew,
            'lease_seconds': 2., 'cancel_requested': False})
    node = worker(tmp_path, handler)
    def execute(_assignment, _directory, lease):
        assert 0 < lease.deadline - time.monotonic() <= 2.
        seen.append(True)
    node._execute = execute
    node.run_once()
    assert seen == [True]


def test_initial_ttl_subtracts_request_latency_and_never_starts_late_claim(tmp_path):
    def handler(request):
        assert request.url.path.endswith('/claim')
        response = claim()
        response['lease'].update(expires_at=time.time() + 3600, lease_seconds=0.03)
        time.sleep(0.05)
        return httpx.Response(200, json=response)
    node = worker(tmp_path, handler)
    node._execute = lambda *_: pytest.fail('started work after conservative TTL elapsed')
    assert node.run_once() is True


def test_late_heartbeat_cannot_extend_previous_monotonic_ttl(tmp_path):
    from emsolver.remote_worker import _Lease
    def handler(request):
        assert request.url.path.endswith('/heartbeat')
        time.sleep(.1)
        return httpx.Response(200, json={'expires_at': time.time() + 12 * 3600,
            'lease_seconds': 60., 'cancel_requested': False})
    node = worker(tmp_path, handler)
    lease = _Lease(node, 'job-test', {'token': 'lease-secret', 'attempt': 1,
        'expires_at': time.time() + 12 * 3600, 'lease_seconds': .06}, request_started=time.monotonic())
    original = lease.deadline
    lease.thread.start()
    lease.thread.join(timeout=.5)
    assert lease.reason == 'LEASE_EXPIRED' and lease.deadline == original
    lease.close()


def test_unsupported_backend_is_not_silently_replaced(tmp_path):
    failures = []
    def handler(request):
        if request.url.path.endswith('/claim'):
            return httpx.Response(200, json=claim(backend='slepc'))
        if request.url.path.endswith('/heartbeat'):
            return httpx.Response(200, json={'expires_at': expires(), 'cancel_requested': False})
        failures.append(json.loads(request.content))
        return httpx.Response(200, json={})
    worker(tmp_path, handler).run_once()
    assert failures[0]['code'] == 'UNSUPPORTED_BACKEND'


@pytest.mark.parametrize('interruption', ['cancel', 'revoke', 'unreachable', 'late_heartbeat'])
def test_running_child_is_killed_when_lease_cannot_authorize_work(tmp_path, monkeypatch, interruption):
    import subprocess
    actual_popen = subprocess.Popen
    children = []
    started = threading.Event()
    response = claim(seconds=0.5)
    response['lease']['expires_at'] = time.time() + 0.5
    requests = []
    def sleeping_child(*args, **kwargs):
        child = actual_popen([os.sys.executable, '-c', 'import time; time.sleep(30)'], **kwargs)
        children.append(child)
        started.set()
        return child
    monkeypatch.setattr(subprocess, 'Popen', sleeping_child)
    def handler(request):
        requests.append(request.url.path)
        if request.url.path.endswith('/claim'):
            return httpx.Response(200, json=response)
        if request.url.path == '/input':
            return httpx.Response(200, content=b'matrix')
        assert request.url.path.endswith('/heartbeat')
        if started.is_set():
            if interruption == 'cancel':
                return httpx.Response(200, json={'expires_at': expires(), 'cancel_requested': True})
            if interruption == 'revoke':
                return httpx.Response(409, json={'detail': 'stale lease'})
            if interruption == 'unreachable':
                return httpx.Response(503)
            time.sleep(0.6)
            return httpx.Response(200, json={'expires_at': expires(), 'cancel_requested': False})
        return httpx.Response(200, json={'expires_at': response['lease']['expires_at'], 'cancel_requested': False})
    assert worker(tmp_path, handler).run_once() is True
    assert len(children) == 1 and children[0].poll() is not None
    assert children[0].returncode != 0
    assert not any(path.endswith(('/complete', '/failure')) for path in requests)
    assert not list((tmp_path / 'work').iterdir())


def test_download_bound_is_enforced_before_creating_worker_spec(tmp_path):
    failures = []
    def handler(request):
        if request.url.path.endswith('/claim'):
            return httpx.Response(200, json=claim())
        if request.url.path == '/input':
            return httpx.Response(200, content=b'matrix-and-extra')
        if request.url.path.endswith('/heartbeat'):
            return httpx.Response(200, json={'expires_at': expires(), 'cancel_requested': False})
        assert request.url.path.endswith('/failure')
        failures.append(json.loads(request.content)['code'])
        return httpx.Response(200, json={})
    worker(tmp_path, handler).run_once()
    assert failures == ['DOWNLOAD_TOO_LARGE']


@pytest.mark.parametrize('limit,code', [('memory_limit_gib', 'RESOURCE_EXHAUSTED'),
                                        ('walltime_seconds', 'WALLTIME_EXCEEDED')])
def test_frozen_resource_budget_kills_child_and_reports_code(tmp_path, monkeypatch, limit, code):
    import subprocess
    actual_popen = subprocess.Popen
    children, failures = [], []
    response = claim()
    response['job']['config']['resources'][limit] = 0.000001 if limit == 'memory_limit_gib' else 0.05
    def sleeping_child(*args, **kwargs):
        child = actual_popen([os.sys.executable, '-c', 'import time; time.sleep(30)'], **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(subprocess, 'Popen', sleeping_child)
    def handler(request):
        if request.url.path.endswith('/claim'):
            return httpx.Response(200, json=response)
        if request.url.path == '/input':
            return httpx.Response(200, content=b'matrix')
        if request.url.path.endswith('/heartbeat'):
            return httpx.Response(200, json={'expires_at': expires(), 'cancel_requested': False})
        assert request.url.path.endswith('/failure')
        failures.append(json.loads(request.content)['code'])
        return httpx.Response(200, json={})
    worker(tmp_path, handler).run_once()
    assert failures == [code]
    assert len(children) == 1 and children[0].returncode != 0


def test_signal_stop_event_kills_running_child_without_publishing(tmp_path, monkeypatch):
    import subprocess
    actual_popen = subprocess.Popen
    children = []
    stop = threading.Event()
    def sleeping_child(*args, **kwargs):
        child = actual_popen([os.sys.executable, '-c', 'import time; time.sleep(30)'], **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(subprocess, 'Popen', sleeping_child)
    def handler(request):
        if request.url.path.endswith('/claim'):
            return httpx.Response(200, json=claim())
        if request.url.path == '/input':
            return httpx.Response(200, content=b'matrix')
        assert request.url.path.endswith('/heartbeat')
        if children:
            stop.set()
        return httpx.Response(200, json={'expires_at': expires(), 'cancel_requested': False})
    assert worker(tmp_path, handler, stop_event=stop).run_once() is True
    assert len(children) == 1 and children[0].returncode != 0


def prepare_upload(tmp_path, handler, *, seconds=2.):
    from emsolver.remote_worker import _Lease
    node = worker(tmp_path, handler)
    directory = tmp_path / 'attempt'
    (directory / 'artifacts').mkdir(parents=True)
    for name in ('manifest.json', 'eigenvalues.npy', 'eigenvectors.npy', 'residuals.npy'):
        (directory / 'artifacts' / name).write_bytes(b'bounded test artifact')
    (directory / 'worker.result.json').write_text('{"status":"SUCCEEDED"}')
    lease = _Lease(node, 'job-test', {'token': 'lease-secret', 'attempt': 1,
        'expires_at': time.time() + seconds, 'lease_seconds': seconds}, request_started=time.monotonic())
    return node, directory, lease


def archive_from_request(request):
    body = request.read()
    return body[body.index(b'PK\x03\x04'):body.rindex(b'PK\x05\x06') + 22]


def test_upload_retries_restart_outage_with_identical_archive_and_heartbeats(tmp_path):
    payloads, beats = [], []
    def handler(request):
        if request.url.path.endswith('/heartbeat'):
            beats.append(True)
            return httpx.Response(200, json={'expires_at': time.time() - 3600,
                'lease_seconds': 2., 'cancel_requested': False})
        assert request.url.path.endswith('/complete')
        assert request.headers['X-EMSolver-Lease'] == 'lease-secret'
        assert request.headers['X-EMSolver-Attempt'] == '1'
        payloads.append(archive_from_request(request))
        if len(payloads) == 1:
            return httpx.Response(503)
        if len(payloads) == 2:
            raise httpx.ConnectError('server restarting', request=request)
        return httpx.Response(200, json={})
    node, directory, lease = prepare_upload(tmp_path, handler)
    lease.thread.start()
    try:
        node._upload(directory, lease, 1024**2)
    finally:
        lease.close()
    assert len(payloads) == 3 and len(set(payloads)) == 1
    assert beats and lease.deadline - time.monotonic() <= 2.


def test_upload_reconciles_lost_response_after_server_committed(tmp_path):
    payloads, heartbeat_conflicts = [], []
    committed = threading.Event()
    def handler(request):
        if request.url.path.endswith('/heartbeat'):
            if committed.is_set():
                heartbeat_conflicts.append(True)
                return httpx.Response(409, json={'detail': 'already completed'})
            return httpx.Response(200, json={'expires_at': expires(), 'lease_seconds': 2., 'cancel_requested': False})
        assert request.url.path.endswith('/complete')
        payloads.append(archive_from_request(request))
        if len(payloads) == 1:
            committed.set()
            time.sleep(0.08)
            raise httpx.ReadError('successful response lost', request=request)
        return httpx.Response(200, json={'status': 'SUCCEEDED'})
    node, directory, lease = prepare_upload(tmp_path, handler)
    lease.thread.start()
    try:
        node._upload(directory, lease, 1024**2)
    finally:
        lease.close()
    assert heartbeat_conflicts
    assert len(payloads) == 2 and payloads[0] == payloads[1]


@pytest.mark.parametrize('status', [400, 401, 403, 409, 410, 422])
def test_upload_never_retries_permanent_rejection(tmp_path, status):
    calls = []
    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(status)
    node, directory, lease = prepare_upload(tmp_path, handler)
    with pytest.raises(WorkerError):
        node._upload(directory, lease, 1024**2)
    assert len(calls) == 1


@pytest.mark.parametrize('interruption', ['deadline', 'cancel', 'late_success'])
def test_upload_retries_never_outlive_lease_or_cancellation(tmp_path, interruption):
    uploads = []
    def handler(request):
        if request.url.path.endswith('/heartbeat'):
            if interruption == 'cancel' and uploads:
                return httpx.Response(200, json={'expires_at': expires(), 'lease_seconds': .08, 'cancel_requested': True})
            return httpx.Response(503)
        assert request.url.path.endswith('/complete')
        uploads.append(archive_from_request(request))
        if interruption == 'late_success':
            time.sleep(.12)
            return httpx.Response(200, json={})
        return httpx.Response(503)
    node, directory, lease = prepare_upload(tmp_path, handler, seconds=.08)
    lease.thread.start()
    try:
        with pytest.raises(WorkerError, match='USER_CANCELLED|LEASE_EXPIRED'):
            node._upload(directory, lease, 1024**2)
    finally:
        lease.close()
    assert len(uploads) == 1


def test_download_restarts_partial_transfer_after_transient_disconnect(tmp_path):
    from emsolver.remote_worker import _Lease
    calls = []
    data = b'm' * 1024**2 + b'atrix'
    class Disconnected(httpx.SyncByteStream):
        def __iter__(self):
            yield data[:1024**2]
            raise httpx.ReadError('connection interrupted')
    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, stream=Disconnected()) if len(calls) == 1 else httpx.Response(200, content=data)
    node = worker(tmp_path, handler)
    lease = _Lease(node, 'job-test', claim()['lease'])
    target = tmp_path / 'download.npz'
    node._download(claim(data)['input'], target, lease, 2 * 1024**2)
    assert target.read_bytes() == data and calls == ['/input', '/input']


@pytest.mark.parametrize('with_checkpoint', [False, True])
def test_real_scipy_run_uploads_only_numerical_bundle_and_strips_child_credentials(tmp_path, monkeypatch, with_checkpoint):
    source = io.BytesIO()
    sparse.save_npz(source, sparse.diags([-5., -1., 0., 2., 7., 9.]).tocsr())
    data = source.getvalue()
    assignment = claim(data)
    checkpoint_data = None
    if with_checkpoint:
        from emsolver.numerics.checkpoint import save_checkpoint
        assignment['job']['config']['solver']['method'] = 'block_shift_invert'
        snapshot = tmp_path / 'snapshot.npz'
        save_checkpoint(snapshot, sparse.diags([-5., -1., 0., 2., 7., 9.]).tocsr(),
                        assignment['job']['config'], np.eye(6, 3),
                        input_sha256=assignment['input']['sha256'])
        checkpoint_data = snapshot.read_bytes()
        assignment['checkpoint'] = {'kind': 'subspace', 'url': '/checkpoint',
                                    'sha256': hashlib.sha256(checkpoint_data).hexdigest()}
    uploads = []
    environments = []
    import subprocess
    actual_popen = subprocess.Popen
    def capture(*args, **kwargs):
        environments.append(kwargs['env'])
        return actual_popen(*args, **kwargs)
    monkeypatch.setattr(subprocess, 'Popen', capture)
    monkeypatch.setenv('EMSOLVER_WORKER_TOKEN', 'do-not-inherit')
    monkeypatch.setenv('AWS_SECRET_ACCESS_KEY', 'do-not-inherit')
    def handler(request):
        if request.url.path.endswith('/claim'):
            advertised = json.loads(request.content)['capabilities']
            assert advertised['backends'] == ['scipy']
            return httpx.Response(200, json=assignment)
        if request.url.path == '/input':
            return httpx.Response(200, content=data)
        if request.url.path == '/checkpoint':
            assert request.headers['X-EMSolver-Lease'] == 'lease-secret'
            return httpx.Response(200, content=checkpoint_data)
        if request.url.path.endswith('/heartbeat'):
            return httpx.Response(200, json={'expires_at': expires(), 'cancel_requested': False})
        if request.url.path.endswith('/complete'):
            body = request.read()
            start = body.index(b'PK\x03\x04')
            end = body.rindex(b'PK\x05\x06') + 22
            with zipfile.ZipFile(io.BytesIO(body[start:end])) as bundle:
                uploads.append({name: bundle.read(name) for name in bundle.namelist()})
            return httpx.Response(200, json={})
        pytest.fail(str(request.url) + ' ' + request.content.decode())
    assert worker(tmp_path, handler).run_once() is True
    assert environments and all('SECRET' not in key and 'TOKEN' not in key for key in environments[0])
    result = json.loads(uploads[0]['worker.result.json'])
    assert result['status'] == 'SUCCEEDED'
    assert result['eigenvalues'] == [0., -1.]
    assert {'eigenvalues.npy', 'eigenvectors.npy', 'manifest.json'} <= uploads[0].keys()
    assert not any('spec' in name or name.startswith('/') or '..' in name for name in uploads[0])
    assert not list((tmp_path / 'work').iterdir())
