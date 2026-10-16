import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from emsolver.api import create_app
from emsolver.service import Service, ServiceError


def test_project_policy_usage_queue_budget_and_search(tmp_path):
    service=Service(tmp_path,start_runner=False)
    project=service.create_project('Wave experiment')
    service.update_project(project['id'],classification='confidential',policy={'storage_limit_gib':1,'max_queued_jobs':1,'priority':4,'retention_days':7})
    assert service.list_projects('wave')[0]['id']==project['id']
    assert service.list_projects('absent')==[]
    matrix=service.generate_sample(project['id'],n=20)
    plan=service.create_plan(config={'problem':{'matrix_id':matrix['id']},'target':{'k':2},'resources':{'threads':1,'disk_limit_gib':.1}})
    service.submit_job(plan['id'])
    with pytest.raises(ServiceError) as exc:service.submit_job(plan['id'])
    assert exc.value.code=='PROJECT_QUEUE_LIMIT'
    usage=service.project_usage(project['id'])
    assert usage['input_bytes']>0 and usage['reserved_output_bytes']==pytest.approx(.1*1024**3)
    assert usage['queued_jobs']==1
    service.close()


def test_project_roles_filter_data_and_audit_denied_calls(tmp_path,monkeypatch):
    accounts=tmp_path/'accounts.json'
    accounts.write_text(json.dumps({'users':[{'subject':name,'token_sha256':hashlib.sha256(token.encode()).hexdigest(),'global_admin':admin}
        for name,token,admin in [('owner','owner-secret',True),('reader','reader-secret',False),('other','other-secret',False)]]}))
    monkeypatch.setenv('EMSOLVER_IDENTITIES_FILE',str(accounts))
    with TestClient(create_app(tmp_path/'data',start_runner=False)) as client:
        owner={'Authorization':'Bearer owner-secret'};reader={'Authorization':'Bearer reader-secret'};other={'Authorization':'Bearer other-secret'}
        project=client.post('/api/v1/projects',json={'name':'Private'},headers=owner).json()
        response=client.patch('/api/v1/projects/'+project['id'],json={'members':[{'subject':'reader','role':'viewer'}]},headers=owner)
        assert response.status_code==200,response.text
        matrix=client.post('/api/v1/samples',json={'project_id':project['id'],'n':20},headers=owner).json()
        assert client.get('/api/v1/projects',headers=other).json()==[]
        assert client.get('/api/v1/matrices/'+matrix['id'],headers=other).status_code==403
        assert client.get('/api/v1/matrices/'+matrix['id'],headers=reader).status_code==200
        assert client.post('/api/v1/configs',json={'config':{'problem':{'matrix_id':matrix['id']}}},headers=reader).status_code==403
        assert client.post('/api/v1/backup',headers=reader).status_code==403
        assert client.get('/api/v1/projects',headers=reader).json()[0]['id']==project['id']
        assert client.get('/api/v1/identity',headers=reader).json()['subject']=='reader'
        assert client.get('/api/v1/projects').status_code==401
        audit=client.get('/api/v1/audit',headers=owner).json()
        assert any(row['actor']=='other' and row['result']=='denied' for row in audit)
        assert all('secret' not in json.dumps(row) for row in audit)


def test_quota_rejects_atomic_submission_and_keeps_no_job(tmp_path):
    service=Service(tmp_path,start_runner=False);project=service.create_project('Quota')
    matrix=service.generate_sample(project['id'],n=20)
    service.update_project(project['id'],policy={'storage_limit_gib':.001})
    plan=service.create_plan(config={'problem':{'matrix_id':matrix['id']},'target':{'k':2},'resources':{'threads':1,'disk_limit_gib':1}})
    with pytest.raises(ServiceError) as failure:service.submit_job(plan['id'])
    assert failure.value.code=='PROJECT_STORAGE_QUOTA'
    assert service.store.count('jobs')==0
    service.close()
