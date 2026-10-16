import json

import numpy as np
import pytest
from scipy import sparse

from emsolver.contracts import resolve_config
from emsolver.numerics import run_solver


def exact_config(**parts):
    return resolve_config({'target':{'k':2,'sigma':.15},
                          'solver':{'method':'block_shift_invert','subspace_max':14,'max_outer_iterations':200,
                                    'adaptive_shift':{'enabled':False},
                                    'block':{'checkpoint_interval':1,'checkpoint_kind':'exact_block'}},
                          'resources':{'threads':1},**parts})


@pytest.mark.parametrize('complex_path,reordered',[(False,False),(True,True)])
def test_exact_block_checkpoint_restores_next_iteration_bitwise_with_actual_refactorization(tmp_path,complex_path,reordered):
    n=60;off=np.full(n-1,.1j if complex_path else .1)
    a=sparse.diags([off.conjugate(),np.linspace(-2.,5.,n),off],[-1,0,1],format='csr')
    source=tmp_path/'input.npz';sparse.save_npz(source,a)
    cfg=exact_config(problem={'reordering':'reverse_cuthill_mckee' if reordered else 'none'})
    uninterrupted=[]
    baseline=run_solver(source,cfg,tmp_path/'baseline',event_callback=uninterrupted.append)
    events=[]
    def cancel():return any(event['event']=='checkpoint_written' and event.get('iteration')==3 for event in events)
    with pytest.raises(InterruptedError,match='CANCELLED'):
        run_solver(source,cfg,tmp_path/'interrupted',event_callback=events.append,cancel_check=cancel)
    checkpoint=tmp_path/'interrupted/exact-block.checkpoint.npz'
    assert checkpoint.is_file()
    resumed_events=[]
    resumed=run_solver(source,cfg,tmp_path/'resumed',event_callback=resumed_events.append,resume_checkpoint_path=checkpoint)
    assert resumed['eigenvalues']==baseline['eigenvalues']
    assert np.array_equal(np.load(tmp_path/'resumed/eigenvectors.npy'),np.load(tmp_path/'baseline/eigenvectors.npy'))
    assert resumed['metrics']['exact_resume'] is True
    assert resumed['metrics']['restored_outer_iterations']==3
    assert resumed['metrics']['outer_iterations']==baseline['metrics']['outer_iterations']
    assert resumed['metrics']['outer_iterations_executed_this_process']==baseline['metrics']['outer_iterations']-3
    assert resumed['metrics']['factorizations']==baseline['metrics']['factorizations']+1
    baseline_residuals=[event['residuals'] for event in uninterrupted if event['event']=='block_residual_checked' and event['iteration']>3]
    resumed_residuals=[event['residuals'] for event in resumed_events if event['event']=='block_residual_checked']
    assert resumed_residuals==baseline_residuals


@pytest.mark.parametrize('change',['adaptive','method','fallback','interval'])
def test_exact_block_profile_rejects_unsupported_controller_combinations(tmp_path,change):
    a=sparse.diags([np.ones(19)*.1,np.linspace(-2.,5.,20),np.ones(19)*.1],[-1,0,1],format='csr')
    source=tmp_path/'input.npz';sparse.save_npz(source,a)
    cfg=exact_config()
    if change=='adaptive':cfg['solver']['adaptive_shift']['enabled']=True
    elif change=='method':cfg['solver']['method']='arpack'
    elif change=='fallback':cfg['solver']['fallback_profiles']=['block_to_arpack_v1']
    else:cfg['solver']['block']['checkpoint_interval']=0
    with pytest.raises(ValueError,match='UNSUPPORTED_EXACT_CHECKPOINT'):
        run_solver(source,cfg,tmp_path/'out')


