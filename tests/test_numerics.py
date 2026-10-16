import json
from pathlib import Path

import numpy as np
import pytest
from scipy import sparse

from emsolver.numerics import load_matrix, inspect_matrix, run_solver, verify_run, generate_sample, capabilities


def cfg(k=3, sigma=0.0, **target):
    return {'target': {'k': k, 'sigma': sigma, **target}}


def save(tmp_path, a, name='a.npz'):
    p = tmp_path / name
    sparse.save_npz(p, sparse.csr_matrix(a))
    return p


def test_triangle_complex_restores_conjugate_and_rejects_wrong_half(tmp_path):
    a = sparse.csr_matrix([[1, 2j], [0, -2]], dtype=complex)
    p = save(tmp_path, a)
    actual = load_matrix(p, storage='upper')
    assert actual[1, 0] == -2j
    with pytest.raises(ValueError, match='TRIANGLE'):
        load_matrix(p, storage='lower')
    with pytest.raises(ValueError, match='HERMITIAN'):
        load_matrix(p)


def test_safe_numpy_and_structure_validation(tmp_path):
    obj = tmp_path/'obj.npy'
    np.save(obj, np.array([{'x': 1}], dtype=object))
    with pytest.raises(ValueError):
        load_matrix(obj)
    bad = save(tmp_path, sparse.diags([1, np.nan]))
    with pytest.raises(ValueError, match='FINITE'):
        load_matrix(bad)
    good = tmp_path/'good.npy'
    np.save(good, np.diag([-2., 0., 3.]))
    m = load_matrix(good)
    assert inspect_matrix(m)['n'] == 3
    assert len(inspect_matrix(m)['preview']) <= 1500


def test_diagonal_certifies_original_target_and_revalidates_artifacts(tmp_path):
    p = save(tmp_path, sparse.diags([-7., -1., 0., 0.2, 3., 8.]))
    out = tmp_path/'run'
    result = run_solver(p, cfg(), out)
    assert result['status'] == 'SUCCEEDED'
    assert result['eigenvalues'] == [0.0, 0.2, -1.0]
    assert max(result['residuals']) < 1e-12
    assert verify_run(p, out)['numerical_status'] == 'PASSED'
    assert json.loads((out/'manifest.json').read_text())['status'] == 'SUCCEEDED'
    x = np.load(out/'eigenvectors.npy')
    x[:, 0] = 0
    np.save(out/'eigenvectors.npy', x)
    assert verify_run(p, out)['numerical_status'] == 'FAILED'


def test_bounded_generic_sparse_target_is_independently_certified(tmp_path):
    a = sparse.diags([np.ones(19), np.linspace(-2, 3, 20), np.ones(19)], [-1, 0, 1])
    a = a+sparse.coo_matrix(([0.2, 0.2], ([0, 3], [3, 0])), shape=(20, 20))
    p = save(tmp_path, a)
    result = run_solver(p, cfg(k=4, sigma=0.1), tmp_path/'run')
    assert result['status'] == 'SUCCEEDED'
    assert result['n_accepted'] == 4
    assert result['validation']['numerical_status'] == 'PASSED'
    assert result['validation']['target_certification'] == 'CERTIFIED'
    assert result['validation']['certification_source']=='bounded_exact_rational_inertia_v1'
    assert max(result['residuals']) < 1e-8


def test_repeated_eigenvalues_preserve_independent_modes_and_boundary(tmp_path):
    p = save(tmp_path, sparse.diags([-3., 0., 0., 0., 4., 8.]))
    fixed = run_solver(p, cfg(k=2), tmp_path/'fixed')
    assert fixed['status'] == 'PARTIAL'
    assert fixed['validation']['boundary_cluster_cut'] is True
    full = run_solver(p, cfg(k=2, cluster_policy='cluster_complete'), tmp_path/'full')
    assert full['status'] == 'SUCCEEDED'
    assert full['n_accepted'] == 3
    x = np.load(tmp_path/'full/eigenvectors.npy')
    assert np.linalg.norm(x.T@x-np.eye(3)) < 1e-12


