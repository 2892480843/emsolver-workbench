import pytest

from emsolver.jobs import JobRepository, JobStatus, transition
from emsolver.store import Store


def test_terminal_states_cannot_be_reopened_and_cancellation_wins():
    job={'id':'j','status':'RUNNING','metrics':{}}
    cancelled=transition(job,JobStatus.CANCELLING)
    assert job['status']=='RUNNING'
    with pytest.raises(ValueError,match='INVALID_JOB_TRANSITION'):
        transition(cancelled,JobStatus.SUCCEEDED)
    final=transition(cancelled,JobStatus.CANCELLED)
    with pytest.raises(ValueError,match='INVALID_JOB_TRANSITION'):
        transition(final,JobStatus.RUNNING)
    with pytest.raises(ValueError,match='INVALID_JOB_TRANSITION'):
        transition({'status':'QUEUED'},JobStatus.SUCCEEDED)


def test_job_repository_queries_do_not_decode_history_lists(tmp_path,monkeypatch):
    store=Store(tmp_path/'state.sqlite3')
    for index,status in enumerate(['SUCCEEDED']*100+['QUEUED','QUEUED','RUNNING']):
        store.put('jobs',str(index),{'id':str(index),'status':status,'project_id':'p','metrics':{}})
    store.put('projects','p',{'id':'p'})
    store.put('matrices','m',{'id':'m','project_id':'p'})
    def forbidden(*args,**kwargs):raise AssertionError('full-list scan')
    monkeypatch.setattr(store,'list',forbidden)
    jobs=JobRepository(store)
    assert jobs.next_queued()['id']=='100'
    assert jobs.queued_count()==2
    assert [row['id'] for row in jobs.recoverable()]==['102']
    summary=jobs.summary('p')
    assert summary['jobs']==103 and summary['active_jobs']==3 and summary['certified_jobs']==100
    assert summary['projects']==1 and summary['matrices']==1
    assert jobs.page(limit=1,status='QUEUED')['items'][0]['id']=='100'
    with pytest.raises(ValueError):jobs.page(status='not-a-job-status')
    store.close()
