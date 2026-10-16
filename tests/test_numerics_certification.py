from fractions import Fraction

import numpy as np
import pytest
from scipy import sparse

from emsolver.contracts import resolve_config


def cycle_matrix(n=8,complex_path=False):
    off=np.ones(n-1)*(1j if complex_path else 1.)
    a=sparse.diags([off.conjugate(),np.arange(1.,n+1),off],[-1,0,1],format='lil')
    a[0,n-1]=.25j if complex_path else .25
    a[n-1,0]=-.25j if complex_path else .25
    return a.tocsr()


@pytest.mark.parametrize('complex_path',[False,True])
def test_exact_sparse_inertia_matches_independent_small_dense_reference(complex_path):
    from emsolver.numerics.certification import InertiaOracle
    a=cycle_matrix(complex_path=complex_path)
    reference=np.linalg.eigvalsh(a.toarray())
    oracle=InertiaOracle(a)
    for point in (-2.,0.,2.5,5.5,10.):
        negative,zero,positive=oracle.inertia(Fraction.from_float(point))
        assert (negative,zero,positive)==(int(np.sum(reference<point)),0,int(np.sum(reference>point)))


def test_exact_inertia_handles_zero_diagonal_two_by_two_complex_pivot_and_nullity():
    from emsolver.numerics.certification import InertiaOracle
    a=sparse.block_diag([sparse.csr_matrix([[0.,1+2j],[1-2j,0.]]),sparse.csr_matrix(np.ones((3,3)))],format='csr')
    oracle=InertiaOracle(a)
    assert oracle.inertia(0)==(1,2,2)
    assert oracle.inertia(1)==(3,0,2)
    assert oracle.inertia(3)==(4,1,0)
    assert oracle.statistics()['two_by_two_pivots']>0


def test_exact_inertia_preserves_original_ieee_extremes_and_exact_endpoint_multiplicity():
    from emsolver.numerics.certification import InertiaOracle
    a=sparse.diags([-1e300,-1e-300,0.,1e-300,1e300],format='csr')
    oracle=InertiaOracle(a)
    assert oracle.inertia(0)==(2,1,2)
    assert oracle.inertia(Fraction.from_float(1e-300))==(3,1,1)
    assert oracle.count_interval(0.,1e-300)==1


@pytest.mark.parametrize('limits,reason',[
    ({'max_n':3},'dimension_budget'),({'max_nnz':3},'input_nonzero_budget'),
    ({'max_entries':3},'fill_budget'),({'max_bits':16},'bit_budget'),
    ({'max_updates':0},'operation_budget'),({'max_seconds':0},'time_budget'),
])
def test_exact_inertia_resource_limits_fail_closed(limits,reason):
    from emsolver.numerics.certification import InertiaOracle,CertificationBudget,CertificateUnavailable
    with pytest.raises(CertificateUnavailable,match=reason):
        InertiaOracle(cycle_matrix(),budget=CertificationBudget(**limits)).inertia(Fraction(1,3))


def test_exact_inertia_rejects_approximately_hermitian_input_without_rounding_it():
    from emsolver.numerics.certification import InertiaOracle,CertificateUnavailable
    a=cycle_matrix();a[0,1]+=1e-14
    with pytest.raises(CertificateUnavailable,match='exact_hermitian_required'):InertiaOracle(a)


@pytest.mark.parametrize('complex_path',[False,True])
def test_general_sparse_nearest_certificate_rejects_missing_and_wrong_neighborhood(complex_path):
    from emsolver.numerics.certification import certify_sparse
    a=cycle_matrix(complex_path=complex_path)
    roots,vectors=np.linalg.eigh(a.toarray())
    sigma=4.1;k=2
    order=np.lexsort((roots,np.abs(roots-sigma)))
    cfg=resolve_config({'target':{'k':k,'sigma':sigma}})
    good=certify_sparse(a,roots[order[:k]],vectors[:,order[:k]],cfg)
    assert good['target_certification']=='CERTIFIED'
    assert good['certification_source']=='bounded_exact_rational_inertia_v1'
    assert good['missing_count']==0
    assert certify_sparse(a,roots[order[:1]],vectors[:,order[:1]],cfg)['target_certification']=='UNVERIFIED'
    wrong=order[[0,3]]
    bad=certify_sparse(a,roots[wrong],vectors[:,wrong],cfg)
    assert bad['target_certification']=='UNVERIFIED'


def test_general_sparse_interval_count_is_independent_and_rejects_omitted_mode():
    from emsolver.numerics.certification import certify_sparse
    a=cycle_matrix()
    roots,vectors=np.linalg.eigh(a.toarray())
    indices=np.flatnonzero((roots>=1.5)&(roots<6.5))
    cfg=resolve_config({'target':{'mode':'interval_all','interval':{'lower':1.5,'upper':6.5}}})
    good=certify_sparse(a,roots[indices],vectors[:,indices],cfg)
    assert good['expected_count']==len(indices)
    assert good['target_certification']=='CERTIFIED'
    bad=certify_sparse(a,roots[indices[:-1]],vectors[:,indices[:-1]],cfg)
    assert bad['expected_count']==len(indices) and bad['missing_count']==1
    assert bad['target_certification']=='UNVERIFIED'


