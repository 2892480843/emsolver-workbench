import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import psutil
import pytest

from emsolver.contracts import resolve_config


class MemoryStore:
    def __init__(self,matrix):self.matrix=matrix
    def get(self,kind,identifier):
        return self.matrix if kind=='matrices' and identifier==self.matrix['id'] else None


def fixture_job(tmp_path):
    root=tmp_path/'data'
    directory=root/'runs'/'j_worker'
    directory.mkdir(parents=True)
    (root/'matrices').mkdir()
    matrix=root/'matrices'/'matrix.npz'
    matrix.write_bytes(b'private immutable matrix')
    matrix.chmod(0o400)
    sha=hashlib.sha256(matrix.read_bytes()).hexdigest()
    record={'id':'m_worker','_path':str(matrix),'hash':sha}
    config=resolve_config({'problem':{'matrix_id':record['id']},'target':{'k':2}})
    job={'id':'j_worker','matrix_id':record['id'],'matrix_hash':sha,'config':config,
         'status':'RUNNING','worker_pid':76543,'worker_create_time':1234.5}
    spec=directory/'worker.spec.json'
    spec.write_text(json.dumps({'config':config,'input_path':str(matrix),'output_dir':str(directory/'artifacts')}),encoding='utf-8')
    spec.chmod(0o600)
    return SimpleNamespace(root=root,store=MemoryStore(record)),job,spec


class FakeProcess:
    def __init__(self,pid,command,birth=1234.5):
        self.pid=pid;self.command=command;self.birth=birth;self.running=True;self.signals=[]
    def create_time(self):return self.birth
    def cmdline(self):return self.command
    def exe(self):return str(Path(sys.executable).resolve())
    def uids(self):return SimpleNamespace(real=os.getuid())
    def is_running(self):return self.running
    def status(self):return psutil.STATUS_RUNNING if self.running else psutil.STATUS_DEAD
    def environ(self):return {'PYTHONPATH':str(Path(__file__).resolve().parents[1])}
    def children(self,recursive):return []
    def terminate(self):self.signals.append('terminate');self.running=False
    def kill(self):self.signals.append('kill');self.running=False


def fake_worker(monkeypatch,gateway,job,spec):
    process=FakeProcess(job['worker_pid'],[sys.executable,'-m','emsolver.worker',str(spec)])
    monkeypatch.setattr(psutil,'Process',lambda pid:process)
    return process


def test_verified_worker_adapter_attaches_without_signals_and_reports_nonchild_exit(tmp_path,monkeypatch):
    from emsolver.recovery import verified_worker,UNKNOWN_RETURN_CODE
    gateway,job,spec=fixture_job(tmp_path)
    process=fake_worker(monkeypatch,gateway,job,spec)
    adapter=verified_worker(gateway,job)
    assert adapter is not None and adapter.pid==job['worker_pid']
    assert adapter.poll() is None and process.signals==[]
    with pytest.raises(subprocess.TimeoutExpired):adapter.wait(timeout=.001)
    process.running=False
    assert adapter.poll()==UNKNOWN_RETURN_CODE
    assert adapter.exit_status_known is False


@pytest.mark.parametrize('change',['birth','module','extra_argument','input_path','output_path','config','writable_input','symlink_spec','missing_birth'])
def test_unverified_worker_is_never_adopted_or_signalled(tmp_path,monkeypatch,change):
    from emsolver.recovery import verified_worker
    gateway,job,spec=fixture_job(tmp_path)
    process=fake_worker(monkeypatch,gateway,job,spec)
    value=json.loads(spec.read_text())
    if change=='birth':process.birth+=1
    elif change=='module':process.command[2]='another.worker'
    elif change=='extra_argument':process.command.append('--unexpected')
    elif change=='input_path':value['input_path']='/tmp/unrelated.npz'
    elif change=='output_path':value['output_dir']=str(gateway.root/'runs'/'another'/'artifacts')
    elif change=='config':value['config']['target']['k']=1
    elif change=='writable_input':Path(gateway.store.matrix['_path']).chmod(0o600)
    elif change=='missing_birth':job.pop('worker_create_time')
    elif change=='symlink_spec':
        replacement=spec.with_name('replacement.json')
        spec.rename(replacement)
        spec.symlink_to(replacement)
    if change not in {'symlink_spec'}:spec.write_text(json.dumps(value),encoding='utf-8')
    assert verified_worker(gateway,job) is None
    assert process.signals==[]


def test_pid_reuse_or_spec_mutation_after_adoption_prevents_termination(tmp_path,monkeypatch):
    from emsolver.recovery import verified_worker
    gateway,job,spec=fixture_job(tmp_path)
    process=fake_worker(monkeypatch,gateway,job,spec)
    adapter=verified_worker(gateway,job)
    process.birth+=1
    adapter.kill()
    adapter.terminate_tree()
    assert process.signals==[]
    process.birth-=1
    spec.write_text('{}',encoding='utf-8')
    adapter.kill()
    adapter.terminate_tree()
    assert process.signals==[]


def test_verified_tree_termination_signals_only_the_adopted_identity(tmp_path,monkeypatch):
    from emsolver.recovery import verified_worker
    gateway,job,spec=fixture_job(tmp_path)
    process=fake_worker(monkeypatch,gateway,job,spec)
    adapter=verified_worker(gateway,job)
    adapter.terminate_tree()
    assert process.signals==['terminate']
    assert adapter.poll() is not None


