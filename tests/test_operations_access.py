import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from emsolver.access import authenticate, configured_users
from emsolver.api import create_app
from emsolver.errors import ServiceError


def accounts(path):
    users = [{'subject': subject, 'token_sha256': hashlib.sha256(token.encode()).hexdigest(),
              **flags} for subject, token, flags in [
                  ('owner', 'owner-key', {'global_admin': True}),
                  ('operator', 'operator-key', {'operations_admin': True}),
                  ('reader', 'reader-key', {}),
              ]]
    path.write_text(json.dumps({'users': users}))
    return users


def test_operations_identity_is_explicit_strict_and_defaults_to_no_extra_privilege(tmp_path, monkeypatch):
    path = tmp_path / 'identities.json'
    users = accounts(path)
    monkeypatch.setenv('EMSOLVER_IDENTITIES_FILE', str(path))
    assert authenticate('Bearer operator-key').operations_admin is True
    assert authenticate('Bearer operator-key').global_admin is False
    assert authenticate('Bearer reader-key').operations_admin is False
    assert authenticate('Bearer owner-key').global_admin is True
    for invalid in ['true', 1, 0, None]:
        users[1]['operations_admin'] = invalid
        path.write_text(json.dumps({'users': users}))
        with pytest.raises(ServiceError, match='Identity configuration'):
            configured_users()
    users[1]['operations_admin'] = True
    users[1]['unknown_role'] = True
    path.write_text(json.dumps({'users': users}))
    with pytest.raises(ServiceError):
        configured_users()


def test_operations_admin_has_only_redacted_operations_access_until_project_membership(tmp_path, monkeypatch):
    path = tmp_path / 'identities.json'
    accounts(path)
    monkeypatch.setenv('EMSOLVER_IDENTITIES_FILE', str(path))
    owner = {'Authorization': 'Bearer owner-key'}
    operator = {'Authorization': 'Bearer operator-key'}
    reader = {'Authorization': 'Bearer reader-key'}
    with TestClient(create_app(tmp_path / 'data', start_runner=False)) as client:
        project = client.post('/api/v1/projects', json={'name': 'confidential-design-name', 'description': 'private-design-description'}, headers=owner).json()
        matrix = client.post('/api/v1/samples', json={'project_id': project['id'], 'n': 20}, headers=owner).json()
        config = client.post('/api/v1/configs', json={'config': {'problem': {'matrix_id': matrix['id']}, 'target': {'k': 2}}}, headers=owner).json()
        plan = client.post('/api/v1/plans', json={'config_id': config['id']}, headers=owner).json()
        job = client.post('/api/v1/jobs', json={'plan_id': plan['id']}, headers=owner).json()
        service = client.app.state.service
        service._finish(job['id'], 'FAILED', 'WORKER_LOST')
        artifact = service.root / 'runs' / job['id'] / 'artifacts' / 'metrics.json'
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text('{"private-business-value": 7}')
        report = client.post('/api/v1/reports', json={'job_id': job['id']}, headers=owner).json()
        identity = client.get('/api/v1/identity', headers=operator).json()
        assert identity['operations_admin'] is True and identity['global_admin'] is False
        assert identity['configured_subjects'] == ['operator']
        assert client.get('/api/v1/projects', headers=operator).json() == []
        assert client.get('/api/v1/matrices', headers=operator).json() == []
        for route in [f'/projects/{project["id"]}/usage', f'/matrices/{matrix["id"]}', f'/jobs/{job["id"]}',
                      f'/jobs/{job["id"]}/results', f'/jobs/{job["id"]}/download?file=metrics.json',
                      f'/jobs/{job["id"]}/events', f'/jobs/{job["id"]}/bundle', f'/reports/{report["id"]}']:
            assert client.get('/api/v1' + route, headers=operator).status_code == 403, route
        assert client.post('/api/v1/projects', json={'name': 'self-grant'}, headers=operator).status_code == 403
        assert client.post('/api/v1/backup', headers=operator).status_code == 403
        assert client.post('/api/v1/backup', headers=owner).status_code == 200
        assert client.patch('/api/v1/projects/' + project['id'], json={'members': [{'subject': 'operator', 'role': 'admin'}]}, headers=operator).status_code == 403
        for route in ['/operations/summary', '/operations/project-policies']:
            assert client.get('/api/v1' + route, headers=reader).status_code == 403
            response = client.get('/api/v1' + route, headers=operator)
            assert response.status_code == 200, response.text
            assert 'confidential-design-name' not in response.text
            assert 'private-design-description' not in response.text
            assert matrix['hash'] not in response.text
            assert 'private-business-value' not in response.text
        page = client.get('/api/v1/operations/project-policies', headers=operator).json()
        assert set(page['items'][0]) == {'project_id', 'policy', 'usage'}
        assert page['items'][0]['project_id'] == project['id']
        policy = {'project_id': project['id'], 'policy': {'priority': 3, 'retention_days': 8}}
        assert client.patch('/api/v1/operations/project-policies', json=policy, headers=reader).status_code == 403
        updated = client.patch('/api/v1/operations/project-policies', json=policy, headers=operator)
        assert updated.status_code == 200, updated.text
        assert updated.json()['policy']['priority'] == 3
        assert 'name' not in updated.json()
        assert client.patch('/api/v1/operations/project-policies', json={**policy, 'name': 'leak'}, headers=operator).status_code == 422
        assert client.patch('/api/v1/operations/project-policies', json={**policy, 'policy': {'priority': True}}, headers=operator).status_code == 400
        assert client.get('/api/v1/projects', headers=operator).json() == []
        client.patch('/api/v1/projects/' + project['id'], json={'members': [{'subject': 'operator', 'role': 'viewer'}]}, headers=owner)
        assert client.get('/api/v1/matrices/' + matrix['id'], headers=operator).status_code == 200
        assert client.get('/api/v1/jobs/' + job['id'] + '/download?file=metrics.json', headers=operator).status_code == 200
        assert client.get('/api/v1/reports/' + report['id'], headers=operator).status_code == 200
        assert client.post('/api/v1/samples', json={'project_id': project['id'], 'n': 20}, headers=operator).status_code == 403
        assert client.post('/api/v1/backup', headers=operator).status_code == 403
        audit = client.get('/api/v1/audit', headers=owner).json()
        assert any(row['action'] == 'operations.policy.update' and row['actor'] == 'operator' for row in audit)
        assert any(row['result'] == 'denied' and row['actor'] == 'operator' for row in audit)
