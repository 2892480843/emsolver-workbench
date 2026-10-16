"""Exercise actual API authentication, transport and numerical publication."""
import hashlib
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from emsolver.api import create_app
from emsolver.cluster_protocol import execution_fingerprint
from emsolver.remote_worker import RemoteWorker


CAPACITY = {'threads': 2, 'memory_gib': 2, 'disk_gib': 2, 'devices': ['cpu']}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv('EMSOLVER_DATABASE_URL', raising=False)
    identities = tmp_path / 'identities.json'
    identities.write_text(json.dumps({'users': [
        {'subject': 'owner', 'token_sha256': hashlib.sha256(b'owner-test-token').hexdigest(), 'global_admin': True},
        {'subject': 'viewer', 'token_sha256': hashlib.sha256(b'viewer-test-token').hexdigest()},
    ]}))
    monkeypatch.setenv('EMSOLVER_IDENTITIES_FILE', str(identities))
    with TestClient(create_app(tmp_path / 'data', start_runner=False, execution_mode='cluster')) as value:
        value.headers['Authorization'] = 'Bearer owner-test-token'
        yield value


def prepare(client):
    service = client.app.state.service
    project = service.create_project('HTTP remote test')
    matrix = service.generate_sample(project['id'], n=12)
    plan = service.create_plan(config={'problem': {'matrix_id': matrix['id']},
        'target': {'k': 2, 'sigma': .1},
        'resources': {'threads': 1, 'memory_limit_gib': 1, 'disk_limit_gib': .05, 'walltime_seconds': 20}})
    job = service.submit_job(plan['id'])
    enrollment = client.post('/api/v1/cluster/workers', json={'name': 'node-a',
        'project_ids': [project['id']], 'resources': CAPACITY, 'backends': ['scipy']})
    assert enrollment.status_code == 201, enrollment.text
    return project, job, enrollment.json()


def claim(client, token):
    return client.post('/internal/v1/cluster/claim', headers={'Authorization': 'Bearer ' + token},
        json={'capabilities': {**CAPACITY, 'backends': ['scipy'], 'execution': execution_fingerprint()}})


def test_local_mode_exposes_honest_status_and_no_worker_entry(tmp_path):
    with TestClient(create_app(tmp_path, start_runner=False, execution_mode='local')) as client:
        assert client.get('/api/v1/cluster/status').json()['enabled'] is False
        assert client.post('/internal/v1/cluster/claim', json={'capabilities': {}}).status_code == 409


def test_business_and_machine_credentials_are_separate(client):
    _, _, enrollment = prepare(client)
    token = enrollment['token']
    assert claim(client, 'owner-test-token').status_code == 401
    assert client.get('/api/v1/projects', headers={'Authorization': 'Bearer ' + token}).status_code == 401
    assert claim(client, token).status_code == 200
    workers = client.get('/api/v1/cluster/workers').json()
    assert len(workers) == 1
    assert 'token' not in workers[0] and 'token_hash' not in workers[0]
    assert token not in json.dumps(client.app.state.service.audit())


def test_viewer_cannot_enroll_or_inspect_nodes(client):
    headers = {'Authorization': 'Bearer viewer-test-token'}
    assert client.get('/api/v1/cluster/status', headers=headers).status_code == 200
    assert client.get('/api/v1/cluster/workers', headers=headers).status_code == 403
    assert client.post('/api/v1/cluster/workers', headers=headers,
        json={'name': 'bad', 'project_ids': ['*'], 'resources': CAPACITY}).status_code == 403


def test_claim_download_heartbeat_and_revocation_are_lease_bound(client):
    _, job, enrollment = prepare(client)
    assignment = claim(client, enrollment['token']).json()
    assert assignment['job']['id'] == job['id']
    assert '_path' not in json.dumps(assignment)
    auth = {'Authorization': 'Bearer ' + enrollment['token'],
            'X-EMSolver-Lease': assignment['lease']['token'], 'X-EMSolver-Attempt': '1'}
    data = client.get(assignment['input']['url'], headers=auth)
    assert data.status_code == 200
    assert hashlib.sha256(data.content).hexdigest() == assignment['input']['sha256']
    assert client.get(assignment['input']['url'], headers={**auth, 'X-EMSolver-Lease': 'wrong'}).status_code == 409
    body = {'lease_token': assignment['lease']['token'], 'attempt': 1}
    response = client.post(f"/internal/v1/cluster/jobs/{job['id']}/heartbeat", headers=auth, json=body)
    assert response.status_code == 200 and response.json()['cancel_requested'] is False
    assert client.post('/api/v1/cluster/workers/' + enrollment['worker']['id'] + '/revoke').status_code == 200
    assert client.get(assignment['input']['url'], headers=auth).status_code == 401


