"""Synthetic proof tests; no reference data enters production certification."""
from dataclasses import replace
import hashlib

import numpy as np
import pytest
from scipy import sparse

from emsolver.contracts import resolve_config
from emsolver.numerics.certification import CertificateUnavailable
from emsolver.numerics.independent_counts import (GappedCountBudget, GappedCountOracle,
    certify_gapped, certify_weighted)


def star(complex_values=False):
    a = np.diag([1., 4., 4., 4., 4.]).astype(np.complex128 if complex_values else np.float64)
    a[0, 1:] = 11. / 16 * (1j if complex_values else 1.)
    a[1:, 0] = a[0, 1:].conjugate()
    return sparse.csr_matrix(a)


@pytest.mark.parametrize('complex_values', [False, True])
def test_weighted_star_proves_new_family_uniform_cannot_separate(complex_values):
    a = star(complex_values)
    roots, vectors = np.linalg.eigh(a.toarray())
    cfg = resolve_config({'target': {'k': 1, 'sigma': 0.}})
    uniform = certify_gapped(a, roots[:1], vectors[:, :1], cfg)
    assert uniform['target_certification'] == 'UNVERIFIED'
    assert uniform['certification_limit'] == 'no_separated_nearest_disk_union'
    result = certify_weighted(a, roots[:1], vectors[:, :1], cfg)
    assert result['target_certification'] == 'CERTIFIED', result
    assert result['certification_source'] == 'exact_dyadic_weighted_gershgorin_v1'
    arithmetic = result['certification_arithmetic']
    assert arithmetic['weight_policy'] == 'row_component_inverse_sqrt_power2_v1'
    assert arithmetic['weight_exponent_min'] == -1 and arithmetic['weight_exponent_max'] == 0
    assert arithmetic['weight_exponents_sha256'] == hashlib.sha256(np.array([0, -1, -1, -1, -1], dtype='<i2').tobytes()).hexdigest()
    assert 'weight_exponents' not in arithmetic
    oracle = GappedCountOracle(a, weight_policy='row_component_inverse_sqrt_power2_v1')
    assert oracle.count_closed(-2.5, 2.5) == 1
    assert oracle.matrix is a  # Counts change; original residual operator does not.


@pytest.mark.parametrize('seed', range(10))
@pytest.mark.parametrize('complex_values', [False, True])
def test_weighted_counts_match_independent_small_dense_eigenvalue_oracle(seed, complex_values):
    rng = np.random.default_rng(seed)
    upper = np.triu(rng.normal(size=(6, 6)), 1) * .005
    if complex_values: upper = upper + 1j * np.triu(rng.normal(size=(6, 6)), 1) * .005
    a = sparse.csr_matrix(upper + upper.conj().T + np.diag([-100., -20., 1., 4., 30., 60.]))
    expected = np.linalg.eigvalsh(a.toarray())
    oracle = GappedCountOracle(a, weight_policy='row_component_inverse_sqrt_power2_v1')
    for point in (-200., -50., -5., 0., 10., 50., 200.):
        assert oracle.inertia(point) == (int(np.sum(expected < point)), 0, int(np.sum(expected > point)))


def test_weighted_path_reuses_association_multiplicity_and_vector_checks():
    a = sparse.diags([1., 2., 100.], format='csr')
    cfg = resolve_config({'target': {'k': 2, 'sigma': 1.5}})
    epsilon = 2.**-44
    repeated = np.array([[1., 1.], [epsilon, -epsilon], [0., 0.]])
    result = certify_weighted(a, np.ones(2), repeated, cfg)
    assert result['target_certification'] == 'UNVERIFIED'
    assert result['certification_limit'] == 'candidate_cluster_count_mismatch'
    for values, vectors in [(np.array([1.]), np.eye(3)[:, :1]),
        (np.array([1., 2.]), np.eye(3)[:, [1, 0]]), (np.array([1., 1.5]), np.eye(3)[:, :2])]:
        assert certify_weighted(a, values, vectors, cfg)['target_certification'] == 'UNVERIFIED'


