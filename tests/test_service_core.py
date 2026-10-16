from pathlib import Path
import pytest
from emsolver.service import Service, ServiceError


def test_project_archive_blocks_plan_and_config_is_immutable(tmp_path):
    service = Service(tmp_path, start_runner=False)
    project = service.create_project('A')
    service.update_project(project['id'], archived=True)
    assert service.list_projects()[0]['archived'] is True
    with pytest.raises(ServiceError) as exc:
        service.generate_sample(project['id'], n=20)
    assert exc.value.code == 'PROJECT_ARCHIVED'
    service.close()


def test_recovery_and_reference_integrity(tmp_path):
    service = Service(tmp_path, start_runner=False)
    service.store.put('jobs', 'lost', {'id':'lost','status':'RUNNING','stage':'solve','created_at':'now'})
    service.close()
    service = Service(tmp_path, start_runner=True)
    assert service.get_job('lost')['error']['code'] == 'WORKER_LOST'
    with pytest.raises(ServiceError) as exc:
        service.get_matrix('missing')
    assert exc.value.status == 404
    service.close()


def test_compare_rejects_unfair_speedup(tmp_path):
    service=Service(tmp_path,start_runner=False)
    for index in range(2):
        service.store.put('jobs',str(index),{'id':str(index),'status':'PARTIAL','matrix_hash':'same','config':{'resources':{'threads':index+1},'validation':{'residual_scaled_max':1e-8}},'runtime':{},'metrics':{'walltime_seconds':index+1}})
    comparison=service.compare_jobs(['0','1'])
    assert comparison['comparable'] is False
    assert comparison['speedup'] is None
    service.close()


def prepared(service):
    project=service.create_project('Integration')
    matrix=service.generate_sample(project['id'],n=40)
    config={'problem':{'matrix_id':matrix['id']},'target':{'k':4,'sigma':0.1},'resources':{'threads':1,'walltime_seconds':20}}
    plan=service.create_plan(config=config)
    return project,matrix,plan


def test_real_matrix_plan_idempotency_and_staleness(tmp_path):
    service=Service(tmp_path,start_runner=False)
    project,matrix,plan=prepared(service)
    assert 'preview' in matrix['diagnostics']
    assert str(tmp_path) not in str(matrix)
    job=service.submit_job(plan['id'],'network-retry')
    assert service.submit_job(plan['id'],'network-retry')['id']==job['id']
    plan2=service.create_plan(config=plan['config'])
    with pytest.raises(ServiceError) as exc:
        service.submit_job(plan2['id'],'network-retry')
    assert exc.value.code=='IDEMPOTENCY_CONFLICT'
    asset=service.store.get('matrices',matrix['id'])
    path=Path(asset['_path']); path.chmod(0o600); path.write_bytes(b'changed')
    with pytest.raises(ServiceError) as exc:
        service.submit_job(plan2['id'])
    assert exc.value.code=='STALE_PLAN'
    # The queued request keeps the immutable value even when caller mutates a response.
    plan['config']['target']['k']=7
    assert service.get_job(job['id'])['config']['target']['k']==4
    service.close()


def test_real_subprocess_results_events_reports_and_restart(tmp_path):
    import time, zipfile
    service=Service(tmp_path)
    _,matrix,plan=prepared(service)
    job=service.submit_job(plan['id'])
    deadline=time.monotonic()+25
    while service.get_job(job['id'])['status'] not in {'SUCCEEDED','PARTIAL','FAILED'} and time.monotonic()<deadline:
        time.sleep(.05)
    done=service.get_job(job['id'])
    assert done['status'] in {'SUCCEEDED','PARTIAL'},done
    assert done['worker_pid'] != __import__('os').getpid()
    assert done['metrics']['peak_rss_tree_bytes']>0
    assert len(service.results(job['id'])['eigenvalues'])==4
    events=service.events(job['id'])
    assert [e['seq'] for e in events]==list(range(1,len(events)+1))
    assert service.events(job['id'],events[-1]['seq'])==[]
    assert sum(e['event'] in {'SUCCEEDED','PARTIAL','FAILED'} for e in events)==1
    assert service.download(job['id'],'eigenvalues.npy').is_file()
    with pytest.raises(ServiceError): service.download(job['id'],'../../metadata.sqlite3')
    report=service.create_report(job['id'])
    assert service.report_path(report['id']).is_file()
    with zipfile.ZipFile(service.create_bundle(job['id'])) as bundle:
        assert 'job.json' in bundle.namelist()
        assert not any(name.startswith('input/') for name in bundle.namelist())
        assert str(tmp_path) not in bundle.read('job.json').decode()
    retry=service.restart_job(job['id'])
    assert retry['parent_job_id']==job['id']
    assert retry['id']!=job['id']
    service.cancel_job(retry['id'])
    service.close()