def test_interval_empty_and_capped_diagonal(tmp_path):
    p = save(tmp_path, sparse.diags([-2., -1., 0., 1., 2., 3.]))
    target = {'mode':'interval_all', 'k':None, 'sigma':None, 'interval':{'lower':0.1,'upper':0.9}, 'max_modes':3}
    empty = run_solver(p, {'target':target}, tmp_path/'empty')
    assert empty['status'] == 'SUCCEEDED'
    assert empty['n_accepted'] == 0
    target['interval'] = {'lower':-1., 'upper':3.}
    target['max_modes'] = 2
    capped = run_solver(p, {'target':target}, tmp_path/'capped')
    assert capped['status'] == 'PARTIAL'
    assert capped['n_accepted'] == 2


def test_cancellation_and_unsupported_backend_are_honest(tmp_path):
    p = save(tmp_path, sparse.eye(10))
    with pytest.raises(InterruptedError):
        run_solver(p, cfg(), tmp_path/'cancel', cancel_check=lambda: True)
    with pytest.raises((ValueError, RuntimeError), match='BACKEND'):
        run_solver(p, {'solver': {'backend':'slepc'}}, tmp_path/'unavailable')
    from emsolver.numerics.gpu import probe_metal
    assert capabilities()['gpu'] == probe_metal()


def test_sample_sparse_and_numpy_exports(tmp_path):
    p = tmp_path/'sample.npz'
    meta = generate_sample(p, kind='complex', n=50)
    assert meta['source'] == 'synthetic'
    assert np.iscomplexobj(load_matrix(p).data)
    result = run_solver(p, {**cfg(), 'output':{'format':'both'}}, tmp_path/'run')
    assert result['n_accepted'] == 3
    assert (tmp_path/'run/eigenvectors.scipy.npz').is_file()


def test_safe_header_rejects_false_allocation_and_unknown_config_behavior(tmp_path):
    import io, zipfile
    header = io.BytesIO()
    np.lib.format.write_array_header_1_0(header, {'descr':'<f8', 'fortran_order':False, 'shape':(1000000000000,)})
    with zipfile.ZipFile(tmp_path/'forged.npz', 'w') as z:
        z.writestr('data.npy', header.getvalue())
    with pytest.raises(ValueError, match='INPUT_BUDGET|INVALID_ARCHIVE'):
        load_matrix(tmp_path/'forged.npz', max_bytes=1000000)
    p = save(tmp_path, sparse.diags(np.arange(10.)))
    with pytest.raises(ValueError, match='UNSUPPORTED_TRANSFORM'):
        run_solver(p, {**cfg(), 'solver':{'transform':'none'}}, tmp_path/'transform')


def test_singular_shift_retries_and_does_not_change_target(tmp_path):
    # A disconnected zero mode makes sigma=0 singular, while the matrix is not diagonal.
    a = sparse.block_diag((sparse.csr_matrix([[0.]]), sparse.diags([np.ones(18), np.linspace(1.,5.,19), np.ones(18)], [-1,0,1])), format='csr')
    p = save(tmp_path, a)
    events = []
    result = run_solver(p, cfg(k=3, sigma=0.), tmp_path/'shift', event_callback=events.append)
    assert result['status'] == 'SUCCEEDED'
    assert len(result['attempts']) >= 2
    assert result['attempts'][0]['status'] == 'FAILED'
    assert all(a['sigma_target'] == 0 for a in result['attempts'])
    assert result['attempts'][1]['sigma_work'] != 0
    assert max(result['residuals']) <= 1e-8
    assert any(e['event'] == 'shift_failed' for e in events)


def test_no_sparse_dense_conversion_even_for_general_complex(tmp_path, monkeypatch):
    p = tmp_path/'complex.npz'
    generate_sample(p, kind='complex', n=80)
    def forbidden(*args, **kwargs):
        raise AssertionError('original sparse matrix was densified')
    monkeypatch.setattr(sparse.csr_matrix, 'toarray', forbidden)
    monkeypatch.setattr(sparse.csc_matrix, 'toarray', forbidden)
    monkeypatch.setattr(sparse.csr_matrix, 'todense', forbidden)
    result = run_solver(p, cfg(k=6), tmp_path/'run')
    assert result['n_accepted'] == 6
    assert result['validation']['numerical_status'] == 'PASSED'


