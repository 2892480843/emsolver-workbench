"""Independent dense references exist only in these small synthetic tests."""
from dataclasses import replace
from fractions import Fraction

import numpy as np
import pytest
from scipy import sparse

from emsolver.contracts import resolve_config
from emsolver.numerics.independent_counts import GappedCountBudget, GappedCountOracle, certify_gapped
from emsolver.numerics.certification import CertificateUnavailable


def gapped(seed=1, complex_values=False, n=8):
    rng = np.random.default_rng(seed)
    upper = np.triu(rng.normal(size=(n, n)), 1) * .01
    if complex_values: upper = upper + 1j * np.triu(rng.normal(size=(n, n)), 1) * .01
    diagonal = np.array([-30., -20., -10., 1., 2., 3., 20., 30.])
    return sparse.csr_matrix(upper + upper.conj().T + np.diag(diagonal))


@pytest.mark.parametrize('seed', range(10))
@pytest.mark.parametrize('complex_values', [False, True])
def test_independent_gapped_counts_match_small_numpy_eigenvalues(seed, complex_values):
    a = gapped(seed, complex_values)
    roots = np.linalg.eigvalsh(a.toarray())
    oracle = GappedCountOracle(a)
    for point in (-35., -25., -15., -5., 0., 10., 25., 35.):
        expected = (int(np.sum(roots < point)), 0, int(np.sum(roots > point)))
        assert oracle.inertia(point) == expected
    assert oracle.count_interval(0., 4.) == 3


@pytest.mark.parametrize('complex_values', [False, True])
def test_nearest_and_interval_certification_include_independent_subspace_proof(complex_values):
    a = gapped(42, complex_values)
    roots, vectors = np.linalg.eigh(a.toarray())
    order = np.lexsort((roots, abs(roots - 1.4)))[:3]
    cfg = resolve_config({'target': {'k': 3, 'sigma': 1.4}})
    result = certify_gapped(a, roots[order], vectors[:, order], cfg)
    assert result['target_certification'] == 'CERTIFIED', result
    assert result['expected_count'] == 3 and result['missing_count'] == 0
    assert result['certification_source'] == 'exact_dyadic_gapped_gershgorin_v1'
    assert result['certification_window_count'] == 3
    cfg = resolve_config({'target': {'mode': 'interval_all', 'interval': {'lower': 0., 'upper': 4.}}})
    selected = np.flatnonzero((roots >= 0) & (roots < 4))
    assert certify_gapped(a, roots[selected], vectors[:, selected], cfg)['target_certification'] == 'CERTIFIED'


def test_wrong_neighborhood_omitted_mode_duplicate_direction_and_wrong_value_never_pass():
    a = gapped(); roots, vectors = np.linalg.eigh(a.toarray())
    cfg = resolve_config({'target': {'k': 3, 'sigma': 1.4}})
    order = np.lexsort((roots, abs(roots - 1.4)))[:3]
    wrong = np.array([3, 4, 6])
    wrong = wrong[np.lexsort((roots[wrong], abs(roots[wrong] - 1.4)))]
    for values, basis in [(roots[order[:2]], vectors[:, order[:2]]), (roots[wrong], vectors[:, wrong]),
        (roots[order], vectors[:, [order[0], order[0], order[2]]]),
        (roots[order] + .001, vectors[:, order]), (roots[order], vectors[:, order[::-1]])]:
        result = certify_gapped(a, values, basis, cfg)
        assert result['target_certification'] == 'UNVERIFIED', result


def test_disks_at_boundary_do_not_claim_floating_inertia_or_hidden_multiplicity():
    a = sparse.csr_matrix([[0., 1.], [1., 0.]])
    with pytest.raises(CertificateUnavailable, match='disk_crosses_count_point'): GappedCountOracle(a).inertia(0.)
    repeated = sparse.diags([0., 0., 10.], format='csr')
    cfg = resolve_config({'target': {'k': 1, 'sigma': 0.}})
    result = certify_gapped(repeated, np.array([0.]), np.array([[1.], [0.], [0.]]), cfg)
    assert result['target_certification'] == 'UNVERIFIED'
    assert result['certification_limit'] == 'too_many_disks_cover_target'


