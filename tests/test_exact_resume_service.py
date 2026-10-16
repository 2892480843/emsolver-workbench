import json
import numpy as np
import pytest
from fastapi.testclient import TestClient
from scipy import sparse
from emsolver.api import create_app
from emsolver.numerics import run_solver


def interrupted_job(service, tmp_path):
    project=service.create_project('Exact block recovery')
    n=40
    matrix_path=service.root/'inbox/input.npz'
    sparse.save_npz(matrix_path,sparse.diags([np.full(n-1,.1),np.linspace(-2.,5.,n),np.full(n-1,.1)],[-1,0,1],format='csr'))
    matrix=service.import_matrix(project['id'],matrix_path)
    plan=service.create_plan(config={'problem':{'matrix_id':matrix['id']},'target':{'k':2,'sigma':.15},
          'solver':{'method':'block_shift_invert','subspace_max':14,'max_outer_iterations':200,
                    'adaptive_shift':{'enabled':False},'block':{'checkpoint_interval':1,'checkpoint_kind':'exact_block'}},
          'resources':{'threads':1}})
    job=service.submit_job(plan['id']);events=[]
    input_path=service.store.get('matrices',matrix['id'])['_path']
    with pytest.raises(InterruptedError):
        run_solver(input_path,job['config'],service.root/'runs'/job['id']/'artifacts',event_callback=events.append,
                   cancel_check=lambda:any(e['event']=='checkpoint_written' and e.get('iteration')==3 for e in events))
    service.cancel_job(job['id'])
    return job,input_path


def test_exact_resume_api_runs_actual_worker_and_preserves_original(tmp_path):
    with TestClient(create_app(tmp_path/'data',start_runner=False)) as client:
        service=client.app.state.service;job,input_path=interrupted_job(service,tmp_path)
        response=client.post('/api/v1/jobs/'+job['id']+'/restart',json={'use_checkpoint':True,'resume_mode':'exact_block'})
        assert response.status_code==201,response.text
        resumed=response.json()
        assert resumed['resumption']['exact_resume'] is True and resumed['parent_job_id']==job['id']
        service._execute(resumed['id'])
        actual=service.get_job(resumed['id'])
        assert actual['status'] in {'SUCCEEDED','PARTIAL'},actual
        assert actual['metrics']['exact_resume'] is True
        assert actual['metrics']['restored_outer_iterations']==3
        baseline=run_solver(input_path,job['config'],tmp_path/'baseline')
        assert actual['result']['eigenvalues']==baseline['eigenvalues']
        assert np.array_equal(np.load(service.root/'runs'/resumed['id']/'artifacts/eigenvectors.npy'),np.load(tmp_path/'baseline/eigenvectors.npy'))
        assert actual['metrics']['factorizations']==baseline['metrics']['factorizations']+1
        assert service.get_job(job['id'])['status']=='CANCELLED'
        assert client.post('/api/v1/jobs/'+job['id']+'/restart',json={'resume_mode':'exact_block'}).status_code==400


def test_exact_resume_queued_snapshot_replacement_is_refused(tmp_path):
    with TestClient(create_app(tmp_path/'data',start_runner=False)) as client:
        service=client.app.state.service;job,_=interrupted_job(service,tmp_path)
        resumed=service.restart_job(job['id'],use_checkpoint=True,resume_mode='exact_block')
        snapshot=service.root/'runs'/job['id']/'artifacts/exact-block.checkpoint.npz'
        snapshot.write_bytes(b'tampered after queueing')
        service._execute(resumed['id'])
        assert service.get_job(resumed['id'])['error']['code']=='CHECKPOINT_INCOMPATIBLE'
        response=client.post('/api/v1/jobs/'+job['id']+'/restart',json={'use_checkpoint':True,'resume_mode':'exact_block'})
        assert response.status_code==409
