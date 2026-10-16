"""Explicit real symmetric-indefinite MUMPS out-of-core profile contracts."""
import copy
import json
import os
from pathlib import Path
import numpy as np
import pytest
from scipy import sparse

from emsolver.contracts import resolve_config
from emsolver.optional_backends import driver_config

PROFILE='mumps_ooc_symmetric_v1'

def config():
    return resolve_config({'solver':{'backend':'slepc','method':'krylovschur','subspace_max':16,
        'linear':{'preconditioner':PROFILE}},'target':{'k':2,'sigma':.1},
        'resources':{'threads':1,'mpi_ranks':1,'memory_limit_gib':4,'walltime_seconds':30,'disk_limit_gib':.2}})

def test_ooc_profile_is_explicit_and_workspace_follows_container_budget():
    actual=driver_config(config())
    assert actual['solver']['factor_profile']==PROFILE
    assert actual['resources']['mumps_workspace_mb']==716
    larger=config();larger['resources']['memory_limit_gib']=16
    assert driver_config(larger)['resources']['mumps_workspace_mb']==768
    legacy=config();legacy['solver']['linear']['preconditioner']='auto_budgeted'
    plain=driver_config(legacy)
    assert 'factor_profile' not in plain['solver']
    assert set(plain['resources'])=={'mpi_ranks','threads'}

@pytest.mark.parametrize('group,key,value',[
    ('resources','mpi_ranks',2),('resources','threads',2),('resources','device','gpu'),
    ('problem','scalar','complex128'),('solver.linear','precision','mixed_float32')])
def test_ooc_rejects_unsupported_profiles_before_allocating(group,key,value):
    changed=config();target=changed
    for piece in group.split('.'):target=target[piece]
    target[key]=value
    with pytest.raises(ValueError,match='UNSUPPORTED_SLEPC_COMBINATION'):driver_config(changed)

def test_ooc_workspace_minimum_refuses_too_small_container_budget():
    changed=config();changed['resources']['memory_limit_gib']=.1
    with pytest.raises(RuntimeError,match='RESOURCE_EXHAUSTED'):driver_config(changed)

def test_driver_accepts_only_complete_bounded_ooc_spec(tmp_path):
    from emsolver.numerics.slepc_driver import read_spec
    spec={'schema_version':'1.0','input_sha256':'a'*64,'config':driver_config(config())}
    path=tmp_path/'spec.json';path.write_text(json.dumps(spec))
    assert read_spec(path)==spec
    for group,key,value in [('resources','mumps_workspace_mb',True),('resources','mumps_workspace_mb',63),
        ('resources','mumps_workspace_mb',769),('resources','mpi_ranks',2),('resources','threads',2),
        ('solver','factor_profile','unknown')]:
        wrong=copy.deepcopy(spec);wrong['config'][group][key]=value;path.write_text(json.dumps(wrong))
        with pytest.raises(ValueError):read_spec(path)
    for group,key in [('solver','factor_profile'),('resources','mumps_workspace_mb')]:
        wrong=copy.deepcopy(spec);del wrong['config'][group][key];path.write_text(json.dumps(wrong))
        with pytest.raises(ValueError):read_spec(path)

def test_borrowed_csr_buffers_are_canonical_correct_type_and_writable(tmp_path):
    from emsolver.numerics.slepc_driver import writable_csr_arrays
    matrix=sparse.diags([-np.ones(5),np.linspace(-2.,3.,6),-np.ones(5)],[-1,0,1],format='csr')
    original=matrix.copy()
    for array in (matrix.data,matrix.indices,matrix.indptr):array.flags.writeable=False
    indptr,indices,data=writable_csr_arrays(matrix,np.int32,np.float64)
    assert all(array.flags.writeable and array.flags.c_contiguous for array in (indptr,indices,data))
    assert indptr.dtype==indices.dtype==np.int32 and data.dtype==np.float64
    assert np.array_equal(data,original.data) and np.array_equal(indices,original.indices)
    assert not np.shares_memory(data,matrix.data)
    malformed=matrix.copy();malformed.has_canonical_format=False
    with pytest.raises(ValueError,match='INVALID_CSR_SNAPSHOT'):writable_csr_arrays(malformed,np.int32,np.float64)
    with pytest.raises(ValueError,match='SCALAR_TYPE_MISMATCH'):writable_csr_arrays(matrix.astype(complex),np.int32,np.float64)

def test_driver_failure_mapping_preserves_only_known_resource_and_factor_reasons(tmp_path):
    from emsolver.optional_backends import read_driver_failure
    path=tmp_path/'failure.json'
    path.write_text(json.dumps({'schema_version':'1.0','error_code':'RESOURCE_EXHAUSTED',
        'failure_stage':'factorization_and_solve','reason':'mumps_workspace_exhausted',
        'diagnostics':{'mumps_infog':{'1':-13,'2':100},'mumps_icntl':{'22':1,'23':716}}}))
    assert read_driver_failure(path)['error_code']=='RESOURCE_EXHAUSTED'
    path.write_text(json.dumps({'schema_version':'1.0','error_code':'UNVERIFIED_SUCCESS',
        'failure_stage':'factorization_and_solve','reason':'made_up','diagnostics':{}}))
    with pytest.raises(ValueError,match='INVALID_BACKEND_FAILURE'):read_driver_failure(path)

actual_ooc=pytest.mark.skipif(os.environ.get('EMSOLVER_TEST_SLEPC_OOC')!='1',reason='Explicit rebuilt OOC-tested SLEPc image required')

