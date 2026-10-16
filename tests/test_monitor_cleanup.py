"""An OOC factor can disappear while the parent samples its working tree."""
from pathlib import Path
from types import SimpleNamespace
from emsolver.contracts import resolve_config
from emsolver.service import Service


def test_parent_monitor_survives_private_factor_removal(tmp_path,monkeypatch):
    service=Service(tmp_path/'data',start_runner=False)
    directory=tmp_path/'job';directory.mkdir()
    class DisappearingFactor:
        def is_file(self):return True
        def stat(self):raise FileNotFoundError('MUMPS factor destroyed after directory scan')
    original=Path.rglob
    def race(path,pattern):
        if path==directory:return iter([DisappearingFactor()])
        return original(path,pattern)
    monkeypatch.setattr(Path,'rglob',race)
    job={'id':'job','status':'RUNNING','stage':'solving','config':resolve_config({})}
    monkeypatch.setattr(service,'get_job',lambda _:job)
    monkeypatch.setattr(service,'_ingest_worker_events',lambda *args:None)
    result=[]
    monkeypatch.setattr(service,'_finish',lambda *args,**kwargs:result.append((args,kwargs)))
    try:
        service._monitor_worker('job',directory,SimpleNamespace(pid=2**30,poll=lambda:0))
        assert result[0][0]==('job','FAILED','WORKER_LOST')
        assert result[0][1]['metrics']['disk_bytes']==0
    finally:service.close()