def test_exact_isolated_zeros_half_open_endpoints_and_empty_interval():
    a = sparse.diags([-2., 0., 0., 3.], format='csr')
    oracle = GappedCountOracle(a)
    assert oracle.inertia(Fraction(0)) == (1, 2, 1)
    assert oracle.count_interval(0., 3.) == 2
    cfg = resolve_config({'target': {'mode': 'interval_all', 'interval': {'lower': .5, 'upper': 2.}}})
    result = certify_gapped(a, np.empty(0), np.empty((4, 0)), cfg)
    assert result['target_certification'] == 'CERTIFIED' and result['expected_count'] == 0
    cfg = resolve_config({'target': {'mode': 'interval_all', 'interval': {'lower': 0., 'upper': 3.}}})
    result = certify_gapped(a, np.zeros(2), np.eye(4)[:, [1, 2]], cfg)
    assert result['target_certification'] == 'CERTIFIED', result


def test_large_nontridiagonal_synthetic_input_exceeds_old_dimension_cap():
    # Exact low block and a disjoint gapped band: no large eigensolve occurs.
    n = 100003
    low = sparse.csr_matrix([[1., 0., .125], [0., 2., 0.], [.125, 0., 3.]])
    high = sparse.diags([np.full(n - 4, .01), np.full(n - 3, 20.), np.full(n - 4, .01)], [-1, 0, 1], format='csr')
    a = sparse.block_diag([low, high], format='csr')
    roots, small = np.linalg.eigh(low.toarray())
    order = np.lexsort((roots, abs(roots - 1.5)))[:2]
    vectors = np.zeros((n, 2)); vectors[:3] = small[:, order]
    cfg = resolve_config({'target': {'k': 2, 'sigma': 1.5}})
    result = certify_gapped(a, roots[order], vectors, cfg)
    assert result['target_certification'] == 'CERTIFIED', result
    assert result['certification_arithmetic']['rows_processed'] == n
    assert result['certification_arithmetic']['arithmetic'] == 'exact_binary64_gaussian_rationals'


def test_input_is_not_silently_symmetrized_and_float_signs_do_not_prove_counts():
    a = gapped(); a[0, 1] += 1e-14
    with pytest.raises(CertificateUnavailable, match='exact_hermitian_required'): GappedCountOracle(a)
    with pytest.raises(CertificateUnavailable): GappedCountOracle(sparse.csr_matrix(np.eye(3, dtype=np.int64)))


def test_extreme_scale_cannot_authorize_a_wrong_small_eigenvalue():
    a = sparse.diags([1., 2., 1e300], format='csr')
    cfg = resolve_config({'target': {'k': 1, 'sigma': 0.}})
    result = certify_gapped(a, np.array([0.]), np.array([[1.], [0.], [0.]]), cfg)
    assert result['target_certification'] == 'UNVERIFIED'
    assert result['certification_limit'] == 'candidate_value_enclosure_too_wide'


@pytest.mark.parametrize('changes, reason', [({'max_n': 3}, 'dimension_budget'), ({'max_nnz': 3}, 'input_nonzero_budget'),
    ({'max_seconds': 0.}, 'time_budget'), ({'max_operations': 0}, 'operation_budget'), ({'max_bits': 2}, 'bit_budget')])
def test_all_resource_limits_fail_closed(changes, reason):
    with pytest.raises(CertificateUnavailable, match=reason): GappedCountOracle(gapped(), budget=replace(GappedCountBudget(), **changes))


def test_no_candidate_eigensolver_dense_matrix_or_floating_factorization(monkeypatch):
    a = gapped(); roots, vectors = np.linalg.eigh(a.toarray())
    order = np.lexsort((roots, abs(roots - 1.4)))[:3]
    def forbidden(*args, **kwargs): raise AssertionError('Proof cannot call eigensolver, factorization or full dense conversion')
    for name in ('toarray', 'todense'): monkeypatch.setattr(sparse.csr_matrix, name, forbidden)
    for name in ('eigh', 'eigvalsh', 'eig', 'eigvals'): monkeypatch.setattr(np.linalg, name, forbidden)
    from scipy.sparse import linalg
    for name in ('eigsh', 'eigs', 'splu', 'spilu'): monkeypatch.setattr(linalg, name, forbidden)
    cfg = resolve_config({'target': {'k': 3, 'sigma': 1.4}})
    assert certify_gapped(a, roots[order], vectors[:, order], cfg)['target_certification'] == 'CERTIFIED'


