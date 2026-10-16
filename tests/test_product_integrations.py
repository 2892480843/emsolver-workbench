from pathlib import Path

from fastapi.testclient import TestClient
from emsolver.api import create_app


def test_knowledge_rules_effects_provenance_and_triangular_derivation(tmp_path):
    with TestClient(create_app(tmp_path,start_runner=False)) as client:
        service=client.app.state.service
        project=service.create_project('Integration')
        matrix=service.generate_sample(project['id'],n=30)
        plan=service.create_plan(config={'problem':{'matrix_id':matrix['id']},'target':{'k':3},'resources':{'threads':1}})
        job=service.submit_job(plan['id']);service._execute(job['id'])
        assert service.get_job(job['id'])['runtime']['provenance']['source']['tree_sha256']
        knowledge=client.get('/api/v1/knowledge').json()
        assert len(knowledge)>=5 and knowledge[0]['version']
        explained=client.post('/api/v1/diagnostics/explain',json={'job_id':job['id']}).json()
        assert explained['source']=='rules' and explained['explanation']['fallback_reason']=='not_configured'
        derived=client.post('/api/v1/matrices/'+matrix['id']+'/derive',json={'triangle':'upper'})
        assert derived.status_code==201,derived.text
        assert derived.json()['derivation']['source_hash']==matrix['hash']
        assert derived.json()['storage']=='upper'
        advice=client.post('/api/v1/configs/advice',json={'job_id':job['id'],'changes':{'solver':{'subspace_max':60}}}).json()
        assert advice['effect_id']
        next_plan=service.create_plan(config_id=advice['id']);next_job=service.submit_job(next_plan['id'])
        link=client.post('/api/v1/advice/effects/'+advice['effect_id']+'/link',json={'job_id':next_job['id']})
        assert link.status_code==200,link.text
        effect=client.get('/api/v1/advice/effects/'+advice['effect_id']).json()
        assert not effect['evaluation']['comparable']
        assert effect['evaluation']['after']['status']=='QUEUED'
        rows=client.get('/api/v1/advice/effects',params={'source_job_id':job['id']}).json()
        assert any(row['record_type']=='linked' for row in rows)
        backup=service.backup();service.restore_backup(backup,tmp_path.parent/(tmp_path.name+'-restored'))


def test_project_priority_fifo_and_new_backend_plan(tmp_path):
    with TestClient(create_app(tmp_path,start_runner=False)) as client:
        service=client.app.state.service
        first=service.create_project('First');second=service.create_project('Second')
        def submit(project):
            matrix=service.generate_sample(project['id'],n=30)
            plan=service.create_plan(config={'problem':{'matrix_id':matrix['id'],'reordering':'reverse_cuthill_mckee'},'target':{'k':3},'solver':{'method':'block_shift_invert','subspace_max':10},'resources':{'threads':1}})
            assert plan['strategy']['actual_method']=='block_shift_invert'
            assert 'components' in plan['estimate']
            return service.submit_job(plan['id'])
        a=submit(first);b=submit(second)
        service.update_project(second['id'],policy={'priority':3})
        assert service.jobs.next_queued()['id']==b['id']
        service.cancel_job(b['id']);assert service.jobs.next_queued()['id']==a['id']


def test_checkpoint_restart_uses_new_job_and_rejects_tampered_snapshot(tmp_path):
    from emsolver.errors import ServiceError
    import pytest
    with TestClient(create_app(tmp_path,start_runner=False)) as client:
        service=client.app.state.service
        project=service.create_project('Checkpoint')
        matrix=service.generate_sample(project['id'],n=30)
        plan=service.create_plan(config={'problem':{'matrix_id':matrix['id']},'target':{'k':3},'solver':{'method':'block_shift_invert','subspace_max':10},'resources':{'threads':1}})
        job=service.submit_job(plan['id']);service._execute(job['id'])
        response=client.post('/api/v1/jobs/'+job['id']+'/restart',json={'use_checkpoint':True})
        assert response.status_code==201,response.text
        restarted=response.json()
        assert restarted['id']!=job['id'] and restarted['resumption']['exact_resume'] is False
        assert restarted['resumption']['source_job_id']==job['id']
        service._execute(restarted['id'])
        assert service.get_job(restarted['id'])['metrics']['warm_start'] is True
        path=tmp_path/'runs'/job['id']/'artifacts'/'subspace.checkpoint.npz'
        path.write_bytes(b'tampered')
        with pytest.raises(ServiceError):service.restart_job(job['id'],use_checkpoint=True)