def test_exact_resume_cannot_be_confused_with_warm_start(tmp_path):
    cfg=exact_config()
    with pytest.raises(ValueError,match='INCOMPATIBLE_CHECKPOINT_REQUEST'):
        run_solver(tmp_path/'input.npz',cfg,tmp_path/'out',initial_subspace_path='one',resume_checkpoint_path='two')


def test_exact_block_resume_preserves_rng_after_actual_stagnation_restart(tmp_path):
    n=40
    a=sparse.diags([np.ones(n-1)*.01,np.linspace(-.001,.001,n),np.ones(n-1)*.01],[-1,0,1],format='csr')
    source=tmp_path/'input.npz';sparse.save_npz(source,a)
    cfg=exact_config();cfg['target']['sigma']=100.
    cfg['solver']['max_outer_iterations']=20;cfg['solver']['block']['stagnation_checks']=2
    baseline_events=[];baseline=run_solver(source,cfg,tmp_path/'baseline',event_callback=baseline_events.append)
    events=[]
    def cancel():return any(event['event']=='checkpoint_written' and event.get('iteration')==3 for event in events)
    with pytest.raises(InterruptedError):run_solver(source,cfg,tmp_path/'interrupted',event_callback=events.append,cancel_check=cancel)
    assert any(event['event']=='block_restarted' for event in events)
    resumed_events=[]
    resumed=run_solver(source,cfg,tmp_path/'resumed',resume_checkpoint_path=tmp_path/'interrupted/exact-block.checkpoint.npz',event_callback=resumed_events.append)
    assert resumed['metrics']['restarts']==baseline['metrics']['restarts']
    assert [event['residuals'] for event in resumed_events if event['event']=='block_residual_checked']==[
        event['residuals'] for event in baseline_events if event['event']=='block_residual_checked' and event['iteration']>3]
    with np.load(tmp_path/'baseline/subspace.checkpoint.npz',allow_pickle=False) as left,np.load(tmp_path/'resumed/subspace.checkpoint.npz',allow_pickle=False) as right:
        assert np.array_equal(left['basis'],right['basis'])


def test_exact_checkpoint_corruption_config_mismatch_and_archive_allocation_are_rejected(tmp_path):
    import hashlib
    from threadpoolctl import threadpool_limits
    from emsolver.numerics.exact_checkpoint import load_exact_checkpoint
    n=40;a=sparse.diags([np.ones(n-1)*.1,np.linspace(-2.,5.,n),np.ones(n-1)*.1],[-1,0,1],format='csr')
    source=tmp_path/'input.npz';sparse.save_npz(source,a);cfg=exact_config()
    events=[]
    with pytest.raises(InterruptedError):
        run_solver(source,cfg,tmp_path/'interrupted',event_callback=events.append,
                   cancel_check=lambda:any(event['event']=='checkpoint_written' for event in events))
    checkpoint=tmp_path/'interrupted/exact-block.checkpoint.npz'
    digest=hashlib.sha256(source.read_bytes()).hexdigest()
    with threadpool_limits(limits=1):
        state,metadata=load_exact_checkpoint(checkpoint,a,cfg,input_sha256=digest)
        assert metadata['phase']=='next_iteration_ready' and state['iteration']==1
        changed=json.loads(json.dumps(cfg));changed['solver']['random_seed']+=1
        with pytest.raises(ValueError,match='config_sha256'):
            load_exact_checkpoint(checkpoint,a,changed,input_sha256=digest)
    with np.load(checkpoint,allow_pickle=False) as archive:fields={name:archive[name] for name in archive.files}
    fields['basis']=fields['basis'].copy();fields['basis'][0,0]+=.01
    tampered=tmp_path/'tampered.npz';np.savez_compressed(tampered,**fields)
    with pytest.raises(ValueError,match='INTEGRITY_FAILED'):
        load_exact_checkpoint(tampered,a,cfg,input_sha256=digest)
    allocation=tmp_path/'allocation.npz'
    np.savez_compressed(allocation,basis=np.zeros((1,1)),metadata=np.asarray('{}'))
    with pytest.raises(ValueError,match='unexpected archive members'):
        load_exact_checkpoint(allocation,a,cfg,input_sha256=digest)