@actual_ooc
def test_actual_ooc_real_indefinite_candidates_have_original_residual_and_diagnostics(tmp_path):
    from emsolver.optional_backends import solve_slepc,probe_slepc
    assert PROFILE in probe_slepc()['factor_profiles']
    n=32;off=np.full(n-1,.035)
    matrix=sparse.diags([off,np.linspace(-4.,5.,n),off],[-1,0,1],format='csr')
    result=solve_slepc(matrix,config(),tmp_path/'published')
    values,vectors=result['values'],result['vectors'];expected=np.linalg.eigvalsh(matrix.toarray())
    wanted=expected[np.argsort(abs(expected-.1))[:2]]
    assert max(np.min(abs(values-value)) for value in wanted)<1e-10
    assert np.max(abs(matrix@vectors-vectors*values))<1e-9
    metrics=result['metrics']
    assert metrics['factor_profile']==PROFILE and metrics['mumps_symmetric_mode']==2
    assert metrics['mumps_icntl']=={'22':1,'23':716}
    assert metrics['mumps_infog']['1']==0
    assert metrics['ooc_bytes_before_factor_destroy']>=0
    assert metrics['ooc_inertia_is_certificate'] is False
    assert result['strategy']['linear_solver']['factorization']=='symmetric_indefinite_ldlt'
    assert list(tmp_path.iterdir())==[]

@actual_ooc
def test_actual_ooc_cancellation_retains_status_and_cleans_private_workspace(tmp_path):
    from emsolver.optional_backends import solve_slepc
    matrix=sparse.diags(np.linspace(-3.,4.,80),format='csr')
    def cancelled(_):raise RuntimeError('USER_CANCELLED')
    with pytest.raises(RuntimeError,match='USER_CANCELLED'):
        solve_slepc(matrix,config(),tmp_path/'published',event_callback=cancelled)
    assert list(tmp_path.iterdir())==[]

@actual_ooc
def test_actual_ooc_rejects_complex_before_container_launch(tmp_path):
    from emsolver.optional_backends import solve_slepc
    matrix=sparse.diags(np.arange(1.,9.).astype(complex),format='csr')
    with pytest.raises(ValueError,match='UNSUPPORTED_SLEPC_COMBINATION'):
        solve_slepc(matrix,config(),tmp_path/'published')
    assert list(tmp_path.iterdir())==[]

def test_ooc_capability_requires_explicit_tested_profile_and_seed_roundtrip():
    from emsolver.optional_backends import _supported_profile,_validate_seed
    versions={'petsc_version':[3,18,5],'slepc_version':[3,18,2],
        'mpi_library':'Open MPI 4.1.4','integer_dtype':'int32',
        'platform':{'system':'Linux','machine':'aarch64','python':'3.11.2'}}
    legacy=_supported_profile('sha256:'+'a'*64,'arm64','b'*64,versions)
    assert legacy['factor_profiles']==['in_core_lu_v1']
    tested=_supported_profile('sha256:'+'a'*64,'arm64','b'*64,versions,ooc_tested=True)
    assert tested['factor_profiles']==['in_core_lu_v1',PROFILE]
    assert _validate_seed(tested)==tested
    invalid=copy.deepcopy(tested);invalid['factor_profiles'].append('unverified_gpu_factor')
    with pytest.raises(ValueError,match='INVALID_SLEPC_PROFILE'):_validate_seed(invalid)

def test_ooc_failure_preserves_workspace_diagnosis_and_disk_usage_before_destroy(tmp_path):
    from types import SimpleNamespace
    from emsolver.numerics.slepc_driver import _write_ooc_failure
    from emsolver.optional_backends import read_driver_failure
    scratch=tmp_path/'ooc';scratch.mkdir();(scratch/'factor').write_bytes(b'x'*64)
    factor=SimpleNamespace(getMumpsInfog=lambda index:-13 if index==1 else 0,
        getMumpsIcntl=lambda index:1 if index==22 else 716)
    pc=SimpleNamespace(getFactorMatrix=lambda:factor)
    code=_write_ooc_failure(tmp_path,RuntimeError('private native error text'),pc,scratch,'factorization_and_solve')
    assert code=='RESOURCE_EXHAUSTED'
    failure=read_driver_failure(tmp_path/'failure.json')
    assert failure['reason']=='mumps_workspace_exhausted'
    assert failure['diagnostics']['ooc_bytes_before_factor_destroy']==64
    assert 'private' not in (tmp_path/'failure.json').read_text()

@actual_ooc
def test_actual_ooc_disk_budget_and_cleanup_apply_to_private_factor_workspace(tmp_path):
    from emsolver.optional_backends import solve_slepc
    changed=config();changed['resources']['disk_limit_gib']=1e-7
    matrix=sparse.diags(np.linspace(-3.,4.,80),format='csr')
    with pytest.raises(RuntimeError,match='RESOURCE_EXHAUSTED'):
        solve_slepc(matrix,changed,tmp_path/'published')
    assert list(tmp_path.iterdir())==[]

@actual_ooc
def test_actual_ooc_singular_target_preserves_structured_failure_and_cleans_container(tmp_path):
    from emsolver.optional_backends import solve_slepc,read_driver_failure
    changed=config();changed['target']['sigma']=0.
    matrix=sparse.diags(np.arange(-6.,7.),format='csr')
    with pytest.raises(ValueError,match='SLEPC_FACTORIZATION_FAILED'):
        solve_slepc(matrix,changed,tmp_path/'published')
    failure=read_driver_failure(tmp_path/'published/slepc.failure.json')
    assert failure['reason']=='mumps_singular_factor'
    assert failure['diagnostics']['mumps_infog']['1'] in {-6,-10}
    assert not list(tmp_path.glob('.slepc-*'))
