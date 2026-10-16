"""Route large exact proofs without allocating one row index per nonzero."""
import numpy as np
import pytest
from scipy import sparse
from emsolver.contracts import resolve_config
from emsolver.numerics import core


def test_certification_checks_cancellation_before_structural_classification(monkeypatch):
    a = sparse.diags([1., 2., 3.], format='csr')
    cfg = resolve_config({'target': {'k': 1, 'sigma': 0.}})
    def cancelled(): raise InterruptedError('CANCELLED_BEFORE_CERTIFICATION')
    def forbidden(*args, **kwargs): raise AssertionError('Cancellation must precede structural scans')
    monkeypatch.setattr(core, '_is_diagonal', forbidden)
    with pytest.raises(InterruptedError, match='CANCELLED_BEFORE_CERTIFICATION'):
        core._certify(a, np.ones(1), np.eye(3)[:, :1], cfg, check=cancelled)


def test_structural_checks_use_bounded_chunks_including_empty_rows(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('No full nonzero-sized row-index expansion')
    a=sparse.csr_matrix(([1.,2.],([0,70000],[0,70000])),shape=(70002,70002))
    monkeypatch.setattr(np,'repeat',forbidden)
    assert core._is_diagonal(a)
    off=a.tolil();off[0,2]=.125;off[2,0]=.125;off=off.tocsr()
    assert not core._is_diagonal(off)
    assert not core._is_banded_csr(off,1)
    tri=sparse.diags([np.ones(70001),np.ones(70002),np.ones(70001)],[-1,0,1],format='csr')
    assert core._is_banded_csr(tri,1)


def test_core_routes_large_nontridiagonal_problem_to_independent_gapped_count():
    n=81
    low=sparse.csr_matrix([[1.,0.,.125],[0.,2.,0.],[.125,0.,3.]])
    high=sparse.diags([np.full(n-4,.01),np.full(n-3,20.),np.full(n-4,.01)],[-1,0,1],format='csr')
    a=sparse.block_diag([low,high],format='csr')
    roots, small=np.linalg.eigh(low.toarray())
    order=np.lexsort((roots,abs(roots-1.5)))[:2]
    vectors=np.zeros((n,2));vectors[:3]=small[:,order]
    cfg=resolve_config({'target':{'k':2,'sigma':1.5}})
    report=core._certify(a,roots[order],vectors,cfg)
    assert report['target_certification']=='CERTIFIED',report
    assert report['certification_source']=='exact_dyadic_gapped_gershgorin_v1'
    assert report['previous_certification_limit']=='dimension_budget'
    bad=core._certify(a,roots[order],np.column_stack([vectors[:,0],vectors[:,0]]),cfg)
    assert bad['target_certification']=='UNVERIFIED'


def test_large_certifier_reaches_export_and_fresh_original_input_reverification(tmp_path):
    n=81
    low=sparse.csr_matrix([[1.,0.,.125],[0.,2.,0.],[.125,0.,3.]])
    high=sparse.diags([np.full(n-4,.01),np.full(n-3,20.),np.full(n-4,.01)],[-1,0,1],format='csr')
    a=sparse.block_diag([low,high],format='csr')
    source=tmp_path/'input.npz';sparse.save_npz(source,a)
    cfg=resolve_config({'target':{'k':2,'sigma':1.5},'solver':{'subspace_max':20}})
    result=core.run_solver(source,cfg,tmp_path/'run')
    assert result['status']=='SUCCEEDED',result['validation']
    assert result['validation']['certification_source']=='exact_dyadic_gapped_gershgorin_v1'
    checked=core.verify_run(source,tmp_path/'run')
    assert checked['numerical_status']=='PASSED'
    assert checked['target_certification']=='CERTIFIED'


def test_core_uses_weighted_proof_only_after_independent_uniform_geometry_refusal():
    n=81
    small=np.diag([1.,4.,4.,4.,4.])
    small[0,1:]=11/16;small[1:,0]=11/16
    a=sparse.block_diag([sparse.csr_matrix(small),sparse.eye(n-5)*20.],format='csr')
    roots,basis=np.linalg.eigh(small)
    vectors=np.zeros((n,1));vectors[:5,0]=basis[:,0]
    cfg=resolve_config({'target':{'k':1,'sigma':0.}})
    report=core._certify(a,roots[:1],vectors,cfg)
    assert report['target_certification']=='CERTIFIED',report
    assert report['certification_source']=='exact_dyadic_weighted_gershgorin_v1'
    assert report['preceding_certification']['limit']=='no_separated_nearest_disk_union'
    wrong=core._certify(a,np.array([0.]),vectors,cfg)
    assert wrong['target_certification']=='UNVERIFIED'