def test_tiny_scale_residual_and_nonhermitian_diagonal_are_checked(tmp_path):
    p = save(tmp_path, sparse.diags(np.array([-3., 0., 1., 2., 5., 9.])*1e-250))
    result = run_solver(p, cfg(k=2, sigma=1.1e-250), tmp_path/'tiny')
    assert result['status'] == 'SUCCEEDED'
    assert result['eigenvalues'][0] == 1e-250
    p = save(tmp_path, sparse.diags(np.array([1.,2.,3.,4.])+1e-4j), 'imaginary.npz')
    with pytest.raises(ValueError, match='HERMITIAN'):
        load_matrix(p, storage='upper')


def test_malformed_sparse_structure_rejected_without_constructing(tmp_path):
    p = tmp_path/'bad.npz'
    np.savez(p, format=np.array(b'csr'), shape=np.array([4,4]), data=np.ones(2), indices=np.array([0,8]), indptr=np.array([0,1,1,1,2]))
    with pytest.raises(ValueError, match='INVALID_INDICES'):
        load_matrix(p)


def test_hundred_thousand_diagonal_case_actual_cpu(tmp_path):
    p = tmp_path/'large.npz'
    generate_sample(p, kind='near_singular', n=100000)
    result = run_solver(p, cfg(k=12), tmp_path/'large')
    assert result['status'] == 'SUCCEEDED'
    assert result['metrics']['n'] == 100000
    assert result['metrics']['peak_memory_bytes'] > 0
    assert result['n_accepted'] == 12
    assert max(result['residuals']) < 1e-15


def test_subnormal_hermitian_scale_does_not_underflow_or_accept_asymmetry(tmp_path):
    a = sparse.csr_matrix([[1e-320, 1e-320], [0., 2e-320]])
    p = save(tmp_path, a, 'bad-small.npz')
    with pytest.raises(ValueError, match='HERMITIAN'):
        load_matrix(p)
    p = save(tmp_path, sparse.diags(np.array([-3., 0., 1., 2., 5., 9.])*1e-320), 'good-small.npz')
    result = run_solver(p, cfg(k=2, sigma=1.1e-320), tmp_path/'subnormal')
    assert result['status'] == 'SUCCEEDED'
    assert all(np.isfinite(result['residuals']))


def test_general_interval_exploration_uses_bounded_candidates(tmp_path):
    p = tmp_path/'interval.npz'
    generate_sample(p, kind='complex', n=30)
    a = sparse.load_npz(p)+sparse.coo_matrix(([0.2, 0.2], ([0, 3], [3, 0])), shape=(30, 30))
    sparse.save_npz(p, a.tocsr())
    result = run_solver(p, {'target':{'mode':'interval_all','interval':{'lower':-1., 'upper':1.}}}, tmp_path/'explore')
    assert result['status'] == 'SUCCEEDED'
    assert 0 < result['n_accepted'] <= 28
    assert all(-1 <= value < 1 for value in result['eigenvalues'])


def test_arpack_partial_convergence_is_preserved_and_validated(tmp_path, monkeypatch):
    from scipy.sparse import linalg as sla
    from emsolver.numerics import core
    p = tmp_path/'partial.npz'
    generate_sample(p, n=40)
    actual = core.sla.eigsh
    def partial(*args, **kwargs):
        vals, vecs = actual(*args, **kwargs)
        raise sla.ArpackNoConvergence('controlled partial boundary', vals[:2], vecs[:, :2])
    monkeypatch.setattr(core.sla, 'eigsh', partial)
    result = run_solver(p, {**cfg(k=4), 'solver':{'adaptive_shift':{'enabled':False}}}, tmp_path/'partial')
    assert result['status'] == 'PARTIAL'
    assert result['n_accepted'] == 2
    assert result['attempts'][0]['status'] == 'PARTIAL_CONVERGENCE'
    assert result['validation']['numerical_status'] == 'PASSED'


