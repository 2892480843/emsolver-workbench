import importlib.util

import numpy as np
import pytest
from scipy import sparse


def test_metal_probe_does_not_claim_cuda_or_fp64_gpu_support():
    from emsolver.numerics.gpu import probe_metal
    probe = probe_metal()
    assert probe['cuda_supported'] is False
    assert probe['gpu_precision'] == 'float32_or_complex64'
    assert probe['authoritative_validation'] == 'CPU_float64_original_problem'


@pytest.mark.parametrize('complex_input', [False, True])
def test_actual_metal_sparse_operator_matches_independent_cpu_multiply(complex_input):
    from emsolver.numerics.gpu import MetalSparseOperator, probe_metal
    if not probe_metal()['supported']:
        pytest.skip('Actual Apple Metal and optional MLX are required')
    rng = np.random.default_rng(301)
    values = rng.normal(size=150)
    vector = rng.normal(size=40)
    if complex_input:
        values = values + 1j * rng.normal(size=150)
        vector = vector + 1j * rng.normal(size=40)
    matrix = sparse.coo_matrix((values, (rng.integers(0, 40, 150), rng.integers(0, 40, 150))), shape=(40, 40)).tocsr()
    operation = MetalSparseOperator(matrix, memory_limit_bytes=64 * 1024**2)
    result = operation.matvec(vector)
    np.testing.assert_allclose(result, matrix @ vector, rtol=3e-6, atol=3e-6)
    metrics = operation.metrics()
    assert metrics['gpu_matvec_calls'] == 1
    assert metrics['gpu_backend'] == 'MLX_Metal_sparse_COO'
    assert metrics['gpu_nnz'] == matrix.nnz
    assert metrics['gpu_peak_allocated_bytes'] > 0
    assert metrics['gpu_seconds'] > 0
    assert not metrics['gpu_full_matrix_dense']
    with pytest.raises(ValueError):
        operation.matvec(np.full(40, np.nan))
    with pytest.raises(ValueError):
        operation.matvec(np.ones(39))


def test_metal_gpu_profile_is_explicitly_bounded_and_requires_mixed_jd():
    from emsolver.numerics.gpu import validate_gpu_profile
    from emsolver.contracts import resolve_config
    base = resolve_config({'resources': {'device': 'gpu'}, 'solver': {'method': 'arpack'}})
    with pytest.raises(ValueError, match='UNSUPPORTED_GPU_PROFILE'):
        validate_gpu_profile(base, require_available=False)
    jd = resolve_config({'resources': {'device': 'gpu'}, 'solver': {'method': 'jacobi_davidson',
         'transform': 'precond', 'linear': {'method': 'gmres', 'preconditioner': 'jacobi', 'precision': 'mixed_float32'}}})
    assert validate_gpu_profile(jd, require_available=False) == 'metal_jd_correction_v1'
    jd['resources']['mpi_ranks'] = 2
    with pytest.raises(ValueError, match='UNSUPPORTED_GPU_PROFILE'):
        validate_gpu_profile(jd, require_available=False)


def test_metal_preflights_budget_before_allocating_coo_coordinates(monkeypatch):
    from emsolver.numerics import gpu
    monkeypatch.setattr(gpu,'probe_metal',lambda:{'supported':True})
    matrix=sparse.eye(100,format='csr')
    def forbidden(*args,**kwargs):raise AssertionError('COO allocation occurred before budget rejection')
    monkeypatch.setattr(sparse.csr_matrix,'tocoo',forbidden)
    with pytest.raises(ValueError,match='RESOURCE_ESTIMATE_EXCEEDED'):
        gpu.MetalSparseOperator(matrix,memory_limit_bytes=1)


@pytest.mark.parametrize('active,peak',[(65,65),(32,65)])
def test_metal_actual_memory_guard_rejects_exceeded_soft_limit(active,peak):
    from types import SimpleNamespace
    from emsolver.numerics.gpu import MetalSparseOperator
    operation=object.__new__(MetalSparseOperator)
    operation.mx=SimpleNamespace(get_active_memory=lambda:active,get_peak_memory=lambda:peak)
    operation.memory_limit=64;operation.peak=0
    with pytest.raises(RuntimeError,match='RESOURCE_EXHAUSTED'):operation._check_memory()


@pytest.mark.parametrize('kind', ['indefinite', 'complex'])
def test_real_metal_jd_corrections_are_verified_against_original_fp64_problem(tmp_path, kind):
    from emsolver.numerics.gpu import probe_metal
    if not probe_metal()['supported']:
        pytest.skip('Actual optional Metal runtime is required')
    from emsolver.numerics import core
    matrix = tmp_path / 'input.npz'
    core.generate_sample(matrix, kind, n=40, seed=20261016)
    config = {'target': {'k': 2, 'sigma': .1}, 'solver': {'method': 'jacobi_davidson',
              'transform': 'precond', 'subspace_max': 20, 'max_outer_iterations': 100,
              'linear': {'method': 'gmres', 'preconditioner': 'jacobi', 'precision': 'mixed_float32',
                         'rtol_initial': .001, 'max_iterations': 500}, 'block': {'max_restarts': 4}},
              'resources': {'device': 'gpu', 'threads': 1, 'memory_limit_gib': 1, 'walltime_seconds': 30}}
    result = core.run_solver(matrix, config, tmp_path / 'run')
    assert result['validation']['numerical_status'] == 'PASSED'
    assert result['validation']['n_validated'] == 2
    assert result['metrics']['gpu_matvec_calls'] > 0
    assert result['metrics']['float32_linear_solves'] > 0
    assert result['metrics']['gpu_full_matrix_dense'] is False
    import json
    strategy = json.loads((tmp_path / 'run' / 'strategy.json').read_text())
    assert strategy['compute_device'] == 'metal_mixed_correction_cpu64'
    assert strategy['linear_solver']['sparse_product_device'] == 'Metal_GPU_FP32'