def test_general_sparse_certificate_does_not_accept_duplicate_or_wrong_eigenspaces():
    from emsolver.numerics.certification import certify_sparse
    a=cycle_matrix();roots,vectors=np.linalg.eigh(a.toarray())
    cfg=resolve_config({'target':{'k':2,'sigma':4.1}})
    order=np.lexsort((roots,np.abs(roots-4.1)))[:2]
    duplicate=np.column_stack([vectors[:,order[0]],vectors[:,order[0]]])
    assert certify_sparse(a,roots[order],duplicate,cfg)['target_certification']=='UNVERIFIED'
    assert certify_sparse(a,roots[order],vectors[:,order[::-1]],cfg)['target_certification']=='UNVERIFIED'


def test_general_sparse_certificate_preserves_small_mode_value_in_large_dynamic_range():
    from emsolver.numerics.certification import certify_sparse
    a=sparse.block_diag([cycle_matrix(4),sparse.diags([1e300])],format='csr')
    # This vector has a tiny scaled residual, but lambda=0 is not its eigenvalue.
    vector=np.array([[1.],[0.],[0.],[0.],[0.]])
    cfg=resolve_config({'target':{'k':1,'sigma':0.}})
    report=certify_sparse(a,np.array([0.]),vector,cfg)
    assert report['target_certification']=='UNVERIFIED'


def test_sparse_certificate_never_calls_candidate_solver_or_full_matrix_densification(monkeypatch):
    from emsolver.numerics.certification import certify_sparse
    a=cycle_matrix();roots,vectors=np.linalg.eigh(a.toarray())
    cfg=resolve_config({'target':{'k':2,'sigma':4.1}})
    order=np.lexsort((roots,np.abs(roots-4.1)))[:2]
    def forbidden(*args,**kwargs):raise AssertionError('independent certificate cannot use eigenvalue solver or densify')
    monkeypatch.setattr(sparse.csr_matrix,'toarray',forbidden)
    monkeypatch.setattr(sparse.csr_matrix,'todense',forbidden)
    monkeypatch.setattr(np.linalg,'eigh',forbidden)
    monkeypatch.setattr(np.linalg,'eigvalsh',forbidden)
    assert certify_sparse(a,roots[order],vectors[:,order],cfg)['target_certification']=='CERTIFIED'


def test_general_sparse_certificate_integrates_original_problem_export_and_fresh_revalidation(tmp_path):
    from emsolver.numerics import core
    a=cycle_matrix(12)
    source=tmp_path/'input.npz';sparse.save_npz(source,a)
    cfg=resolve_config({'target':{'k':3,'sigma':4.1},'resources':{'threads':1}})
    result=core.run_solver(source,cfg,tmp_path/'run')
    assert result['status']=='SUCCEEDED'
    assert result['validation']['certification_source']=='bounded_exact_rational_inertia_v1'
    assert core.verify_run(source,tmp_path/'run')['target_certification']=='CERTIFIED'


def test_sparse_empty_interval_and_exact_half_open_endpoints():
    from emsolver.numerics.certification import certify_sparse
    # A nontridiagonal permutation of a rational 2x2 block and isolated roots.
    a=sparse.csr_matrix([[0.,0.,1.,0.],[0.,2.,0.,0.],[1.,0.,0.,0.],[0.,0.,0.,3.]])
    empty=resolve_config({'target':{'mode':'interval_all','interval':{'lower':.1,'upper':.9}}})
    report=certify_sparse(a,np.empty(0),np.empty((4,0)),empty)
    assert report['target_certification']=='CERTIFIED' and report['expected_count']==0
    cfg=resolve_config({'target':{'mode':'interval_all','interval':{'lower':-1.,'upper':1.}}})
    x=np.array([[1.],[0.],[-1.],[0.]])
    assert certify_sparse(a,np.array([-1.]),x,cfg)['target_certification']=='CERTIFIED'
    assert certify_sparse(a,np.array([1.]),np.array([[1.],[0.],[1.],[0.]]),cfg)['target_certification']=='UNVERIFIED'


def test_sparse_certificate_reports_bounded_refusal_and_invokes_resource_callback():
    from emsolver.numerics.certification import certify_sparse,CertificationBudget
    a=cycle_matrix();roots,vectors=np.linalg.eigh(a.toarray())
    cfg=resolve_config({'target':{'k':2,'sigma':4.1}})
    order=np.lexsort((roots,np.abs(roots-4.1)))[:2]
    report=certify_sparse(a,roots[order],vectors[:,order],cfg,budget=CertificationBudget(max_inertias=1))
    assert report['target_certification']=='UNVERIFIED'
    assert report['certification_limit']=='inertia_evaluation_budget'
    def cancelled():raise RuntimeError('CANCELLED: independent certificate interrupted')
    with pytest.raises(RuntimeError,match='CANCELLED'):
        certify_sparse(a,roots[order],vectors[:,order],cfg,check=cancelled)
