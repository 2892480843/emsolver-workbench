from fastapi.testclient import TestClient
from emsolver.api import create_app


def test_api_auth_all_routes_and_strict_payload(tmp_path,monkeypatch):
    monkeypatch.setenv('EMSOLVER_API_TOKEN','private-test-token')
    with TestClient(create_app(tmp_path,start_runner=False)) as client:
        assert client.get('/api/v1/health').status_code==401
        assert client.post('/api/v1/backup').status_code==401
        client.headers['Authorization']='Bearer private-test-token'
        assert client.get('/api/v1/health').status_code==200
        assert client.post('/api/v1/projects',json={'name':'p','unknown':True}).status_code==422
        project=client.post('/api/v1/projects',json={'name':'p'}).json()
        assert client.get('/api/v1/projects').json()==[project]
        assert client.patch('/api/v1/projects/'+project['id'],json={'archived':True}).status_code==200
        assert len(client.get('/api/v1/coverage').json())==66


def test_errors_do_not_expose_local_paths(tmp_path):
    with TestClient(create_app(tmp_path,start_runner=False)) as client:
        response=client.get('/api/v1/jobs/nonexistent')
        assert response.status_code==404
        assert response.json()['error']['code']=='NOT_FOUND'
        assert str(tmp_path) not in response.text
        response=client.post('/api/v1/matrices/import',json={'project_id':'p','path':'/etc/passwd'})
        assert response.status_code==400
        assert '/etc/passwd' not in response.text


def test_api_real_sample_config_plan_job_and_binary_security(tmp_path):
    import io,numpy as np
    with TestClient(create_app(tmp_path,start_runner=False)) as client:
        project=client.post('/api/v1/projects',json={'name':'API experiment'}).json()
        sample=client.post('/api/v1/samples',json={'project_id':project['id'],'kind':'repeated','n':40})
        assert sample.status_code==201,sample.text
        matrix=sample.json()
        assert str(tmp_path) not in sample.text
        body={'problem':{'matrix_id':matrix['id']},'target':{'k':4,'sigma':0.1},'resources':{'threads':1}}
        invalid={**body,'unknown':'reject'}
        assert client.post('/api/v1/configs',json={'config':invalid}).status_code==400
        frozen=client.post('/api/v1/configs',json={'config':body}).json()
        plan=client.post('/api/v1/plans',json={'config_id':frozen['id']}).json()
        submitted=client.post('/api/v1/jobs',json={'plan_id':plan['id'],'idempotency_key':'http-retry'})
        assert submitted.status_code==201,submitted.text
        job=submitted.json()
        assert client.post('/api/v1/jobs',json={'plan_id':plan['id'],'idempotency_key':'http-retry'}).json()['id']==job['id']
        assert client.get('/api/v1/jobs/'+job['id']+'/download',params={'file':'../../metadata.sqlite3'}).status_code==403
        assert client.post('/api/v1/jobs/'+job['id']+'/cancel').json()['status']=='CANCELLED'
        assert client.get('/api/v1/jobs/'+job['id']+'/events',params={'after':1}).json()[0]['seq']==2
        assert client.post('/api/v1/matrices/'+matrix['id']+'/archive').status_code==200
        assert client.post('/api/v1/jobs',json={'plan_id':plan['id']}).status_code==409
        data=io.BytesIO(); np.save(data,np.array([{'object':'unsafe'}],dtype=object))
        upload=client.post('/api/v1/matrices',data={'project_id':project['id']},files={'file':('unsafe.npy',data.getvalue(),'application/octet-stream')})
        assert upload.status_code==400
        assert len(client.get('/api/v1/matrices').json())==1