def test_backup_missing_payload_requires_explicit_degraded_restore_and_pending_import_is_safe(tmp_path):
    import zipfile
    import pytest
    from emsolver.service import Service
    from emsolver.errors import ServiceError
    service=Service(tmp_path/'source',start_runner=False)
    project=service.create_project('Restore')
    matrix=service.generate_sample(project['id'],n=30)
    upload=service.root/'inbox'/'upload.npz'
    upload.write_bytes(Path(service.store.get('matrices',matrix['id'])['_path']).read_bytes())
    task=service.import_manager.submit(project['id'],upload,'matrix.npz')
    backup=service.backup()
    damaged=tmp_path/'missing.zip'
    with zipfile.ZipFile(backup) as source,zipfile.ZipFile(damaged,'w') as dest:
        for name in source.namelist():
            if not name.startswith('matrices/'):dest.writestr(name,source.read(name))
    with pytest.raises(ServiceError):service.restore_backup(damaged,tmp_path/'strict')
    result=service.restore_backup(damaged,tmp_path/'degraded',allow_missing=True)
    assert result['missing_files'] and result['degraded']
    restored=Service(tmp_path/'degraded',start_runner=False)
    assert restored.get_matrix(matrix['id'])['availability']=='MISSING'
    with pytest.raises(ServiceError) as failure:restored.create_plan(config={'problem':{'matrix_id':matrix['id']}})
    assert failure.value.code=='INPUT_MISSING'
    import_record=restored.store.get('imports',task['id'])
    assert import_record['status']=='FAILED' and import_record['error']['code']=='SOURCE_NOT_BACKED_UP'
    assert not any(key.startswith('_') for key in import_record)
    assert restored.store.get('project_storage',project['id']) is None
    restored.close();service.close()


def test_effect_preview_read_never_appends_and_record_is_idempotent(tmp_path):
    from emsolver.service import Service
    service=Service(tmp_path,start_runner=False)
    project=service.create_project('Advice')
    matrix=service.generate_sample(project['id'],n=30)
    original=service.submit_job(service.create_plan(config={'problem':{'matrix_id':matrix['id']},'target':{'k':3}})['id'])
    changed=service.apply_advice(job_id=original['id'],changes={'solver':{'subspace_max':60}})
    new=service.submit_job(service.create_plan(config_id=changed['id'])['id'])
    service.link_advice_effect(changed['effect_id'],new['id'])
    count=service.store.count('advice_effects')
    for _ in range(3):assert service.advice_effect(changed['effect_id'])['evaluation']['record_type']=='preview'
    assert service.store.count('advice_effects')==count
    first=service.evaluate_advice_effect(changed['effect_id'])
    assert service.evaluate_advice_effect(changed['effect_id'])['id']==first['id']
    assert service.store.count('advice_effects')==count+1
    service.close()


def test_restart_reconciles_only_hash_checked_published_results(tmp_path):
    from emsolver.service import Service
    service=Service(tmp_path,start_runner=False)
    project=service.create_project('Reconcile')
    matrix=service.generate_sample(project['id'],n=30)
    job=service.submit_job(service.create_plan(config={'problem':{'matrix_id':matrix['id']},'target':{'k':3},'resources':{'threads':1}})['id'])
    service._execute(job['id']);done=service.get_job(job['id'])
    # Model a crash after manifest publication but before the final DB commit.
    job.update(status='RUNNING',started_at=done['started_at'],stage='EXPORTING')
    service.store.put('jobs',job['id'],job)
    service.close()
    service=Service(tmp_path,start_runner=True)
    restored=service.get_job(job['id'])
    assert restored['status']==done['status']
    assert restored['metrics']['reconciled_published_run']
    assert restored['metrics']['recovery_observation_gap']
    service.close()


def test_recovered_event_cursor_does_not_replay_worker_records(tmp_path):
    from emsolver.service import Service
    from emsolver.events import WorkerEventReader
    service=Service(tmp_path,start_runner=False)
    project=service.create_project('Cursor')
    matrix=service.generate_sample(project['id'],n=30)
    job=service.submit_job(service.create_plan(config={'problem':{'matrix_id':matrix['id']},'target':{'k':3}})['id'])
    run=tmp_path/'runs'/job['id'];run.mkdir()
    path=run/'worker.events.jsonl';path.write_text('{"event":"ONE"}\n')
    service._ingest_worker_events(job['id'],WorkerEventReader(path))
    cursor=service.get_job(job['id'])['worker_event_offset']
    with path.open('a') as handle:handle.write('{"event":"TWO"}\n')
    service._ingest_worker_events(job['id'],WorkerEventReader(path,offset=cursor))
    assert [event['event'] for event in service.events(job['id'])]==['QUEUED','ONE','TWO']
    service.close()