def test_exact_periodic_state_cannot_exceed_disk_budget_or_publish_success_manifest(tmp_path):
    n=2000
    a=sparse.diags([np.ones(n-1)*.01,np.linspace(-2.,5.,n),np.ones(n-1)*.01],[-1,0,1],format='csr')
    source=tmp_path/'input.npz';sparse.save_npz(source,a)
    cfg=exact_config();cfg['target']['k']=1
    cfg['solver']['subspace_max']=150;cfg['solver']['block']['oversampling']=128
    cfg['resources']['disk_limit_gib']=.0015
    with pytest.raises(RuntimeError,match='RESOURCE_EXHAUSTED'):
        run_solver(source,cfg,tmp_path/'run')
    assert not (tmp_path/'run/manifest.json').exists()
    assert sum(path.stat().st_size for path in (tmp_path/'run').glob('*') if path.is_file())<=.0015*1024**3


def test_exact_resume_retains_consumed_walltime_budget(tmp_path):
    import hashlib
    from threadpoolctl import threadpool_limits
    from emsolver.numerics.exact_checkpoint import load_exact_checkpoint,save_exact_checkpoint
    n=40;a=sparse.diags([np.ones(n-1)*.1,np.linspace(-2.,5.,n),np.ones(n-1)*.1],[-1,0,1],format='csr')
    source=tmp_path/'input.npz';sparse.save_npz(source,a);cfg=exact_config()
    cfg['resources']['walltime_seconds']=10.
    events=[]
    with pytest.raises(InterruptedError):
        run_solver(source,cfg,tmp_path/'interrupted',event_callback=events.append,
                   cancel_check=lambda:any(event['event']=='checkpoint_written' for event in events))
    checkpoint=tmp_path/'interrupted/exact-block.checkpoint.npz';digest=hashlib.sha256(source.read_bytes()).hexdigest()
    with threadpool_limits(limits=1):
        state,_=load_exact_checkpoint(checkpoint,a,cfg,input_sha256=digest)
        state['elapsed_seconds']=11.
        consumed=tmp_path/'consumed.npz';save_exact_checkpoint(consumed,a,cfg,state,input_sha256=digest)
    with pytest.raises(RuntimeError,match='WALLTIME_EXCEEDED'):
        run_solver(source,cfg,tmp_path/'resumed',resume_checkpoint_path=consumed)
    assert not (tmp_path/'resumed/manifest.json').exists()


def test_warm_checkpoint_accepts_only_the_new_inert_default_field_omission(tmp_path):
    from emsolver.contracts import content_hash
    from emsolver.numerics.checkpoint import save_checkpoint,load_checkpoint
    n=20;a=sparse.diags(np.arange(1.,n+1),format='csr')
    cfg=resolve_config({'target':{'k':2}})
    path=tmp_path/'warm.npz';basis=np.eye(n,3)
    save_checkpoint(path,a,cfg,basis)
    with np.load(path,allow_pickle=False) as archive:fields={name:archive[name] for name in archive.files}
    metadata=json.loads(str(fields['metadata'].item()))
    legacy=json.loads(json.dumps(cfg));legacy['solver']['block'].pop('checkpoint_kind')
    metadata['config_sha256']=content_hash(legacy);fields['metadata']=np.asarray(json.dumps(metadata))
    np.savez_compressed(path,**fields)
    restored,_=load_checkpoint(path,a,cfg)
    assert np.array_equal(restored,basis)
    from emsolver.resumption import validate_snapshot
    assert validate_snapshot(path,cfg,None)
    legacy['solver']['block'].pop('max_restarts')
    metadata['config_sha256']=content_hash(legacy);fields['metadata']=np.asarray(json.dumps(metadata))
    np.savez_compressed(path,**fields)
    with pytest.raises(ValueError,match='CHECKPOINT_CONFIG_MISMATCH'):load_checkpoint(path,a,cfg)
    from emsolver.errors import ServiceError
    with pytest.raises(ServiceError,match='CHECKPOINT_INCOMPATIBLE'):validate_snapshot(path,cfg,None)