def test_complex_imaginary_candidates_rejected_before_real_projection(tmp_path, monkeypatch):
    from emsolver.numerics import core
    p = tmp_path/'complex-bad.npz'
    generate_sample(p, kind='complex', n=40)
    actual = core.sla.eigs
    def with_bad_imaginary(*args, **kwargs):
        vals, vecs = actual(*args, **kwargs)
        return vals+1j, vecs
    monkeypatch.setattr(core.sla, 'eigs', with_bad_imaginary)
    result = run_solver(p, {**cfg(k=4), 'solver':{'adaptive_shift':{'enabled':False}}}, tmp_path/'imaginary')
    assert result['status'] == 'FAILED'
    assert result['n_accepted'] == 0
    assert result['attempts'][0]['imaginary_rejected'] > 0


def test_budget_guard_prevents_diagonal_vector_allocation(tmp_path):
    p = save(tmp_path, sparse.diags(np.arange(100.)))
    with pytest.raises(RuntimeError, match='RESOURCE_EXHAUSTED'):
        run_solver(p, {**cfg(), 'resources':{'memory_limit_gib':1e-8}}, tmp_path/'budget')
    assert not (tmp_path/'budget/manifest.json').exists()


def test_cancellation_during_arpack_publishes_no_manifest(tmp_path):
    p = tmp_path/'cancel-running.npz'
    generate_sample(p, n=100)
    state = {'cancel':False}
    def event(item):
        if item['event'] == 'arpack_started':
            state['cancel'] = True
    with pytest.raises(InterruptedError):
        run_solver(p, cfg(), tmp_path/'cancel-running', event_callback=event, cancel_check=lambda:state['cancel'])
    assert not (tmp_path/'cancel-running/manifest.json').exists()


def test_structural_tridiagonal_certifies_real_and_complex_without_dense_reference(tmp_path):
    for kind in ('indefinite','complex'):
        p = tmp_path/f'{kind}.npz'
        generate_sample(p, kind=kind, n=80)
        result = run_solver(p, cfg(k=6), tmp_path/f'{kind}-certified')
        assert result['status'] == 'SUCCEEDED'
        assert result['validation']['certification_source'] == 'tridiagonal_sturm_lapack_stebz'
        assert result['validation']['target_certification'] == 'CERTIFIED'
        assert verify_run(p, tmp_path/f'{kind}-certified')['artifact_integrity'] == 'PASSED'


def test_tridiagonal_interval_endpoint_and_repeated_cluster_rules(tmp_path):
    # Twenty identical independent 2x2 blocks have roots -1 and +1, both repeated.
    p = save(tmp_path, sparse.block_diag([sparse.csr_matrix([[0.,1.],[1.,0.]])]*20))
    result = run_solver(p, cfg(k=3,sigma=0.9), tmp_path/'repeat-tri')
    assert result['status'] == 'PARTIAL'
    assert result['validation']['boundary_cluster_cut'] is True
    target={'mode':'interval_all','interval':{'lower':-1.,'upper':1.,'boundary_atol':1e-9},'max_modes':30}
    result = run_solver(p, {'target':target}, tmp_path/'boundary-tri')
    assert result['status'] == 'PARTIAL'
    assert result['validation']['endpoint_ambiguous'] is True


def test_coo_shape_budget_checked_before_row_pointer_allocation(tmp_path, monkeypatch):
    p = tmp_path/'huge-shape.npz'
    np.savez(p, format=np.array(b'coo'), shape=np.array([10**12,10**12]), data=np.ones(1), row=np.array([0]), col=np.array([0]))
    def allocation_trap(*args, **kwargs):
        raise RuntimeError('unsafe CSR row-pointer allocation reached')
    monkeypatch.setattr(sparse.coo_matrix, 'tocsr', allocation_trap)
    with pytest.raises(ValueError, match='INPUT_BUDGET'):
        load_matrix(p, max_bytes=1000000)


@pytest.mark.parametrize('diagonal,sigma,expected', [
    ([0.,1.,2.,3.],1e20,[3.]),
    ([0.,1.,2.,3.],-1e20,[0.]),
    ([-1e308,-1e307,1e307,1e308],1e308,[1e308]),
    ([-1e308,-1e307,1e307,1e308],-1e308,[-1e308]),
    ([-1e308,-1e307,1e307,1e308],0.,[-1e307]),
])
def test_far_shift_and_extreme_scale_diagonal_order_is_certified_exactly(tmp_path, diagonal, sigma, expected):
    p = save(tmp_path,sparse.diags(diagonal))
    result = run_solver(p,cfg(k=1,sigma=sigma),tmp_path/'far')
    assert result['eigenvalues'] == expected
    assert result['status'] == 'SUCCEEDED'
    assert verify_run(p,tmp_path/'far')['target_certification'] == 'CERTIFIED'