def test_backup_restore_rebinds_paths_and_preserves_references(tmp_path):
    service=Service(tmp_path/'source',start_runner=False)
    project,matrix,plan=prepared(service)
    original=service.submit_job(plan['id'])
    archive=service.backup()
    restored_root=tmp_path/'restored'
    restored=service.restore_backup(archive,restored_root)
    assert restored['restored'] is True
    restored_service=Service(restored_root,start_runner=False)
    assert restored_service.get_job(original['id'])['matrix_id']==matrix['id']
    asset=restored_service.store.get('matrices',matrix['id'])
    assert Path(asset['_path']).is_relative_to(restored_root)
    retry=restored_service.submit_job(restored_service.create_plan(config=plan['config'])['id'])
    assert retry['status']=='QUEUED'
    with pytest.raises(ServiceError): service.restore_backup(archive,restored_root)
    restored_service.close(); service.close()


def test_archive_matrix_retains_history_and_rejects_new_plans(tmp_path):
    service=Service(tmp_path,start_runner=False)
    _,matrix,plan=prepared(service)
    job=service.submit_job(plan['id'])
    assert service.archive_matrix(matrix['id'])['archived'] is True
    assert service.get_job(job['id'])['matrix_id']==matrix['id']
    with pytest.raises(ServiceError): service.create_plan(config=plan['config'])
    service.close()


def test_worker_timeout_and_cancel_are_terminal_and_retriable(tmp_path):
    import time
    service=Service(tmp_path)
    project,matrix,plan=prepared(service)
    config=plan['config']; config['resources']['walltime_seconds']=0.01
    timeout_job=service.submit_job(service.create_plan(config=config)['id'])
    deadline=time.monotonic()+10
    while service.get_job(timeout_job['id'])['status'] not in {'TIMED_OUT','FAILED'} and time.monotonic()<deadline: time.sleep(.02)
    done=service.get_job(timeout_job['id'])
    assert done['status']=='TIMED_OUT'
    assert done['error']['code']=='WALLTIME_EXCEEDED'
    assert service.diagnose(done['id'])['suggestions'][0]['changes']['solver']['max_outer_iterations']<config['solver']['max_outer_iterations']
    running=service.submit_job(plan['id'])
    deadline=time.monotonic()+5
    while service.get_job(running['id'])['status']=='QUEUED' and time.monotonic()<deadline: time.sleep(.005)
    service.cancel_job(running['id'])
    while service.get_job(running['id'])['status']=='CANCELLING' and time.monotonic()<deadline: time.sleep(.01)
    assert service.get_job(running['id'])['status']=='CANCELLED'
    service.close()


def test_restore_rejects_traversal_and_tampered_checksum(tmp_path):
    import zipfile,json
    service=Service(tmp_path/'source',start_runner=False)
    for name in ('../escape','runs/../../escape'):
        path=tmp_path/'bad.zip'
        with zipfile.ZipFile(path,'w') as archive:
            archive.writestr('metadata.sqlite3',b'fake')
            archive.writestr('backup-manifest.json','{}')
            archive.writestr(name,'unsafe')
        with pytest.raises(ServiceError) as exc: service.restore_backup(path,tmp_path/'target')
        assert exc.value.code=='UNSAFE_BACKUP_MEMBER'
        assert not (tmp_path/'target').exists()
        assert not (tmp_path/'escape').exists()
    prepared(service)
    original=service.backup()
    path=tmp_path/'changed.zip'
    with zipfile.ZipFile(original) as source,zipfile.ZipFile(path,'w') as archive:
        for item in source.infolist():
            archive.writestr(item.filename,b'corrupted' if item.filename=='metadata.sqlite3' else source.read(item))
    with pytest.raises(ServiceError) as exc: service.restore_backup(path,tmp_path/'target')
    assert exc.value.code=='BACKUP_HASH_MISMATCH'
    service.close()