def test_weight_generation_preserves_extreme_ieee_exponents_without_float_scaling():
    tiny = np.nextafter(0., 1.)
    a = sparse.diags([-np.finfo(float).max, tiny, 1.], format='csr')
    oracle = GappedCountOracle(a, weight_policy='row_component_inverse_sqrt_power2_v1')
    assert oracle.inertia(0.) == (1, 0, 2)
    info = oracle.statistics()
    assert info['weight_exponent_min'] == -511 and info['weight_exponent_max'] == 537
    cfg = resolve_config({'target': {'k': 1, 'sigma': 0.}})
    assert certify_weighted(a, np.array([tiny]), np.eye(3)[:, 1:2], cfg)['target_certification'] == 'CERTIFIED'


def test_zero_rows_do_not_produce_nonfinite_weights_or_hide_multiplicity():
    a = sparse.diags([0., 0., 4.], format='csr')
    oracle = GappedCountOracle(a, weight_policy='row_component_inverse_sqrt_power2_v1')
    assert oracle.inertia(0.) == (0, 2, 1)
    cfg = resolve_config({'target': {'k': 1, 'sigma': 0.}})
    assert certify_weighted(a, np.zeros(1), np.eye(3)[:, :1], cfg)['target_certification'] == 'UNVERIFIED'


@pytest.mark.parametrize('changes,reason', [({'max_operations': 0}, 'operation_budget'),
    ({'max_seconds': 0.}, 'time_budget'), ({'max_nnz': 2}, 'input_nonzero_budget'), ({'max_n': 2}, 'dimension_budget')])
def test_weight_prepass_budgets_fail_closed(changes, reason):
    cfg = resolve_config({'target': {'k': 1, 'sigma': 0.}})
    result = certify_weighted(star(), np.ones(1), np.eye(5)[:, :1], cfg, budget=replace(GappedCountBudget(), **changes))
    assert result['target_certification'] == 'UNVERIFIED'
    assert result['certification_limit'] == reason


def test_weight_prepass_propagates_cancellation_and_validates_policy():
    def cancelled(): raise RuntimeError('CANCELLED_WEIGHT_PREPASS')
    with pytest.raises(RuntimeError, match='CANCELLED_WEIGHT_PREPASS'):
        GappedCountOracle(star(), weight_policy='row_component_inverse_sqrt_power2_v1', check=cancelled)
    cfg = resolve_config({'target': {'k': 1, 'sigma': 0.}})
    result = certify_gapped(star(), np.ones(1), np.eye(5)[:, :1], cfg, weight_policy='untrusted_formula')
    assert result['target_certification'] == 'UNVERIFIED' and result['certification_limit'] == 'unsupported_weight_policy'


def test_weight_generation_never_solves_or_forms_scaled_full_matrix(monkeypatch):
    a = star(); roots, vectors = np.linalg.eigh(a.toarray())
    def forbidden(*args, **kwargs): raise AssertionError('Weights must depend only on bounded original row components')
    for name in ('toarray', 'todense', 'multiply'): monkeypatch.setattr(sparse.csr_matrix, name, forbidden)
    for name in ('eigh', 'eigvalsh', 'eig', 'eigvals', 'solve'): monkeypatch.setattr(np.linalg, name, forbidden)
    from scipy.sparse import linalg
    for name in ('eigsh', 'eigs', 'splu', 'spilu', 'spsolve'): monkeypatch.setattr(linalg, name, forbidden)
    cfg = resolve_config({'target': {'k': 1, 'sigma': 0.}})
    assert certify_weighted(a, roots[:1], vectors[:, :1], cfg)['target_certification'] == 'CERTIFIED'


def test_weighted_extension_reaches_beyond_old_dense_exact_dimension():
    a = np.diag([1.] + [4.] * 80)
    a[0, 1:] = a[1:, 0] = 1. / 20
    a = sparse.csr_matrix(a)
    values, vectors = np.linalg.eigh(a.toarray())
    cfg = resolve_config({'target': {'k': 1, 'sigma': 0.}})
    assert certify_gapped(a, values[:1], vectors[:, :1], cfg)['target_certification'] == 'UNVERIFIED'
    assert certify_weighted(a, values[:1], vectors[:, :1], cfg)['target_certification'] == 'CERTIFIED'


