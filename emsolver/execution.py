"""Synchronous CLI execution with a monitored, isolated numerical child."""
from __future__ import annotations

import json
import multiprocessing as mp
import os
from pathlib import Path
import queue
import re
import time

import psutil
import yaml

from .reporting import atomic_text, sanitize


def _compute(matrix, config, output, channel):
    from .process_environment import child_environment
    threads=str(config['resources']['threads'])
    environment=child_environment(threads)
    os.environ.clear();os.environ.update(environment)
    try:
        from threadpoolctl import threadpool_limits
        from .numerics import run_solver
        def emit(event):
            try: channel.put_nowait(sanitize(event))
            except queue.Full: pass
        with threadpool_limits(limits=int(threads)):
            result=run_solver(matrix,config,output,event_callback=emit)
        atomic_text(Path(output)/'summary.json',json.dumps(sanitize(result),ensure_ascii=False,allow_nan=False))
    except BaseException as exc:
        stable=re.match(r'^([A-Z][A-Z_]{3,}):',str(exc))
        code=stable.group(1) if stable else ('INVALID_INPUT' if isinstance(exc,(ValueError,FileNotFoundError)) else 'IO_ERROR')
        status='TIMED_OUT' if code=='WALLTIME_EXCEEDED' else 'FAILED'
        result={'status':status,'error_code':code,'error':code,'eigenvalues':[], 'residuals':[], 'n_accepted':0}
        atomic_text(Path(output)/'summary.json',json.dumps(sanitize(result),ensure_ascii=False))


def execute_local(matrix: Path, config: dict, output: Path) -> dict:
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    if (output/'summary.json').exists() or (output/'manifest.json').exists():
        raise ValueError('OUTPUT_EXISTS: use a new output directory to preserve previous runs')
    context=mp.get_context('spawn');channel=context.Queue(maxsize=2000)
    child=context.Process(target=_compute,args=(str(matrix),config,str(output),channel))
    start=time.monotonic();peak=0;events=[];termination=None
    child.start()
    try:
        process=psutil.Process(child.pid)
        while child.is_alive():
            try:
                processes=[process,*process.children(recursive=True)]
                rss=sum(p.memory_info().rss for p in processes if p.is_running())
                peak=max(peak,rss)
                if rss>config['resources']['memory_limit_gib']*1024**3:
                    termination=('FAILED','RESOURCE_EXHAUSTED')
                if time.monotonic()-start>config['resources']['walltime_seconds']:
                    termination=('TIMED_OUT','WALLTIME_EXCEEDED')
                disk=sum(p.stat().st_size for p in output.rglob('*') if p.is_file())
                if disk>config['resources']['disk_limit_gib']*1024**3:
                    termination=('FAILED','RESOURCE_EXHAUSTED')
            except psutil.Error: pass
            try:
                while True: events.append(channel.get_nowait())
            except queue.Empty: pass
            if termination:
                for descendant in process.children(recursive=True):
                    try:descendant.terminate()
                    except psutil.Error:pass
                child.terminate();break
            time.sleep(.025)
        child.join(timeout=5)
    except KeyboardInterrupt:
        termination=('CANCELLED','USER_CANCELLED');child.terminate();child.join(timeout=5)
    finally:
        if child.is_alive():child.kill();child.join(timeout=5)
        try:
            while True:events.append(channel.get_nowait())
        except queue.Empty:pass
        channel.close()
    summary=output/'summary.json'
    result=json.loads(summary.read_text()) if summary.exists() else {'status':'FAILED','error_code':'WORKER_LOST','n_accepted':0}
    if termination:result.update(status=termination[0],error_code=termination[1])
    result.setdefault('validation',{'numerical_status':'NOT_VALIDATED','target_certification':'UNVERIFIED'})
    # Numerical export requires an empty directory. Preserve the submitted
    # configuration after the child exits, including failures before export.
    config_path=output/'config.resolved.yaml'
    if not (output/'manifest.json').exists() or not config_path.exists():
        atomic_text(config_path,yaml.safe_dump(config,allow_unicode=True,sort_keys=True))
    metrics=result.setdefault('metrics',{})
    metrics.update(end_to_end_seconds=time.monotonic()-start,process_tree_peak_bytes=peak,
                   memory_limit_bytes=int(config['resources']['memory_limit_gib']*1024**3))
    atomic_text(summary,json.dumps(sanitize(result),ensure_ascii=False,indent=2,allow_nan=False))
    # The numerical manifest hashes metrics.json; parent observations are separate.
    atomic_text(output/'execution.metrics.json',json.dumps(sanitize(metrics),ensure_ascii=False,indent=2,allow_nan=False))
    existing=output/'events.jsonl'
    if not existing.exists():
        atomic_text(existing,'\n'.join(json.dumps({'seq':i+1,**e},ensure_ascii=False) for i,e in enumerate(events)))
    if termination:
        atomic_text(output/'manifest.json',json.dumps({'status':result['status'],'error_code':result['error_code'],'complete':False}))
    return result