def test_exact_service_preflight_streams_digest_without_loading_candidate_arrays(tmp_path,monkeypatch):
    import hashlib
    from emsolver.numerics.exact_checkpoint import preflight_exact_checkpoint
    from emsolver.resumption import validate_exact_snapshot
    from emsolver.errors import ServiceError
    n=40;a=sparse.diags([np.ones(n-1)*.1,np.linspace(-2.,5.,n),np.ones(n-1)*.1],[-1,0,1],format='csr')
    source=tmp_path/'input.npz';sparse.save_npz(source,a);cfg=exact_config();events=[]
    with pytest.raises(InterruptedError):
        run_solver(source,cfg,tmp_path/'interrupted',event_callback=events.append,
                   cancel_check=lambda:any(event['event']=='checkpoint_written' for event in events))
    checkpoint=tmp_path/'interrupted/exact-block.checkpoint.npz';digest=hashlib.sha256(source.read_bytes()).hexdigest()
    def forbidden(*args,**kwargs):raise AssertionError('service preflight must not allocate basis through np.load')
    monkeypatch.setattr(np,'load',forbidden)
    metadata=preflight_exact_checkpoint(checkpoint,cfg,input_sha256=digest,expected_n=n)
    assert metadata['kind']=='exact_block' and metadata['state']['iteration']==1
    assert validate_exact_snapshot(checkpoint,cfg,digest)==hashlib.sha256(checkpoint.read_bytes()).hexdigest()
    with pytest.raises(ValueError,match='input_sha256'):
        preflight_exact_checkpoint(checkpoint,cfg,input_sha256='0'*64)
    with pytest.raises(ServiceError) as error:validate_exact_snapshot(checkpoint,cfg,'0'*64)
    assert error.value.status==409


def test_exact_streaming_preflight_rejects_forged_allocation_header_before_array_read(tmp_path,monkeypatch):
    import io,zipfile
    from emsolver.numerics.exact_checkpoint import preflight_exact_checkpoint
    # All members exist, but basis advertises a huge allocation with no payload.
    fake=tmp_path/'forged.npz'
    header=io.BytesIO()
    np.lib.format.write_array_header_1_0(header,{'descr':'<f8','fortran_order':False,'shape':(10**9,10**9)})
    with zipfile.ZipFile(fake,'w') as archive:
        archive.writestr('basis.npy',header.getvalue())
        for name,array in [('best_values',np.empty(0)),('best_vectors',np.empty((40,0))),('locked',np.zeros(6,dtype=bool)),('metadata',np.asarray('{}'))]:
            buffer=io.BytesIO();np.save(buffer,array,allow_pickle=False);archive.writestr(name+'.npy',buffer.getvalue())
    def forbidden(*args,**kwargs):raise AssertionError('forged header reached an allocating read')
    monkeypatch.setattr(np.lib.format,'read_array',forbidden)
    with pytest.raises(ValueError,match='INPUT_BUDGET_EXCEEDED'):
        preflight_exact_checkpoint(fake,exact_config(),input_sha256='0'*64)


