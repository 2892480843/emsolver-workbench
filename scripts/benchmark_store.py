#!/usr/bin/env python3
"""Reproduce a local query diagnostic without touching application data."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import platform
import sqlite3
import statistics
import sys
import tempfile
import time
from unittest.mock import patch

# Direct script execution must measure this checkout rather than an installed wheel.
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

import emsolver.jobs as jobs_module
import emsolver.store as store_module
from emsolver.jobs import ACTIVE, JobRepository, JobStatus
from emsolver.store import Store


def legacy_summary(store, project_id=None):
    """Frozen pre-aggregation query path, kept only as the diagnostic baseline."""
    with store.read_transaction():
        counts = {status.value: store.count('jobs', status=status, project_id=project_id) for status in JobStatus}
        return {'projects': store.count('projects'),
                'matrices': store.count('matrices', project_id=project_id),
                'jobs': store.count('jobs', project_id=project_id),
                'active_jobs': sum(counts[status.value] for status in ACTIVE),
                'certified_jobs': counts[JobStatus.SUCCEEDED],
                'job_status_counts': counts}


def run(rows=20000,repeats=7):
    with tempfile.TemporaryDirectory(prefix='emsolver-query-') as directory:
        store=Store(Path(directory)/'metadata.sqlite3')
        try:
            projects=min(rows,100)
            with store.transaction():
                for index in range(projects):
                    project_id='p' if index==0 else f'p{index}'
                    store.put('projects',project_id,{'id':project_id})
                    store.put('matrices',str(index),{'id':str(index),'project_id':project_id})
                for index in range(rows):
                    scope=index%projects
                    store.put('jobs',str(index),{'id':str(index),'project_id':'p' if scope==0 else f'p{scope}',
                        'status':'QUEUED' if index>=rows-20 else 'SUCCEEDED','payload':'x'*1800})
            jobs=JobRepository(store)
            functions={
                'legacy_full_list_first':lambda:next(x for x in store.list('jobs') if x['status']=='QUEUED'),
                'indexed_first':lambda:store.first('jobs',status='QUEUED'),
                'indexed_count':lambda:store.count('jobs',status='QUEUED'),
                'indexed_page_50':lambda:store.page('jobs',limit=50),
                'legacy_summary_global':lambda:legacy_summary(store),
                'aggregated_summary_global':lambda:jobs.summary(),
                'legacy_summary_project':lambda:legacy_summary(store,'p'),
                'aggregated_summary_project':lambda:jobs.summary('p'),
            }
            assert functions['legacy_full_list_first']()['id']==functions['indexed_first']()['id']
            assert functions['indexed_count']()==20
            for scope in ('global','project'):
                assert functions[f'legacy_summary_{scope}']()==functions[f'aggregated_summary_{scope}']()
            # Warm each path, then rotate its position within interleaved rounds.
            # Query count is deterministic; elapsed time can vary with cache and load.
            for function in functions.values():function()
            samples={key:[] for key in functions}
            names=list(functions)
            for iteration in range(repeats):
                order=names[iteration%len(names):]+names[:iteration%len(names)]
                for key in order:
                    function=functions[key]
                    start=time.perf_counter_ns();function();samples[key].append((time.perf_counter_ns()-start)/1e6)
            decoded,selects={},{}
            loads=json.loads
            for key,function in functions.items():
                with patch('emsolver.store.json.loads',wraps=loads) as decoder:
                    function();decoded[key]=decoder.call_count
                statements=[]
                store.db.set_trace_callback(statements.append)
                try:function()
                finally:store.db.set_trace_callback(None)
                selects[key]=len([statement for statement in statements if statement.lstrip().upper().startswith('SELECT')])
                if key.startswith('aggregated_summary_'):
                    assert selects[key]<=3 and decoded[key]==0, 'Aggregation diagnostic loaded an unexpected query path'
            plans={name:[row[3] for row in store.db.execute(query,parameters)] for name,query,parameters in (
                ('queue_first','EXPLAIN QUERY PLAN SELECT body FROM entities WHERE kind=? AND status=? ORDER BY seq LIMIT 1',('jobs','QUEUED')),
                ('queue_count','EXPLAIN QUERY PLAN SELECT count(*) FROM entities WHERE kind=? AND status=?',('jobs','QUEUED')),
                ('summary_global','EXPLAIN QUERY PLAN SELECT status,COUNT(*) FROM entities WHERE kind=? GROUP BY status',('jobs',)),
                ('summary_project','EXPLAIN QUERY PLAN SELECT status,COUNT(*) FROM entities WHERE kind=? AND project_id=? GROUP BY status',('jobs','p')),
            )}
            return {'rows':rows,'repeats':repeats,'projects':projects,'scoped_project_jobs':store.count('jobs',project_id='p'),
                'scope':'Local warmed-cache synthetic query diagnostic; not solver or production performance.',
                'protocol':'One warmup per path; interleaved rounds with rotating path order. Baseline and current summary results checked equal.',
                'environment':{'platform':platform.platform(),'machine':platform.machine(),'python':sys.version.split()[0],'sqlite':sqlite3.sqlite_version},
                'source_files':{name:{'path':str(Path(path).resolve()),'sha256':hashlib.sha256(Path(path).read_bytes()).hexdigest()}
                                for name,path in (('benchmark',__file__),('jobs',jobs_module.__file__),('store',store_module.__file__))},
                'samples_ms':samples,'median_ms':{key:statistics.median(value) for key,value in samples.items()},
                'range_ms':{key:[min(value),max(value)] for key,value in samples.items()},
                'decoded_json_records':decoded,'select_statements':selects,'query_plans':plans,
                'summary_equal':{'global':True,'project':True},
                'limitations':['Timing varies with data distribution, cache, runtime and competing machine load; no fixed speedup is guaranteed.',
                               'Synthetic jobs contain 1800-byte payloads, 20 queued jobs, and otherwise succeeded jobs across up to 100 projects.',
                               'No application data, solver workload, release package, schema or index is modified.']}
        finally:store.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rows',type=int,default=20000)
    parser.add_argument('--repeats',type=int,default=7)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if not 20<=args.rows<=100000 or not 1<=args.repeats<=30:parser.error('rows20..100000 and repeats1..30 required')
    result=run(args.rows,args.repeats)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result['median_ms']))