def test_cancellation_propagates_and_budget_parameters_are_strict():
    def cancelled(): raise RuntimeError('CANCELLED')
    with pytest.raises(RuntimeError, match='CANCELLED'): GappedCountOracle(gapped(), check=cancelled)
    for changes in ({'max_n': True}, {'max_seconds': float('nan')}, {'max_operations': 2.5}):
        with pytest.raises((ValueError, CertificateUnavailable)): GappedCountOracle(gapped(), budget=replace(GappedCountBudget(), **changes))


def test_degenerate_target_accepts_independent_nonorthogonal_basis_but_not_false_rank():
    a = sparse.diags([1., 1., 10., 20.], format='csr')
    cfg = resolve_config({'target': {'k': 2, 'sigma': 1.}})
    good = np.array([[1., .5], [0., 1.], [0., 0.], [0., 0.]])
    assert certify_gapped(a, np.ones(2), good, cfg)['target_certification'] == 'CERTIFIED'
    # Both candidate projections are the same target direction; arbitrarily
    # tiny residuals alone must not turn the exterior direction into coverage.
    epsilon = 1e-15
    wrong = np.array([[1., 1.], [0., 0.], [epsilon, -epsilon], [0., 0.]])
    result = certify_gapped(a, np.ones(2), wrong, cfg)
    assert result['target_certification'] == 'UNVERIFIED', result
    assert result['certification_limit'] == 'candidate_subspace_rank_unproved'


def test_csr_flags_cannot_override_actual_duplicate_structure():
    a = sparse.csr_matrix((np.array([1., 1., 4.]), np.array([0, 0, 1]), np.array([0, 2, 3])), shape=(2, 2))
    a.has_sorted_indices = True
    a.has_canonical_format = True
    with pytest.raises(CertificateUnavailable, match='invalid_canonical_ieee_input'): GappedCountOracle(a)


def test_whole_target_span_cannot_replace_candidate_eigenvalue_multiplicity():
    a = sparse.diags([1., 2., 100.], format='csr')
    epsilon = 2.**-44
    vectors = np.array([[1., 1.], [epsilon, -epsilon], [0., 0.]])
    cfg = resolve_config({'target': {'k': 2, 'sigma': 1.5}})
    result = certify_gapped(a, np.array([1., 1.]), vectors, cfg)
    assert result['target_certification'] == 'UNVERIFIED', result
    assert result['certification_limit'] == 'candidate_cluster_count_mismatch'


def test_principal_angle_is_checked_for_the_entire_ill_conditioned_basis():
    a = sparse.diags([1., 1., 100.], format='csr')
    epsilon = 2.**-48
    vectors = np.array([[1., 1.], [epsilon, -epsilon], [.5 * epsilon, -.5 * epsilon]])
    cfg = resolve_config({'target': {'k': 2, 'sigma': 1.}})
    result = certify_gapped(a, np.ones(2), vectors, cfg)
    assert result['target_certification'] == 'UNVERIFIED', result
    assert result['certification_limit'] == 'mode_subspace_not_verified'


def test_empty_certificate_checks_late_cancellation(monkeypatch):
    a = sparse.diags([1., 2., 3.], format='csr')
    cfg = resolve_config({'target': {'mode': 'interval_all', 'interval': {'lower': 10., 'upper': 20.}}})
    state = {'stop': False}
    original = GappedCountOracle.target_window
    def window(self, target):
        result = original(self, target); state['stop'] = True; return result
    def check():
        if state['stop']: raise RuntimeError('CANCELLED_AFTER_COUNT')
    monkeypatch.setattr(GappedCountOracle, 'target_window', window)
    with pytest.raises(RuntimeError, match='CANCELLED_AFTER_COUNT'):
        certify_gapped(a, np.empty(0), np.empty((3, 0)), cfg, check=check)


def test_vector_budget_is_checked_before_full_finite_scan(monkeypatch):
    a = sparse.diags([1., 2., 3.], format='csr')
    cfg = resolve_config({'target': {'k': 1, 'sigma': 0.}})
    def forbidden(*args, **kwargs): raise AssertionError('Must reject before scanning arrays')
    monkeypatch.setattr(np, 'isfinite', forbidden)
    result = certify_gapped(a, np.ones(1), np.ones((3, 1)), cfg,
        budget=replace(GappedCountBudget(), max_vector_entries=2))
    assert result['certification_limit'] == 'candidate_resource_budget'