def test_exact_atomic_state_survives_abrupt_process_exit_and_resumes_in_real_worker(tmp_path):
    import os,subprocess,sys
    from pathlib import Path
    n=70;off=np.full(n-1,.1j)
    a=sparse.diags([off.conjugate(),np.linspace(-2.,5.,n),off],[-1,0,1],format='csr')
    source=tmp_path/'input.npz';sparse.save_npz(source,a);cfg=exact_config()
    config_path=tmp_path/'config.json';config_path.write_text(json.dumps(cfg),encoding='utf-8')
    environment=os.environ.copy();environment['PYTHONPATH']=str(Path(__file__).resolve().parents[1])
    environment.update(OPENBLAS_NUM_THREADS='1',OMP_NUM_THREADS='1',VECLIB_MAXIMUM_THREADS='1')
    environment.pop('EMSOLVER_SLEPC_IMAGE',None)
    interrupted=tmp_path/'interrupted'
    code="""import json,os,sys
from emsolver.numerics import run_solver
def emit(event):
    if event['event']=='checkpoint_written' and event.get('iteration')==3:os._exit(9)
run_solver(sys.argv[1],json.load(open(sys.argv[2])),sys.argv[3],event_callback=emit)
"""
    crashed=subprocess.run([sys.executable,'-c',code,str(source),str(config_path),str(interrupted)],env=environment,capture_output=True,timeout=15)
    assert crashed.returncode==9
    assert (interrupted/'exact-block.checkpoint.npz').is_file()
    assert not (interrupted/'manifest.json').exists()
    results=[]
    for name,resume in [('baseline',False),('resumed',True)]:
        directory=tmp_path/name;directory.mkdir()
        spec={'config':cfg,'input_path':str(source),'output_dir':str(directory/'artifacts')}
        if resume:spec['resume_checkpoint_path']=str(interrupted/'exact-block.checkpoint.npz')
        path=directory/'worker.spec.json';path.write_text(json.dumps(spec),encoding='utf-8')
        process=subprocess.run([sys.executable,'-m','emsolver.worker',str(path)],env=environment,capture_output=True,timeout=15)
        diagnostic=(directory/'worker.result.json').read_text() if (directory/'worker.result.json').is_file() else process.stderr[-1000:]
        assert process.returncode==0,diagnostic
        results.append(json.loads((directory/'worker.result.json').read_text()))
    baseline,resumed=results
    assert baseline['eigenvalues']==resumed['eigenvalues']
    assert np.array_equal(np.load(tmp_path/'baseline/artifacts/eigenvectors.npy'),np.load(tmp_path/'resumed/artifacts/eigenvectors.npy'))
    assert resumed['metrics']['exact_resume'] is True
    assert resumed['metrics']['factorizations']==baseline['metrics']['factorizations']+1


def test_exact_checkpoint_binds_loaded_algorithm_even_when_disk_source_hash_matches_new_code(tmp_path,monkeypatch):
    import hashlib
    from pathlib import Path
    from threadpoolctl import threadpool_limits
    from emsolver.numerics import block
    from emsolver.numerics.exact_checkpoint import load_exact_checkpoint
    n=30;a=sparse.diags([np.ones(n-1)*.1,np.linspace(-2.,5.,n),np.ones(n-1)*.1],[-1,0,1],format='csr')
    source=tmp_path/'input.npz';sparse.save_npz(source,a);cfg=exact_config()
    # Model an installation edited after its worker imported the old function.
    # Both saves/loads see the same new disk bytes; only loaded code differs.
    read_bytes=Path.read_bytes
    def edited_disk(path):
        data=read_bytes(path)
        return data+b'\n# installation changed while the old worker was running\n' if path==Path(block.__file__) else data
    monkeypatch.setattr(Path,'read_bytes',edited_disk)
    events=[]
    with pytest.raises(InterruptedError):
        run_solver(source,cfg,tmp_path/'interrupted',event_callback=events.append,
                   cancel_check=lambda:any(event['event']=='checkpoint_written' for event in events))
    checkpoint=tmp_path/'interrupted/exact-block.checkpoint.npz';digest=hashlib.sha256(read_bytes(source)).hexdigest()
    with np.load(checkpoint,allow_pickle=False) as archive:metadata=json.loads(str(archive['metadata'].item()))
    assert 'loaded_algorithm_sha256' in metadata
    original_basis=block._basis
    def new_loaded_basis(*args,**kwargs):return original_basis(*args,**kwargs)
    monkeypatch.setattr(block,'_basis',new_loaded_basis)
    with threadpool_limits(limits=1),pytest.raises(ValueError,match='loaded_algorithm_sha256'):
        load_exact_checkpoint(checkpoint,a,cfg,input_sha256=digest)