def test_server_worker_outlives_launcher_and_can_be_verified_without_orphan_leak(tmp_path):
    from scipy import sparse
    import numpy as np
    from emsolver.recovery import verified_worker
    gateway,job,spec=fixture_job(tmp_path)
    matrix=Path(gateway.store.matrix['_path'])
    matrix.chmod(0o600)
    n=2000
    sparse.save_npz(matrix,sparse.diags([np.ones(n-1)*.08,np.linspace(-3.,4.,n),np.ones(n-1)*.08],[-1,0,1],format='csr'))
    matrix.chmod(0o400)
    sha=hashlib.sha256(matrix.read_bytes()).hexdigest()
    gateway.store.matrix['hash']=sha;job['matrix_hash']=sha
    job['config']=resolve_config({'problem':{'matrix_id':job['matrix_id']},'target':{'k':2},
              'solver':{'method':'block_shift_invert','max_outer_iterations':1000000,'subspace_max':10},
              'validation':{'residual_scaled_max':1e-25},'resources':{'threads':1,'walltime_seconds':30}})
    spec.write_text(json.dumps({'config':job['config'],'input_path':str(matrix),'output_dir':str(spec.parent/'artifacts')}),encoding='utf-8')
    launcher='''import json,os,subprocess,sys,psutil
environment=os.environ.copy()
environment['PYTHONPATH']=sys.argv[2]
environment.update(OPENBLAS_NUM_THREADS='1',OMP_NUM_THREADS='1',VECLIB_MAXIMUM_THREADS='1')
worker=subprocess.Popen([sys.executable,'-m','emsolver.worker',sys.argv[1]],env=environment,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
print(json.dumps({'pid':worker.pid,'birth':psutil.Process(worker.pid).create_time(),'launcher_pid':os.getpid()}),flush=True)
'''
    launched=subprocess.run([sys.executable,'-c',launcher,str(spec),str(Path(__file__).resolve().parents[1])],capture_output=True,text=True,check=True,timeout=10)
    info=json.loads(launched.stdout)
    job.update(worker_pid=info['pid'],worker_create_time=info['birth'])
    process=psutil.Process(info['pid'])
    try:
        assert process.ppid()!=info['launcher_pid']
        deadline=time.monotonic()+5
        while not (spec.parent/'worker.events.jsonl').exists() and process.is_running() and time.monotonic()<deadline:
            time.sleep(.02)
        assert (spec.parent/'worker.events.jsonl').exists()
        adapter=verified_worker(gateway,job)
        # Apple Silicon's Docker translation can expose a non-stat-able
        # Rosetta executable instead of the worker interpreter. Preserve the
        # production rejection; this environment cannot prove adoption.
        if adapter is None and process.exe()=='/run/rosetta/rosetta':
            try:os.path.samefile(process.exe(),sys.executable)
            except FileNotFoundError:
                assert verified_worker(gateway,job) is None
                pytest.skip('Rosetta process executable is unavailable; verified_worker correctly fails closed')
        assert adapter is not None and adapter.poll() is None
        adapter.terminate_tree()
        assert adapter.wait(timeout=5) is not None
    finally:
        try:
            if process.is_running() and abs(process.create_time()-info['birth'])<.001:
                process.kill()
        except psutil.NoSuchProcess:pass


def test_executable_change_after_adoption_is_not_signalled(tmp_path,monkeypatch):
    from emsolver.recovery import verified_worker
    gateway,job,spec=fixture_job(tmp_path)
    process=fake_worker(monkeypatch,gateway,job,spec)
    adapter=verified_worker(gateway,job)
    process.exe=lambda:'/bin/sh'
    adapter.kill()
    adapter.terminate_tree()
    assert process.signals==[]


@pytest.mark.parametrize('content',['x'*(1024**2+1),'{}\n{}','{"config":{},"config":{}}','{"config":NaN}'])
def test_unbounded_or_ambiguous_specifications_are_rejected_without_signals(tmp_path,monkeypatch,content):
    from emsolver.recovery import verified_worker
    gateway,job,spec=fixture_job(tmp_path)
    process=fake_worker(monkeypatch,gateway,job,spec)
    spec.write_text(content,encoding='utf-8')
    assert verified_worker(gateway,job) is None
    assert process.signals==[]


def test_descendant_pid_reuse_is_not_signalled_as_an_original_child(tmp_path,monkeypatch):
    from emsolver.recovery import verified_worker
    gateway,job,spec=fixture_job(tmp_path)
    process=fake_worker(monkeypatch,gateway,job,spec)
    legitimate=FakeProcess(76544,['numerical-child'],birth=1235.)
    reused=FakeProcess(76545,['unrelated'],birth=1235.)
    reads={'count':0}
    def reused_birth():
        reads['count']+=1
        return 1235. if reads['count']==1 else 1240.
    reused.create_time=reused_birth
    process.children=lambda recursive:[legitimate,reused]
    adapter=verified_worker(gateway,job)
    adapter.terminate_tree()
    assert legitimate.signals==['terminate']
    assert reused.signals==[]