def test_capability_rejections_and_cross_connection_idempotency(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    service=Service(tmp_path,start_runner=False)
    _,matrix,plan=prepared(service)
    for change in ({'solver':{'backend':'slepc'}},{'resources':{'device':'gpu'}},{'resources':{'mpi_ranks':2}},{'solver':{'method':'lobpcg'}}):
        config=__import__('copy').deepcopy(plan['config'])
        for section,values in change.items(): config[section].update(values)
        with pytest.raises(ServiceError) as exc: service.create_plan(config=config)
        assert exc.value.code=='BACKEND_UNAVAILABLE'
    second=Service(tmp_path,start_runner=False)
    with ThreadPoolExecutor(max_workers=2) as executor:
        ids=list(executor.map(lambda s:s.submit_job(plan['id'],'concurrent-retry')['id'],[service,second]))
    assert ids[0]==ids[1]
    assert len(service.list_jobs())==1
    second.close();service.close()


def test_fifo_queue_has_one_running_process_at_a_time(tmp_path):
    import time
    service=Service(tmp_path,start_runner=False)
    _,_,plan=prepared(service)
    jobs=[service.submit_job(plan['id']) for _ in range(2)]
    service.close()
    service=Service(tmp_path)
    deadline=time.monotonic()+15
    while any(service.get_job(j['id'])['status'] not in {'SUCCEEDED','PARTIAL','FAILED'} for j in jobs) and time.monotonic()<deadline:
        assert sum(j['status']=='RUNNING' for j in service.list_jobs())<=1
        time.sleep(.01)
    first,second=[service.get_job(j['id']) for j in jobs]
    assert first['status'] in {'SUCCEEDED','PARTIAL'}
    assert second['status'] in {'SUCCEEDED','PARTIAL'}
    assert first['finished_at']<=second['started_at']
    service.close()


def test_selected_export_preview_metadata_and_protected_cleanup(tmp_path):
    import numpy as np,time
    service=Service(tmp_path)
    project=service.create_project('complex data')
    matrix=service.generate_sample(project['id'],kind='complex',n=40)
    updated=service.update_matrix(matrix['id'],metadata={'source_url':'https://example.org/data','license':'user-approved','units':'s^-2','frequency_mapping':'lambda=omega_squared'})
    assert updated['metadata']['frequency_mapping']=='lambda=omega_squared'
    plan=service.create_plan(config={'problem':{'matrix_id':matrix['id']},'target':{'k':4,'sigma':1.0},'resources':{'threads':1}})
    job=service.submit_job(plan['id'])
    deadline=time.monotonic()+15
    while service.get_job(job['id'])['status'] not in {'SUCCEEDED','PARTIAL','FAILED'} and time.monotonic()<deadline:time.sleep(.02)
    assert service.get_job(job['id'])['status'] in {'SUCCEEDED','PARTIAL'}
    artifact=service.export_result(job['id'],[2,0],format='numpy')
    original=np.load(service.download(job['id'],'eigenvectors.npy'),allow_pickle=False)
    with np.load(artifact,allow_pickle=False) as exported:
        assert np.array_equal(exported['mode_indices'],[2,0])
        assert np.allclose(exported['eigenvectors'],original[:,[2,0]])
        assert np.iscomplexobj(exported['eigenvectors'])
    preview=service.vector_preview(job['id'],0,sample_points=16)
    assert len(preview['samples'])==16
    assert preview['interpretation']=='algebraic mode samples; no physical field geometry supplied'
    with pytest.raises(ServiceError):service.export_result(job['id'],[-1])
    with pytest.raises(ServiceError):service.vector_preview(job['id'],5)
    assert 'frequencies_hz' in service.results(job['id'])
    path=service.root/'inbox'/'old.tmp';path.write_bytes(b'failed import')
    __import__('os').utime(path,(0,0))
    dry=service.cleanup(dry_run=True)
    assert path.exists() and 'inbox/old.tmp' in dry['candidates']
    service.cleanup(dry_run=False)
    assert not path.exists()
    assert service.download(job['id'],'eigenvectors.npy').exists()
    assert Path(service.store.get('matrices',matrix['id'])['_path']).exists()
    service.close()


def test_batch_keeps_every_run_and_builds_html_report(tmp_path):
    service=Service(tmp_path,start_runner=False)
    project,matrix,plan=prepared(service)
    batch=service.create_benchmark(project['id'],[matrix['id']],k=4,sigma=.1,repeats=3)
    assert len(batch['jobs'])==3
    service.cancel_job(batch['jobs'][1])
    batch=service.get_benchmark(batch['id'])
    assert batch['summary']['total']==3
    assert batch['summary']['failed']==1
    assert len(batch['runs'])==3
    report=service.create_report(benchmark_id=batch['id'])
    text=service.report_path(report['id']).read_text()
    for job_id in batch['jobs']: assert job_id in text
    service.close()


def test_restart_persists_parent_before_runner_can_finish(tmp_path,monkeypatch):
    service=Service(tmp_path,start_runner=False)
    _,_,plan=prepared(service)
    original=service.submit_job(plan['id'])
    service._finish(original['id'],'CANCELLED','USER_CANCELLED')
    saved_submit=service.submit_job
    first_rows=[]
    saved_put=service.store.put
    def record(kind,key,value):
        if kind=='jobs' and key!=original['id'] and not first_rows:
            first_rows.append(value.copy())
        return saved_put(kind,key,value)
    monkeypatch.setattr(service.store,'put',record)
    def worker_wins(*args,**kwargs):
        submitted=saved_submit(*args,**kwargs)
        running={**submitted,'status':'RUNNING'}
        service.store.put('jobs',submitted['id'],running)
        service._finish(submitted['id'],'SUCCEEDED')
        return submitted
    monkeypatch.setattr(service,'submit_job',worker_wins)
    retry=service.restart_job(original['id'])
    assert first_rows[0]['parent_job_id']==original['id']
    assert service.get_job(retry['id'])['status']=='SUCCEEDED'
    service.close()


def test_public_bundle_uses_frozen_permission_and_excludes_private_results(tmp_path):
    import json,zipfile
    import numpy as np
    service=Service(tmp_path,start_runner=False)
    _,matrix,_=prepared(service)
    service.update_matrix(matrix['id'],metadata={'public':False})
    config={'problem':{'matrix_id':matrix['id']},'target':{'k':4}}
    private=service.submit_job(service.create_plan(config=config)['id'])
    service.store.put('jobs',private['id'],{**private,'status':'RUNNING'})
    service._finish(private['id'],'PARTIAL',result={'eigenvalues':[123.456]})
    run=service.root/'runs'/private['id']/'artifacts';run.mkdir(parents=True)
    np.save(run/'eigenvectors.npy',np.eye(2))
    (run/'config.resolved.yaml').write_text('target:\n  k: 4\n')
    service.update_matrix(matrix['id'],metadata={'public':True,'license':'CC0-1.0'})
    with zipfile.ZipFile(service.create_bundle(private['id'])) as archive:
        assert 'result' not in json.loads(archive.read('job.json'))
        assert not json.loads(archive.read('export-policy.json'))['results_included']
        assert 'run/eigenvectors.npy' not in archive.namelist()
    public=service.submit_job(service.create_plan(config=config)['id'])
    service.store.put('jobs',public['id'],{**public,'status':'RUNNING'})
    service._finish(public['id'],'PARTIAL',result={'eigenvalues':[123.456]})
    with zipfile.ZipFile(service.create_bundle(public['id'])) as archive:
        assert json.loads(archive.read('job.json'))['result']['eigenvalues']==[123.456]
        assert json.loads(archive.read('export-policy.json'))['results_included']
    service.close()


def test_benchmark_deduplicates_inputs_and_rejects_queue_overflow_before_submit(tmp_path):
    service=Service(tmp_path,start_runner=False)
    project,matrix,_=prepared(service)
    batch=service.create_benchmark(project['id'],[matrix['id'],matrix['id']],repeats=2)
    assert len(batch['jobs'])==2 and batch['matrix_ids']==[matrix['id']]
    for index in range(98):
        service.store.put('jobs',str(index),{'id':str(index),'status':'QUEUED'})
    before=len(service.list_jobs())
    with pytest.raises(ServiceError) as error:
        service.create_benchmark(project['id'],[matrix['id']],repeats=1)
    assert error.value.code=='BENCHMARK_QUEUE_LIMIT'
    assert len(service.list_jobs())==before
    service.close()