def test_loaded_algorithm_digest_normalizes_relocated_nested_code_filenames(monkeypatch):
    from types import CodeType,FunctionType
    from emsolver.numerics import core
    from emsolver.numerics.exact_checkpoint import _loaded_algorithm_hash
    before=_loaded_algorithm_hash();original=core._target_order
    def relocate(code):
        return code.replace(co_filename='/another/trusted/install/core.py',
                            co_consts=tuple(relocate(item) if isinstance(item,CodeType) else item for item in code.co_consts))
    changed=FunctionType(relocate(original.__code__),original.__globals__,original.__name__,original.__defaults__,original.__closure__)
    monkeypatch.setattr(core,'_target_order',changed)
    assert _loaded_algorithm_hash()==before


def test_loaded_algorithm_digest_ignores_equal_immutable_constant_reference_layout(monkeypatch):
    from types import FunctionType
    from emsolver.numerics import block
    from emsolver.numerics.exact_checkpoint import _loaded_algorithm_hash
    compiled=compile("def probe():\n    return ('constant with spaces', 'constant with spaces')\n",'<probe>','exec').co_consts[0]
    shared='constant with spaces'
    shared_code=compiled.replace(co_consts=(None,(shared,shared)))
    independent_code=compiled.replace(co_consts=(None,(shared.encode().decode(),shared.encode().decode())))
    assert shared_code==independent_code
    monkeypatch.setattr(block,'_basis',FunctionType(shared_code,{}))
    before=_loaded_algorithm_hash()
    monkeypatch.setattr(block,'_basis',FunctionType(independent_code,{}))
    assert _loaded_algorithm_hash()==before


def test_loaded_algorithm_digest_is_stable_for_source_and_bytecode_across_hash_seeds(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path
    root=Path(__file__).resolve().parents[1]
    script='from emsolver.numerics.exact_checkpoint import _loaded_algorithm_hash;print(_loaded_algorithm_hash())'
    results=[]
    for seed,compile_source in [('1',False),('2',True),('3',False)]:
        command=[sys.executable]
        if compile_source:command+=['-X',f'pycache_prefix={tmp_path/"fresh-pycache"}']
        process=subprocess.run([*command,'-c',script],cwd=root,
                               env={**os.environ,'PYTHONHASHSEED':seed,'PYTHONPATH':str(root)},
                               capture_output=True,text=True,check=True,timeout=30)
        results.append(process.stdout.strip())
    assert len(set(results))==1


def test_exact_environment_ignores_library_observation_order_but_binds_all_fields(monkeypatch):
    from emsolver.numerics import core
    from emsolver.numerics.exact_checkpoint import _environment
    first={'internal_api':'openblas','prefix':'libscipy_openblas','version':'0.3.34','architecture':'ARMV8','num_threads':1}
    second={**first,'version':'0.3.31'}
    monkeypatch.setattr(core,'_threadpools',lambda:[first,second,first.copy()])
    before=_environment()
    monkeypatch.setattr(core,'_threadpools',lambda:[second,first.copy(),first])
    assert _environment()==before
    # Multiplicity and every library property remain part of the identity.
    monkeypatch.setattr(core,'_threadpools',lambda:[first,second])
    assert _environment()!=before
    for key,value in [('internal_api','other'),('prefix','other'),('version','0.3.35'),
                      ('architecture','other'),('num_threads',2)]:
        monkeypatch.setattr(core,'_threadpools',lambda key=key,value=value:[first,second,{**first,key:value}])
        assert _environment()!=before
