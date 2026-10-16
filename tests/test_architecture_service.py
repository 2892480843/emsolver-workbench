import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from emsolver.api import create_app
from emsolver.service import Service


def prepare(service):
    project=service.create_project('Architecture')
    matrix=service.generate_sample(project['id'],n=12)
    plan=service.create_plan(config={'problem':{'matrix_id':matrix['id']},'target':{'k':2},'resources':{'threads':1}})
    return project,matrix,plan


def test_submit_rollback_does_not_leave_ghost_file_events(tmp_path,monkeypatch):
    service=Service(tmp_path,start_runner=False)
    _,_,plan=prepare(service)
    original=service.store.audit
    def fail(action=None,*args,**kwargs):
        if action=='job.submit':raise OSError('injected audit failure')
        return original(action,*args,**kwargs)
    monkeypatch.setattr(service.store,'audit',fail)
    with pytest.raises(OSError):service.submit_job(plan['id'])
    assert service.store.list('jobs')==[]
    assert service.store.db.execute('SELECT count(*) FROM events').fetchone()[0]==0
    assert not list((tmp_path/'runs').glob('*/events.jsonl'))
    service.close()


def test_queue_hot_paths_use_indexed_queries(tmp_path,monkeypatch):
    service=Service(tmp_path,start_runner=False)
    project,matrix,plan=prepare(service)
    def forbidden(*args,**kwargs):raise AssertionError('history scan')
    monkeypatch.setattr(service,'list_jobs',forbidden)
    first=service.submit_job(plan['id'])
    assert service.jobs.next_queued()['id']==first['id']
    assert service.workspace_summary()['active_jobs']==1
    batch=service.create_benchmark(project['id'],[matrix['id']],k=2,repeats=2)
    assert len(batch['jobs'])==2
    service.cancel_job(first['id'])
    assert service.jobs.queued_count()==2
    service.close()


def test_worker_final_event_is_drained_before_terminal_event(tmp_path,monkeypatch):
    import emsolver.service as module
    service=Service(tmp_path,start_runner=False)
    _,_,plan=prepare(service)
    job=service.submit_job(plan['id'])
    directory=tmp_path/'runs'/job['id']
    class Process:
        pid=os.getpid()
        def poll(self):
            # Deterministically place the last event after the ordinary drain.
            (directory/'worker.events.jsonl').write_text(json.dumps({'stage':'exporting','event':'FINAL_WORKER_EVENT'})+'\n')
            (directory/'worker.result.json').write_text(json.dumps({'status':'SUCCEEDED','n_accepted':2}))
            return 0
    monkeypatch.setattr(module.subprocess,'Popen',lambda *args,**kwargs:Process())
    try:
        service._execute(job['id'])
        names=[row['event'] for row in service.events(job['id'])]
        assert 'FINAL_WORKER_EVENT' in names
        assert names.index('FINAL_WORKER_EVENT')<names.index('SUCCEEDED')
    finally:
        service._process=None
        service.close()


def test_paged_api_keeps_legacy_lists_and_uses_real_totals(tmp_path):
    with TestClient(create_app(tmp_path,start_runner=False)) as client:
        service=client.app.state.service
        project,_,plan=prepare(service)
        jobs=[service.submit_job(plan['id']) for _ in range(5)]
        first=client.get('/api/v1/jobs/page',params={'limit':2}).json()
        assert [row['id'] for row in first['items']]==[row['id'] for row in jobs[:2]]
        assert first['total']==5 and first['next_cursor']
        second=client.get('/api/v1/jobs/page',params={'limit':2,'cursor':first['next_cursor']}).json()
        assert [row['id'] for row in second['items']]==[row['id'] for row in jobs[2:4]]
        assert len(client.get('/api/v1/jobs').json())==5
        service.cancel_job(jobs[0]['id'])
        logs=client.get(f"/api/v1/jobs/{jobs[0]['id']}/events/page",params={'limit':1}).json()
        assert logs['next_after']==1 and logs['has_more']
        tail=client.get(f"/api/v1/jobs/{jobs[0]['id']}/events/page",params={'after':logs['next_after']}).json()
        assert tail['items'][0]['event']=='CANCELLED' and not tail['has_more']
        summary=client.get('/api/v1/workspace/summary',params={'project_id':project['id']}).json()
        assert summary['jobs']==5 and summary['active_jobs']==4 and summary['matrices']==1
        assert client.get('/api/v1/jobs/page',params={'cursor':'corrupt'}).status_code==400
        assert client.get('/api/v1/jobs/page',params={'limit':201}).status_code==422
        assert client.get('/api/v1/jobs/page',params={'status':'ALL'}).status_code==422


