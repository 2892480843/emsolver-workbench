import numpy as np
import pytest
from scipy import sparse
from scipy.linalg import eigh_tridiagonal
from types import SimpleNamespace

from emsolver.contracts import resolve_config
from emsolver.numerics import algorithm_plan, capabilities, core, estimate_resources


def ilu_config(**parts):
    config = resolve_config({'target': {'k': 2, 'sigma': .15},
                             'solver': {'method': 'jacobi_davidson', 'transform': 'precond',
                                        'subspace_max': 26, 'max_outer_iterations': 100,
                                        'linear': {'method': 'gmres', 'preconditioner': 'ilu_budgeted_v1',
                                                   'max_iterations': 300}},
                             'resources': {'threads': 1}})
    for section, values in parts.items():
        config[section].update(values)
    return config


def banded_matrix(n=30, complex_values=False, clustered=False):
    diagonal = np.linspace(-3., 4., n)
    if clustered:
        diagonal[n//2-2:n//2+2] = [.145, .149, .151, .155]
    off = np.full(n-1, .008 if clustered else .08)
    matrix = sparse.diags([off, diagonal, off], [-1, 0, 1], format='csr')
    if complex_values:
        phases = sparse.diags(np.exp(1j*np.linspace(0, 2., n)))
        matrix = (phases @ matrix @ phases.conjugate()).tocsr()
    return matrix, eigh_tridiagonal(diagonal, off, eigvals_only=True)


def test_ilu_plan_is_explicit_cpu64_policy_and_existing_defaults_stay_unchanged():
    config = ilu_config()
    plan = algorithm_plan(config)
    policy = plan['linear_solver']['preconditioner_policy']
    assert policy['id'] == 'ilu_budgeted_v1'
    assert policy['drop_tol'] == 1e-3 and policy['fill_factor'] == 2.
    assert policy['drop_rule'] == 'basic,area' and policy['permc_spec'] == 'COLAMD'
    assert policy['reuse'] == 'one_factorization_per_solve_fixed_target_shift'
    assert 'ilu_budgeted_v1' in capabilities()['jacobi_davidson']['preconditioners']
    assert resolve_config({})['solver']['linear']['preconditioner'] == 'auto_budgeted'


@pytest.mark.parametrize('section,change,code', [
    ('solver', {'linear': {'method': 'gmres', 'preconditioner': 'ilu_budgeted_v1',
                           'precision': 'mixed_float32'}}, 'UNSUPPORTED_PRECISION'),
    ('resources', {'device': 'gpu'}, 'UNSUPPORTED_PRECONDITIONER'),
])
def test_ilu_rejects_unsupported_precision_and_device(section, change, code):
    config = ilu_config()
    if section == 'solver':
        config['solver']['linear'].update(change['linear'])
    else:
        config[section].update(change)
    with pytest.raises(ValueError, match=code):
        algorithm_plan(config)


@pytest.mark.parametrize('complex_values,clustered', [(False, False), (True, False), (False, True)])
def test_ilu_jd_reuses_factor_and_accepts_only_original_residuals(tmp_path, complex_values, clustered):
    matrix, roots = banded_matrix(complex_values=complex_values, clustered=clustered)
    path = tmp_path/'matrix.npz'
    sparse.save_npz(path, matrix)
    config = ilu_config()
    result = core.run_solver(path, config, tmp_path/'run')
    assert result['n_accepted'] == 2
    assert result['validation']['numerical_status'] == 'PASSED'
    expected = roots[np.lexsort((roots, np.abs(roots-.15)))[:2]]
    np.testing.assert_allclose(result['eigenvalues'], expected, atol=1e-8)
    metrics = result['metrics']
    assert metrics['factorizations'] == metrics['preconditioner_factorizations'] == 1
    assert metrics['linear_solves'] > 1
    assert metrics['preconditioner_factor_nnz'] > 0
    assert metrics['preconditioner_applications'] > metrics['linear_solves']
    assert metrics['preconditioner_factorization_seconds'] >= 0
    assert metrics['preconditioner_apply_seconds'] >= 0
    assert metrics['max_linear_residual'] <= config['solver']['linear']['rtol_initial']*(1+1e-6)
    assert core.verify_run(path, tmp_path/'run')['numerical_status'] == 'PASSED'


def test_ilu_estimate_accounts_each_factor_and_panel_without_claiming_rss_bound():
    stats = {'n': 1000, 'nnz': 5000, 'dtype': 'complex128', 'is_diagonal': False}
    estimate = estimate_resources(stats, ilu_config())
    components = estimate['components']
    assert components['ilu_l_factor_bytes'] >= 2*stats['nnz']*(16+8)
    assert components['ilu_u_factor_bytes'] >= 2*stats['nnz']*(16+8)
    assert components['ilu_panel_workspace_bytes'] > stats['n']*16
    assert estimate['factorization_fill_in']['bounded'] is False
    assert estimate['factorization_fill_in']['policy']['fill_factor'] == 2.
    assert estimate['requires_runtime_process_tree_guard'] is True


class FakeRuntime:
    def __init__(self, check=None, rss=0):
        self.process = SimpleNamespace(memory_info=lambda: SimpleNamespace(rss=rss))
        self.callback = check
        self.checks = 0

    def check(self):
        self.checks += 1
        if self.callback:
            self.callback(self.checks)

    def emit(self, *args, **kwargs):
        pass


def build_ilu(matrix, config=None, runtime=None, sigma=0.):
    from emsolver.numerics.preconditioning import BudgetedILU
    counters = {'factorizations': 0, 'preconditioner_applications': 0}
    result = BudgetedILU.build(matrix, sigma, config or ilu_config(), runtime or FakeRuntime(), counters)
    return result, counters


def test_ilu_budget_rejection_precedes_csc_or_factor_allocation(monkeypatch):
    from emsolver.numerics import preconditioning
    matrix, _ = banded_matrix()
    def forbidden(*args, **kwargs):
        pytest.fail('CSC or factorization allocation reached after unsafe ILU estimate')
    monkeypatch.setattr(sparse.csr_matrix, 'tocsc', forbidden)
    monkeypatch.setattr(preconditioning.sla, 'spilu', forbidden)
    with pytest.raises(RuntimeError, match='RESOURCE_EXHAUSTED: ILU live-buffer estimate'):
        build_ilu(matrix, ilu_config(resources={'memory_limit_gib': 1e-5}))


def test_ilu_checks_actual_headroom_before_csc_allocation(monkeypatch):
    matrix, _ = banded_matrix()
    config = ilu_config(resources={'memory_limit_gib': 1.})
    def forbidden(*args, **kwargs):
        pytest.fail('CSC allocation reached without process headroom')
    monkeypatch.setattr(sparse.csr_matrix, 'tocsc', forbidden)
    with pytest.raises(RuntimeError, match='RESOURCE_EXHAUSTED: insufficient process memory headroom'):
        build_ilu(matrix, config, FakeRuntime(rss=1024**3))


def test_ilu_csc_setup_allocation_failure_has_resource_error_code(monkeypatch):
    def fail(*args, **kwargs):
        raise MemoryError('controlled sparse allocation failure')
    monkeypatch.setattr(sparse.csr_matrix, 'tocsc', fail)
    with pytest.raises(RuntimeError, match='RESOURCE_EXHAUSTED.*ILU.*setup'):
        build_ilu(banded_matrix()[0])


@pytest.mark.parametrize('exception,message', [
    (RuntimeError('Factor is exactly singular'), 'PRECONDITIONER_FAILED.*zero pivot'),
    (MemoryError('allocation'), 'RESOURCE_EXHAUSTED.*factorization'),
])
def test_ilu_reports_factor_failure_without_unrecorded_retries(monkeypatch, exception, message):
    from emsolver.numerics import preconditioning
    calls = []
    def fail(matrix, **policy):
        calls.append(policy)
        raise exception
    monkeypatch.setattr(preconditioning.sla, 'spilu', fail)
    with pytest.raises(RuntimeError, match=message):
        build_ilu(banded_matrix()[0])
    assert calls == [{'drop_tol': 1e-3, 'fill_factor': 2., 'drop_rule': 'basic,area', 'permc_spec': 'COLAMD'}]


@pytest.mark.parametrize('after_factor', [False, True])
def test_ilu_preserves_runtime_cancellation_before_and_after_factor(monkeypatch, after_factor):
    from emsolver.numerics import preconditioning
    built = []
    def factor(matrix, **kwargs):
        built.append(True)
        return SimpleNamespace(nnz=matrix.nnz)
    def check(count):
        if bool(built) == after_factor:
            raise InterruptedError('CANCELLED: controlled check boundary')
    monkeypatch.setattr(preconditioning.sla, 'spilu', factor)
    with pytest.raises(InterruptedError, match='CANCELLED'):
        build_ilu(banded_matrix()[0], runtime=FakeRuntime(check))
    assert bool(built) == after_factor


def test_ilu_nonfinite_apply_fails_and_never_exports_sparse_factors(monkeypatch):
    from emsolver.numerics import preconditioning
    class NonfiniteFactor:
        nnz = 100
        @property
        def L(self):
            pytest.fail('large L export is prohibited')
        @property
        def U(self):
            pytest.fail('large U export is prohibited')
        def solve(self, vector):
            return np.full_like(vector, np.nan)
    monkeypatch.setattr(preconditioning.sla, 'spilu', lambda *args, **kwargs: NonfiniteFactor())
    ilu, counters = build_ilu(banded_matrix()[0])
    assert counters['preconditioner_factor_nnz'] == 100
    with pytest.raises(RuntimeError, match='PRECONDITIONER_FAILED.*nonfinite'):
        ilu.apply(np.ones(30))


def test_ilu_target_eigenvalue_uses_explicit_stabilized_preconditioner_shift():
    n = 31
    matrix = sparse.diags([np.ones(n-1), np.ones(n-1)], [-1, 1], shape=(n,n), format='csr')
    original = matrix.copy()
    ilu, counters = build_ilu(matrix)
    assert np.isfinite(ilu.apply(np.linspace(-1., 1., n))).all()
    assert counters['preconditioner_target_shift_scaled'] == 0.
    assert counters['preconditioner_work_shift_scaled'] == np.sqrt(np.finfo(float).eps)*2.
    assert (matrix != original).nnz == 0


def test_ilu_cannot_accept_false_gmres_success_without_true_correction_residual(tmp_path, monkeypatch):
    from emsolver.numerics import correction
    matrix, _ = banded_matrix()
    path = tmp_path/'matrix.npz'
    sparse.save_npz(path, matrix)
    monkeypatch.setattr(correction.sla, 'gmres', lambda operator, rhs, **kwargs: (np.zeros_like(rhs), 0))
    result = core.run_solver(path, ilu_config(), tmp_path/'run')
    assert result['status'] == 'FAILED'
    assert result['n_accepted'] == 0
    assert result['attempts'][0]['status'] == 'INNER_NOT_CONVERGED'
    assert result['metrics']['inner_failures'] == 1
    assert result['metrics']['max_linear_residual'] == 1.


def test_ilu_factor_failure_emits_stable_diagnostic_event_and_numerical_exit(monkeypatch):
    from emsolver.numerics import preconditioning
    from emsolver.cli import _result_exit
    events=[]
    runtime=FakeRuntime()
    runtime.emit=lambda stage,event,**payload:events.append((stage,event,payload))
    def fail(*args,**kwargs):
        raise RuntimeError('Factor is exactly singular')
    monkeypatch.setattr(preconditioning.sla,'spilu',fail)
    with pytest.raises(RuntimeError,match='PRECONDITIONER_FAILED'):
        build_ilu(banded_matrix()[0],runtime=runtime)
    failed=[e for e in events if e[1]=='preconditioner_failed']
    assert len(failed)==1
    assert failed[0][2]['metrics']['failure_stage']=='factorization'
    assert failed[0][2]['metrics']['reason']=='singular_pivot'
    assert failed[0][2]['metrics']['attempts']==1
    assert _result_exit({'status':'FAILED','error_code':'PRECONDITIONER_FAILED'})==3