@pytest.mark.parametrize('dtype', [np.float32, np.float64, np.complex64, np.complex128])
def test_weighted_interval_preserves_exact_zero_and_half_open_endpoints(dtype):
    a = sparse.diags([-4., 0., 0., 1., 16.], format='csr', dtype=dtype)
    cfg = resolve_config({'target': {'mode': 'interval_all', 'interval': {'lower': 0., 'upper': 1.}}})
    proof = certify_weighted(a, np.zeros(2, dtype=dtype), np.eye(5, dtype=dtype)[:, 1:3], cfg)
    assert proof['target_certification'] == 'CERTIFIED' and proof['expected_count'] == 2
    assert certify_weighted(a, np.zeros(1, dtype=dtype), np.eye(5, dtype=dtype)[:, 1:2], cfg)['target_certification'] == 'UNVERIFIED'


def test_extreme_nonzero_weight_ratios_stay_exact_without_underflow():
    tiny = np.nextafter(0., 1.)
    huge = np.finfo(float).max
    a = sparse.csr_matrix(np.array([[huge, tiny], [tiny, -tiny]]))
    oracle = GappedCountOracle(a, weight_policy='row_component_inverse_sqrt_power2_v1')
    from fractions import Fraction
    assert oracle.radii == [Fraction(tiny) * 2**1048, Fraction(tiny) / 2**1048]
    assert oracle.inertia(0.) == (1, 0, 1)


def test_weighted_counts_refuse_original_nonhermitian_operator_and_bit_overrun():
    a = star(); a[0, 1] *= 1.0000000000000002
    cfg = resolve_config({'target': {'k': 1, 'sigma': 0.}})
    proof = certify_weighted(a, np.ones(1), np.eye(5)[:, :1], cfg)
    assert proof['target_certification'] == 'UNVERIFIED' and proof['certification_limit'] == 'exact_hermitian_required'
    a = sparse.diags([np.nextafter(0., 1.), 1.], format='csr')
    proof = certify_weighted(a, np.ones(1), np.eye(2)[:, :1], cfg,
        budget=replace(GappedCountBudget(), max_bits=100))
    assert proof['target_certification'] == 'UNVERIFIED' and proof['certification_limit'] == 'bit_budget'


def test_weight_hash_depends_on_source_coefficients_not_candidate_values():
    a = star(); values, vectors = np.linalg.eigh(a.toarray())
    cfg = resolve_config({'target': {'k': 1, 'sigma': 0.}})
    good = certify_weighted(a, values[:1], vectors[:, :1], cfg)
    bad = certify_weighted(a, np.array([.5]), vectors[:, :1], cfg)
    assert bad['target_certification'] == 'UNVERIFIED'
    assert good['certification_arithmetic']['weight_exponents_sha256'] == bad['certification_arithmetic']['weight_exponents_sha256']


def test_impossible_weight_prepass_budget_refuses_before_full_input_work(monkeypatch):
    a = star(); values = np.ones(1); vectors = np.eye(5)[:, :1]
    cfg = resolve_config({'target': {'k': 1, 'sigma': 0.}})
    def forbidden(*args, **kwargs): raise AssertionError('Impossible prepass budget must refuse before full input work')
    monkeypatch.setattr(sparse.csr_matrix, 'conjugate', forbidden)
    monkeypatch.setattr(np, 'empty', forbidden)
    monkeypatch.setattr(np, 'isfinite', forbidden)
    proof = certify_weighted(a, values, vectors, cfg,
        budget=replace(GappedCountBudget(), max_operations=a.shape[0] + a.nnz - 1))
    assert proof['target_certification'] == 'UNVERIFIED' and proof['certification_limit'] == 'operation_budget'