def test_original_distance_sort_exactly_resolves_rounded_ties_and_overflow():
    from fractions import Fraction
    from emsolver.numerics.core import _target_order
    fixtures=[
       (np.array([-1.,0.,1.,2.]),1e20),
       (np.array([-1e308,-1e307,1e307,1e308]),3e307),
       (np.array([-1.7e308,-1.6e308,-1.3e308,1.,1.6e308,1.7e308]),1.5e308),
       (np.array([-1e308,0.,1.,1e308]),0.25),
       (np.array([0.,1.,2.,3.,4.]),2.),
       (np.array([np.nextafter(-1.,-np.inf),-1.,1.,np.nextafter(1.,np.inf)]),np.nextafter(0.,1.)),
    ]
    for values,sigma in fixtures:
        origin=Fraction.from_float(sigma)
        expected=sorted(range(len(values)),key=lambda i:(abs(Fraction.from_float(float(values[i]))-origin),float(values[i])))
        assert list(_target_order(values,sigma)) == expected


def test_diagonal_certification_catches_wrong_sort_even_if_solver_order_is_broken(tmp_path,monkeypatch):
    from emsolver.numerics import core
    from emsolver.contracts import resolve_config
    a=sparse.diags([0.,1.,2.,3.],format='csr')
    monkeypatch.setattr(core,'_target_order',lambda values,sigma:np.arange(len(values)))
    result=core._certify(a,np.array([0.]),np.array([[1.],[0.],[0.],[0.]]),resolve_config(cfg(k=1,sigma=1e20)))
    assert result['target_certification'] == 'UNVERIFIED'


def test_target_distance_order_matches_exact_reference_across_binary_scales():
    from fractions import Fraction
    from emsolver.numerics.core import _target_order
    rng=np.random.default_rng(424242)
    for _ in range(60):
        values=np.ldexp(rng.uniform(-1.,1.,19),rng.integers(-1070,1024,19))
        sigma=float(np.ldexp(rng.uniform(-1.,1.),int(rng.integers(-1070,1024))))
        origin=Fraction.from_float(sigma)
        expected=sorted(range(len(values)),key=lambda i:(abs(Fraction.from_float(float(values[i]))-origin),float(values[i])))
        assert list(_target_order(values,sigma)) == expected


@pytest.mark.parametrize('neighbor',[2.5e-13,3e-13,4e-13])
def test_tridiagonal_wrong_neighbor_cannot_be_certified_with_wide_match_tolerance(neighbor):
    from emsolver.numerics import core
    from emsolver.contracts import resolve_config
    a=sparse.block_diag((sparse.csr_matrix([[0.]]),sparse.csr_matrix([[neighbor]]),sparse.csr_matrix([[1.,0.01],[0.01,2.]])),format='csr')
    config=resolve_config(cfg(k=1,sigma=0.))
    vectors=np.array([[0.],[1.],[0.],[0.]])
    validation,_=core._validate(a,np.array([neighbor]),vectors,config)
    assert validation['numerical_status']=='PASSED'
    certification=core._certify(a,np.array([neighbor]),vectors,config)
    assert certification['target_certification']=='UNVERIFIED'


def test_tiny_absolute_residual_does_not_underflow_into_passing_validation():
    from emsolver.numerics import core
    from emsolver.contracts import resolve_config
    config=resolve_config({'validation':{'absolute_residual_max':1e-210}})
    a=sparse.diags([1e-200,1.],format='csr')
    validation,_=core._validate(a,np.array([0.]),np.array([[1.],[0.]]),config)
    assert validation['absolute_residuals_2'][0]==pytest.approx(1e-200,rel=1e-15,abs=0)
    assert validation['numerical_status']=='FAILED'