def test_worker_event_reader_rewinds_after_database_rollback(tmp_path,monkeypatch):
    import pytest
    from emsolver.service import Service
    from emsolver.events import WorkerEventReader
    service=Service(tmp_path,start_runner=False)
    project=service.create_project('Rollback')
    matrix=service.generate_sample(project['id'],n=30)
    job=service.submit_job(service.create_plan(config={'problem':{'matrix_id':matrix['id']},'target':{'k':3}})['id'])
    run=tmp_path/'runs'/job['id'];run.mkdir()
    path=run/'worker.events.jsonl';path.write_text('{"event":"RECOVERABLE_EVENT"}\n')
    reader=WorkerEventReader(path);original=service.store.put
    def fail(kind,*args,**kwargs):
        if kind=='jobs':raise OSError('Injected DB failure')
        return original(kind,*args,**kwargs)
    with monkeypatch.context() as fault:
        fault.setattr(service.store,'put',fail)
        with pytest.raises(OSError):service._ingest_worker_events(job['id'],reader)
    assert reader.offset==0
    service._ingest_worker_events(job['id'],reader)
    assert [event['event'] for event in service.events(job['id'])]==['QUEUED','RECOVERABLE_EVENT']
    service.close()


def test_backup_serializes_retention_with_snapshot(tmp_path,monkeypatch):
    import threading,time,zipfile
    from emsolver.service import Service
    service=Service(tmp_path/'source',start_runner=False)
    project=service.create_project('Atomic backup');matrix=service.generate_sample(project['id'],n=30)
    job=service.submit_job(service.create_plan(config={'problem':{'matrix_id':matrix['id']},'target':{'k':3},'resources':{'threads':1}})['id'])
    service._execute(job['id']);job=service.get_job(job['id']);job.update(retained=False,finished_at='2000-01-01T00:00:00+00:00');service.store.put('jobs',job['id'],job)
    ready=threading.Event();attempted=threading.Event();errors=[]
    def sweep():
        ready.wait(5);attempted.set()
        try:service.lifecycle_cleanup(False)
        except BaseException as exc:errors.append(exc)
    thread=threading.Thread(target=sweep);thread.start()
    original=zipfile.ZipFile
    def pause(*args,**kwargs):
        if len(args)>1 and args[1]=='w':ready.set();assert attempted.wait(5)
        return original(*args,**kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(zipfile,'ZipFile',pause)
        archive=service.backup()
    thread.join(5);assert not thread.is_alive() and not errors
    restored=service.restore_backup(archive,tmp_path/'restored')
    assert not restored['degraded']
    assert (tmp_path/'restored'/'runs'/job['id']/'artifacts'/'eigenvectors.npy').is_file()
    service.close()


def test_reconcile_recomputes_original_residual_after_manifest_hash_laundering(tmp_path):
    import json,numpy as np
    from emsolver.service import Service
    from emsolver.integrity import file_hash
    service=Service(tmp_path,start_runner=False)
    project=service.create_project('Revalidation')
    matrix=service.generate_sample(project['id'],n=30)
    job=service.submit_job(service.create_plan(config={'problem':{'matrix_id':matrix['id']},'target':{'k':3},'resources':{'threads':1}})['id'])
    service._execute(job['id']);finished=service.get_job(job['id'])
    folder=tmp_path/'runs'/job['id']/'artifacts'
    vectors=np.load(folder/'eigenvectors.npy');vectors[:,0]=0;np.save(folder/'eigenvectors.npy',vectors)
    manifest=json.loads((folder/'manifest.json').read_text());manifest['artifacts']['eigenvectors.npy']=file_hash(folder/'eigenvectors.npy')
    (folder/'manifest.json').write_text(json.dumps(manifest))
    job.update(status='RUNNING',stage='exporting',started_at=finished['started_at']);service.store.put('jobs',job['id'],job)
    service.close();service=Service(tmp_path,start_runner=True)
    recovered=service.get_job(job['id'])
    assert recovered['status']=='FAILED' and recovered.get('result') is None
    assert not recovered['metrics'].get('reconciled_published_run')
    service.close()