def test_remote_worker_runs_real_scipy_through_all_http_routes(client, tmp_path):
    _, job, enrollment = prepare(client)

    def transport(request):
        response = client.request(request.method, request.url.raw_path.decode(),
            headers=dict(request.headers), content=request.read())
        return httpx.Response(response.status_code, headers=response.headers,
                              content=response.content, request=request)

    with httpx.Client(transport=httpx.MockTransport(transport)) as network:
        worker = RemoteWorker('http://127.0.0.1:18082', enrollment['token'], tmp_path / 'node',
            capabilities=CAPACITY, client=network, heartbeat_interval=.1)
        assert worker.run_once() is True
    finished = client.get('/api/v1/jobs/' + job['id']).json()
    assert finished['status'] == 'SUCCEEDED'
    assert finished['result']['validation']['target_certification'] == 'CERTIFIED'
    assert finished['metrics']['remote_worker'] is True
    assert client.get(f"/api/v1/jobs/{job['id']}/download", params={'file':'eigenvalues.npy'}).status_code == 200


def test_cluster_request_budget_applies_before_large_json_parsing(client):
    _, _, enrollment = prepare(client)
    response = client.post('/internal/v1/cluster/claim',
        headers={'Authorization': 'Bearer ' + enrollment['token'], 'Content-Type': 'application/json'},
        content=b'x' * 65537)
    assert response.status_code == 413


def test_version_conflicts_have_http_contract(client):
    project = client.app.state.service.create_project('Versioned')
    first = client.patch('/api/v1/projects/' + project['id'], json={'name': 'Updated', 'expected_version': 1})
    assert first.status_code == 200
    second = client.patch('/api/v1/projects/' + project['id'], json={'description': 'stale', 'expected_version': 1})
    assert second.status_code == 409 and second.json()['error']['code'] == 'VERSION_CONFLICT'


def test_complete_rejects_missing_lease_before_multipart_parsing(client, monkeypatch):
    from starlette.formparsers import MultiPartParser
    _, job, enrollment = prepare(client)

    async def forbidden_parse(self):
        raise AssertionError('An unauthorised submission must not spool its body')

    monkeypatch.setattr(MultiPartParser, 'parse', forbidden_parse)
    response = client.post(f"/internal/v1/cluster/jobs/{job['id']}/complete",
        headers={'Authorization': 'Bearer ' + enrollment['token']},
        files={'bundle': ('result.zip', b'not-read')})
    assert response.status_code == 422
    assert response.json()['error']['code'] == 'LEASE_HEADERS_REQUIRED'


def test_busy_intake_rejects_before_multipart_parsing(client, monkeypatch):
    from emsolver.cluster_admission import acquire_admission
    from starlette.formparsers import MultiPartParser
    _, job, enrollment = prepare(client)
    assignment = claim(client, enrollment['token']).json()
    guard = acquire_admission(client.app.state.service, enrollment['worker']['id'], job['id'], 1)

    async def forbidden_parse(self):
        raise AssertionError('A duplicate upload must not spool its body')

    monkeypatch.setattr(MultiPartParser, 'parse', forbidden_parse)
    try:
        response = client.post(f"/internal/v1/cluster/jobs/{job['id']}/complete",
            headers={'Authorization': 'Bearer ' + enrollment['token'],
                'X-EMSolver-Lease': assignment['lease']['token'], 'X-EMSolver-Attempt': '1'},
            files={'bundle': ('result.zip', b'not-read')})
        assert response.status_code == 503
        assert response.json()['error']['code'] == 'CLUSTER_INTAKE_BUSY'
    finally:
        guard.close()


def test_streamed_request_budget_cannot_be_bypassed_without_content_length(client):
    _, _, enrollment = prepare(client)
    response = client.post('/internal/v1/cluster/claim',
        headers={'Authorization': 'Bearer ' + enrollment['token'], 'Content-Type': 'application/json'},
        content=iter([b'x' * 32768, b'x' * 32769]))
    assert 'Content-Length' not in response.request.headers
    assert response.status_code == 413