def test_tridiagonal_correct_but_ambiguous_neighbor_remains_unverified():
    from emsolver.numerics import core
    from emsolver.contracts import resolve_config
    a=sparse.block_diag((sparse.csr_matrix([[0.]]),sparse.csr_matrix([[3e-13]]),sparse.csr_matrix([[1.,0.01],[0.01,2.]])),format='csr')
    config=resolve_config(cfg(k=1,sigma=0.))
    report=core._certify(a,np.array([0.]),np.array([[1.],[0.],[0.],[0.]]),config)
    assert report['target_certification']=='UNVERIFIED'
    assert report['certification_limit']=='unresolved_neighboring_modes'


@pytest.mark.parametrize('scale,phase',[(1e308,1.),(1.,1j)])
def test_absolute_residual_preserves_original_units_and_complex_phase(scale,phase):
    from emsolver.numerics import core
    from emsolver.contracts import resolve_config
    config=resolve_config({'validation':{'absolute_residual_max':1e-210}})
    a=sparse.diags([1e-200,scale],format='csr')
    report,_=core._validate(a,np.array([0.]),np.array([[phase],[0.]]),config)
    assert report['absolute_residuals_2'][0]==pytest.approx(1e-200,rel=1e-15,abs=0)
    assert report['numerical_status']=='FAILED'


def test_tridiagonal_interval_does_not_certify_missing_repeated_mode():
    from emsolver.numerics import core
    from emsolver.contracts import resolve_config
    a=sparse.block_diag((sparse.csr_matrix([[0.]]),sparse.csr_matrix([[0.]]),sparse.csr_matrix([[1.,0.01],[0.01,2.]])),format='csr')
    config=resolve_config({'target':{'mode':'interval_all','interval':{'lower':-0.5,'upper':0.5}}})
    report=core._certify(a,np.array([0.]),np.array([[1.],[0.],[0.],[0.]]),config)
    assert report['target_certification']=='UNVERIFIED'
    assert report['missing_count']==1


def test_complex_subnormal_nonzero_vectors_normalize_without_nan():
    from emsolver.numerics import core
    from emsolver.contracts import resolve_config
    vector=np.array([[1e-320+1e-320j],[0.]],dtype=complex)
    report,normalized=core._validate(sparse.diags([1.,2.],format='csr'),np.array([1.]),vector,resolve_config({}))
    assert np.isfinite(normalized).all()
    assert report['numerical_status']=='PASSED'
    assert normalized[0,0]==pytest.approx((1+1j)/np.sqrt(2))


def test_tridiagonal_nearest_distance_tie_with_reference_uncertainty_is_unverified(monkeypatch):
    from emsolver.numerics import core
    from emsolver.contracts import resolve_config
    # A valid tiny perturbation in the independent reference must not reverse
    # the exact distance-then-value choice between the true roots -1 and +1.
    a=sparse.csr_matrix([[0.,1.],[1.,0.]])
    monkeypatch.setattr(core,'eigvalsh_tridiagonal',lambda *args,**kwargs:np.array([-1.,1.-1e-15]))
    wrong_positive=np.array([[1.],[1.]])/np.sqrt(2)
    report=core._certify(a,np.array([1.]),wrong_positive,resolve_config(cfg(k=1,sigma=0.)))
    assert report['target_certification']=='UNVERIFIED'


@pytest.mark.parametrize('neighbor',[1e-10,1e-9,1e-8])
def test_tridiagonal_correct_value_with_wrong_neighbor_vector_is_not_certified(neighbor):
    from emsolver.numerics import core
    from emsolver.contracts import resolve_config
    a=sparse.block_diag((sparse.csr_matrix([[0.]]),sparse.csr_matrix([[neighbor]]),sparse.csr_matrix([[1.,0.01],[0.01,2.]])),format='csr')
    config=resolve_config(cfg(k=1,sigma=0.))
    wrong_vector=np.array([[0.],[1.],[0.],[0.]])
    numerical,_=core._validate(a,np.array([0.]),wrong_vector,config)
    assert numerical['numerical_status']=='PASSED'
    report=core._certify(a,np.array([0.]),wrong_vector,config)
    assert report['target_certification']=='UNVERIFIED'
