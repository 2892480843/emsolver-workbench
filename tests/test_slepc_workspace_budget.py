"""Private budget enforcement must not depend on observing a running process."""
from pathlib import Path
import json
import numpy as np
import pytest
from scipy import sparse
from emsolver.contracts import resolve_config
import emsolver.optional_backends as backend


def completed_backend(monkeypatch,tmp_path,*,extra=None):
    started=[]
    monkeypatch.setattr(backend,'probe_slepc',lambda:{'supported':True,
        'factor_profiles':[backend.OOC_PROFILE],'image_id':'sha256:'+'a'*64,
        'architecture':'arm64','versions':{},'driver_sha256':'b'*64})
    monkeypatch.setattr(backend,'_settings',lambda:('docker','local',{},None))
    monkeypatch.setattr(backend,'_remove',lambda *args:True)
    class Completed:
        returncode=0
        def __init__(self,*args,**kwargs):
            started.append(True)
            workspace=next(tmp_path.glob('.slepc-*'));output=workspace/'output';snapshot=workspace/'input/matrix.npz'
            np.save(output/'values.npy',np.array([-1.,1.]))
            np.save(output/'vectors.npy',np.eye(8)[:,[3,5]])
            result={'schema_version':'1.0','candidate_status':'CONVERGED',
                'input_sha256':backend.file_hash(snapshot),'n':8,'n_candidates':2,
                'values_sha256':backend.file_hash(output/'values.npy'),
                'vectors_sha256':backend.file_hash(output/'vectors.npy'),
                'metrics':{'backend':'slepc','method':'krylovschur','mpi_ranks':1},
                'certification':'CANDIDATES_ONLY_MAIN_WORKER_MUST_VERIFY_ORIGINAL_PROBLEM'}
            (output/'result.json').write_text(json.dumps(result))
            if extra=='disk':
                scratch=output/'ooc';scratch.mkdir();(scratch/'factor').write_bytes(b'x'*(128*1024))
            elif extra in {'stdout','stderr'}:
                stream=kwargs[extra];stream.write(b'x'*(backend.MAX_JSON_BYTES+1));stream.flush()
        def poll(self):return 0
    monkeypatch.setattr(backend.subprocess,'Popen',Completed)
    config=resolve_config({'solver':{'backend':'slepc','method':'krylovschur','subspace_max':6,
        'linear':{'preconditioner':backend.OOC_PROFILE}},'target':{'k':2},
        'resources':{'threads':1,'mpi_ranks':1,'memory_limit_gib':4,'walltime_seconds':30,
                     'disk_limit_gib':(64*1024)/1024**3 if extra=='disk' else .02}})
    return config,started


def test_private_snapshot_exceeding_budget_is_rejected_before_container_launch(tmp_path,monkeypatch):
    config,started=completed_backend(monkeypatch,tmp_path)
    config['resources']['disk_limit_gib']=1e-6
    with pytest.raises(RuntimeError,match='RESOURCE_EXHAUSTED'):
        backend.solve_slepc(sparse.diags(np.arange(-4.,4.),format='csr'),config,tmp_path/'published')
    assert started==[]
    assert list(tmp_path.iterdir())==[]


@pytest.mark.parametrize('extra,exception,message',[
    ('disk',RuntimeError,'RESOURCE_EXHAUSTED'),
    ('stdout',ValueError,'BACKEND_LOG_BUDGET_EXCEEDED'),
    ('stderr',ValueError,'BACKEND_LOG_BUDGET_EXCEEDED')])
def test_already_finished_container_cannot_bypass_final_private_budgets(tmp_path,monkeypatch,extra,exception,message):
    config,started=completed_backend(monkeypatch,tmp_path,extra=extra)
    with pytest.raises(exception,match=message):
        backend.solve_slepc(sparse.diags(np.arange(-4.,4.),format='csr'),config,tmp_path/'published')
    assert started==[True]
    assert list(tmp_path.iterdir())==[]


def test_completed_container_within_private_budgets_keeps_valid_candidates(tmp_path,monkeypatch):
    config,started=completed_backend(monkeypatch,tmp_path)
    result=backend.solve_slepc(sparse.diags(np.arange(-4.,4.),format='csr'),config,tmp_path/'published')
    assert result['values'].tolist()==[-1.,1.]
    assert started==[True] and list(tmp_path.iterdir())==[]


def test_private_directory_counter_tolerates_deleted_factors_and_ignores_symlinks(tmp_path,monkeypatch):
    from emsolver.optional_backends import private_directory_bytes
    workspace=tmp_path/'workspace';workspace.mkdir()
    (workspace/'keep').write_bytes(b'1234567');vanishing=workspace/'factor';vanishing.write_bytes(b'x'*80)
    outside=tmp_path/'outside';outside.write_bytes(b'x'*4096)
    (workspace/'link').symlink_to(outside)
    actual=backend.os.scandir
    class Entry:
        def __init__(self,entry):self.entry=entry;self.path=entry.path
        def stat(self,*,follow_symlinks=True):
            if self.entry.name=='factor':
                vanishing.unlink();raise FileNotFoundError(str(vanishing))
            return self.entry.stat(follow_symlinks=follow_symlinks)
    class Scan:
        def __init__(self,path):self.entries=actual(path)
        def __enter__(self):return iter(Entry(entry) for entry in self.entries)
        def __exit__(self,*args):self.entries.close()
    monkeypatch.setattr(backend.os,'scandir',Scan)
    assert private_directory_bytes(workspace,100)==7
    assert not vanishing.exists()
