"""Accuracy continuation uses an independent synthetic grid, never public answers."""
import numpy as np
import pytest
from scipy import sparse
from emsolver.contracts import resolve_config
from emsolver.numerics import core


def config(**kwargs):
    result=resolve_config({'target':{'k':2,'sigma':0.},'solver':{'method':'jacobi_davidson','transform':'precond','subspace_max':15,'max_outer_iterations':100,'linear':{'method':'gmres','preconditioner':'ilu_budgeted_v1','rtol_initial':1e-6},'block':{'max_restarts':4}},'resources':{'threads':1}})
    for section,values in kwargs.items():result[section].update(values)
    return result


def grid_matrix(complex_input=False):
    t=sparse.diags([-np.ones(3),np.full(4,2.),-np.ones(3)],[-1,0,1],format='csr')
    a=(25000*sparse.kron(t,sparse.eye(4))+100000*sparse.kron(sparse.eye(4),t)).tocsr()
    if complex_input:
        # Use exact power-of-two Gaussian phases so strict IEEE Hermiticity
        # remains true, independently of floating matrix multiplication order.
        phases=np.array([1.,1j,-1.,-1j]*4)
        coo=(25000*sparse.kron(t,sparse.eye(4))+100000*sparse.kron(sparse.eye(4),t)).tocoo()
        a=sparse.csr_matrix((coo.data*phases[coo.row]*phases[coo.col].conjugate(),(coo.row,coo.col)),shape=coo.shape)
    return a


@pytest.mark.parametrize('complex_input',[False,True])
def test_jd_continues_after_numerical_pass_to_reach_existing_strict_certificate(tmp_path,complex_input):
    a=grid_matrix(complex_input);source=tmp_path/'input.npz';sparse.save_npz(source,a)
    events=[];r=core.run_solver(source,config(),tmp_path/'run',event_callback=events.append)
    assert r['status']=='SUCCEEDED',r['validation']
    assert r['validation']['target_certification']=='CERTIFIED'
    assert r['metrics']['accuracy_refinement_checks']>0
    assert r['metrics']['accuracy_refinement_iterations']>0
    assert any(e['event']=='certification_accuracy_refinement' for e in events)
    assert core.verify_run(source,tmp_path/'run')['target_certification']=='CERTIFIED'


def test_budget_exhaustion_preserves_previously_numerical_valid_modes(tmp_path):
    source=tmp_path/'input.npz';sparse.save_npz(source,grid_matrix())
    r=core.run_solver(source,config(solver={'max_outer_iterations':5}),tmp_path/'run')
    assert r['status']=='PARTIAL'
    assert r['validation']['numerical_status']=='PASSED'
    assert r['n_accepted']==2
    assert r['metrics']['accuracy_refinement_pending']==2
    assert r['validation']['target_certification']=='UNVERIFIED'


def test_opt_out_keeps_numeric_stopping_and_refinement_hint_never_is_certificate(tmp_path):
    source=tmp_path/'input.npz';sparse.save_npz(source,grid_matrix())
    r=core.run_solver(source,config(validation={'completeness_required':False}),tmp_path/'run')
    assert r['validation']['numerical_status']=='PASSED'
    assert r['validation']['target_certification']=='UNVERIFIED'
    assert r['metrics']['accuracy_refinement_checks']==0


def test_inapplicable_certificate_does_not_start_futile_precision_refinement(tmp_path):
    # A connected zero-diagonal block crossing sigma cannot be separated by
    # positive weighted circles. Its exact zero mode is numerically solvable.
    block=sparse.csr_matrix([[0.,0.,.1],[0.,0.,0.],[.1,0.,0.]])
    a=sparse.block_diag([block,sparse.eye(78)*20],format='csr')
    source=tmp_path/'input.npz';sparse.save_npz(source,a)
    cfg=config(target={'k':1},solver={'subspace_max':12})
    r=core.run_solver(source,cfg,tmp_path/'run')
    assert r['status']=='PARTIAL',r
    assert r['validation']['numerical_status']=='PASSED'
    assert r['metrics']['accuracy_certification_probes']==1
    assert r['metrics']['accuracy_refinement_checks']==0
    assert r['metrics']['accuracy_certification_probe_seconds']>=0


@pytest.mark.parametrize('complex_input',[False,True])
def test_new_ilu_profile_is_used_once_per_solve_with_cached_outer_constraints(tmp_path,complex_input):
    a=grid_matrix(complex_input);source=tmp_path/'input.npz';sparse.save_npz(source,a)
    cfg=config();cfg['solver']['linear']['preconditioner']='ilu_equilibrated_compensated_v2'
    r=core.run_solver(source,cfg,tmp_path/'run')
    assert r['status']=='SUCCEEDED',r
    assert r['metrics']['preconditioner_factorizations']==1
    assert r['metrics']['preconditioner_equilibration_passes']==4
    assert r['metrics']['preconditioner_constraint_builds']==r['metrics']['outer_iterations']-1
    assert r['metrics']['preconditioner_constraint_solves']>0
    assert r['metrics']['max_linear_residual']<=cfg['solver']['linear']['rtol_initial']*(1+1e-6)
    assert core.verify_run(source,tmp_path/'run')['target_certification']=='CERTIFIED'


def test_shared_fidelity_and_hint_handle_extreme_scaling_and_cancellation():
    from fractions import Fraction
    from emsolver.numerics.accuracy import refinement_readiness, value_fidelity_budget
    assert value_fidelity_budget(16,Fraction(3,2))==Fraction(4096*16,2**52)*Fraction(3,2)
    a=sparse.diags([1.,2.],format='csr');x=np.eye(2)
    for scale in (1e-300,1e300):
        ready=refinement_readiness(a,np.array([scale,2*scale]),x,scale,[0,1],check=lambda:None)
        assert ready.all()
    assert not refinement_readiness(a,np.array([1.,2.]),np.zeros((2,2)),1.,[0,1],check=lambda:None).any()
    def stop():raise InterruptedError('CANCELLED')
    with pytest.raises(InterruptedError,match='CANCELLED'):
        refinement_readiness(a,np.array([1.,2.]),x,1.,[0,1],check=stop)