def test_repeated_cancellation_is_idempotent_while_worker_exits(tmp_path):
    service=Service(tmp_path,start_runner=False)
    _,_,plan=prepare(service)
    job=service.submit_job(plan['id'])
    job.update(status='RUNNING')
    service.store.put('jobs',job['id'],job)
    assert service.cancel_job(job['id'])['status']=='CANCELLING'
    assert service.cancel_job(job['id'])['status']=='CANCELLING'
    assert sum(row['event']=='CANCEL_REQUESTED' for row in service.events(job['id']))==1
    service._finish(job['id'],'SUCCEEDED')
    assert service.get_job(job['id'])['status']=='CANCELLED'
    service.close()


def test_restore_streams_database_rows_and_rebuilds_indexes_in_queue_order(tmp_path,monkeypatch):
    import sqlite3
    service=Service(tmp_path/'source',start_runner=False)
    _,_,plan=prepare(service)
    jobs=[service.submit_job(plan['id']) for _ in range(3)]
    service.cancel_job(jobs[1]['id'])
    with service.store.transaction():
        for index in range(1005):service._event(jobs[0]['id'],{'event':'sample','iteration':index})
    archive=service.backup()
    connect=sqlite3.connect
    class Cursor(sqlite3.Cursor):
        def fetchall(self):raise AssertionError('restore must batch database reads')
    class Connection(sqlite3.Connection):
        def execute(self,query,*args):return self.cursor(Cursor).execute(query,*args)
    def bounded(*args,**kwargs):
        if kwargs.get('uri'):kwargs['factory']=Connection
        return connect(*args,**kwargs)
    monkeypatch.setattr(sqlite3,'connect',bounded)
    restored=service.restore_backup(archive,tmp_path/'restored')
    assert restored['restored']
    other=Service(tmp_path/'restored',start_runner=False)
    assert [row['id'] for row in other.list_jobs()]==[row['id'] for row in jobs]
    assert other.jobs.next_queued()['id']==jobs[0]['id']
    assert other.store.count('jobs',status='QUEUED')==2
    assert len(other.events(jobs[0]['id']))==1006
    assert other.store.instance_id!=service.store.instance_id
    other.close();service.close()


def test_legacy_backup_restores_with_original_job_order(tmp_path):
    import sqlite3,zipfile,hashlib
    service=Service(tmp_path/'source',start_runner=False)
    _,_,plan=prepare(service)
    jobs=[service.submit_job(plan['id']) for _ in range(3)]
    original=service.backup();database=tmp_path/'legacy.sqlite3'
    with zipfile.ZipFile(original) as z:database.write_bytes(z.read('metadata.sqlite3'))
    with sqlite3.connect(database) as connection:
        connection.execute('ALTER TABLE entities RENAME TO entities_schema2')
        connection.execute('CREATE TABLE entities(kind TEXT,id TEXT,body TEXT NOT NULL,PRIMARY KEY(kind,id))')
        connection.execute('INSERT INTO entities(kind,id,body) SELECT kind,id,body FROM entities_schema2 ORDER BY seq')
        connection.execute('DROP TABLE entities_schema2')
        connection.execute('DROP TABLE store_meta')
        connection.execute('PRAGMA user_version=0')
    archive=tmp_path/'legacy.zip'
    with zipfile.ZipFile(original) as source,zipfile.ZipFile(archive,'w') as target:
        manifest=json.loads(source.read('backup-manifest.json'))
        manifest['files']['metadata.sqlite3']=hashlib.sha256(database.read_bytes()).hexdigest()
        for member in source.namelist():
            target.writestr(member,json.dumps(manifest) if member=='backup-manifest.json' else database.read_bytes() if member=='metadata.sqlite3' else source.read(member))
    service.restore_backup(archive,tmp_path/'restored')
    restored=Service(tmp_path/'restored',start_runner=False)
    assert [job['id'] for job in restored.list_jobs()]==[job['id'] for job in jobs]
    assert restored.store.db.execute('PRAGMA user_version').fetchone()[0]==2
    assert restored.jobs.queued_count()==3
    restored.close();service.close()
