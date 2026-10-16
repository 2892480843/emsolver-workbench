import json

import numpy as np
import pytest
from scipy import sparse

from emsolver.contracts import resolve_config
from emsolver.numerics import core


def config(**parts):
    return resolve_config({'target': {'k': 3, 'sigma': 0.15}, **parts})


def matrix_file(tmp_path, a):
    path = tmp_path / 'matrix.npz'
    sparse.save_npz(path, a.tocsr())
    return path


def test_backend_protocol_enforces_lifecycle_and_returns_original_order(tmp_path):
    from emsolver.numerics import create_backend
    a = sparse.diags([np.ones(29), np.linspace(-3., 5., 30), np.ones(29)], [-1, 0, 1], format='csr')
    cfg = config(problem={'reordering': 'reverse_cuthill_mckee'})
    backend = create_backend(cfg)
    with pytest.raises(ValueError, match='BACKEND_LIFECYCLE'):
        backend.finalize()
    prepared = backend.prepare(a)
    assert prepared['reordering'] == 'reverse_cuthill_mckee'
    backend.solve()
    result = backend.finalize()
    assert result.vectors.shape == (30, 3)
    assert backend.monitor()['phase'] == 'FINALIZED'
    for value, vector in zip(result.values, result.vectors.T):
        assert np.linalg.norm(a @ vector-value*vector) < 1e-8
    with pytest.raises(ValueError, match='BACKEND_LIFECYCLE'):
        backend.solve()


def test_reordering_saves_mapping_and_validation_uses_original_input(tmp_path):
    a = sparse.diags([np.ones(39), np.linspace(-3., 5., 40), np.ones(39)], [-1, 0, 1], format='csr')
    path = matrix_file(tmp_path, a)
    cfg = config(problem={'reordering': 'reverse_cuthill_mckee'})
    result = core.run_solver(path, cfg, tmp_path / 'run')
    mapping = np.load(tmp_path / 'run/permutation.npy', allow_pickle=False)
    assert sorted(mapping) == list(range(40))
    assert not np.array_equal(mapping, np.arange(40))
    x = np.load(tmp_path / 'run/eigenvectors.npy', allow_pickle=False)
    for value, vector in zip(result['eigenvalues'], x.T):
        assert np.linalg.norm(a @ vector-value*vector) < 1e-8
    assert core.verify_run(path, tmp_path / 'run')['numerical_status'] == 'PASSED'
    strategy = json.loads((tmp_path / 'run/strategy.json').read_text())
    assert strategy['verification_input'] == 'original_unpermuted_matrix'
    assert strategy['actual_method'] == 'arpack'


def test_memory_estimate_lists_live_storage_fill_risk_and_does_not_promise_peak():
    from emsolver.numerics import estimate_resources
    a = sparse.diags([np.ones(999), np.ones(1000), np.ones(999)], [-1, 0, 1], format='csr')
    estimate = estimate_resources(a, config(problem={'reordering':'reverse_cuthill_mckee'}))
    assert estimate['minimum_memory_bytes'] > a.data.nbytes+a.indices.nbytes+a.indptr.nbytes
    assert estimate['estimated_memory_range_bytes'][1] > estimate['estimated_memory_range_bytes'][0]
    assert estimate['components']['permutation_bytes'] >= 1000*8
    assert estimate['components']['validation_bytes'] > 0
    assert estimate['factorization_fill_in']['bounded'] is False
    assert estimate['is_measured_peak'] is False
    json.dumps(estimate, allow_nan=False)


def test_direct_numerical_entry_rejects_unused_mass_matrix_in_standard_problem(tmp_path):
    path=matrix_file(tmp_path,sparse.diags(np.arange(1.,21.),format='csr'))
    cfg=config(problem={'matrix_id':'m_a','matrix_b_id':'m_b'})
    with pytest.raises(ValueError,match='UNSUPPORTED_PROBLEM'):
        core.run_solver(path,cfg,tmp_path/'run')
    assert not (tmp_path/'run/manifest.json').exists()


def test_diagonal_correction_estimate_does_not_reserve_unused_iterative_buffers():
    from emsolver.numerics import estimate_resources
    cfg=config(solver={'method':'jacobi_davidson','transform':'precond','subspace_max':1024,
                       'linear':{'method':'gmres','preconditioner':'jacobi','precision':'mixed_float32'}})
    stats={'n':100000,'nnz':100000,'dtype':'float64','storage_bytes':1600004,'is_diagonal':True}
    diagonal=estimate_resources(stats,cfg)
    iterative=estimate_resources({**stats,'is_diagonal':False},cfg)
    for key in ('inner_krylov_bytes','mixed_precision_matrix_bytes','preconditioner_storage_bytes'):
        assert diagonal['components'][key]==0
        assert iterative['components'][key]>0


def test_nullspace_metadata_checks_matrix_without_discarding_zero_modes(tmp_path):
    a = sparse.diags([0., 0., 2., 3., 4., 5.], format='csr')
    path = matrix_file(tmp_path, a)
    cfg = config(problem={'nullspace': {'basis_indices':[0, 1], 'interpretation':'expected_nullspace', 'source':'model constraints'}})
    cfg['target']['sigma'] = 0.
    result = core.run_solver(path, cfg, tmp_path / 'run')
    diagnostics = json.loads((tmp_path / 'run/diagnostics.json').read_text())
    assert result['eigenvalues'][:2] == [0., 0.]
    assert diagnostics['nullspace']['verified_dimension'] == 2
    assert diagnostics['nullspace']['physical_classification'] == 'metadata_expected_nullspace'
    assert diagnostics['nullspace']['modes_removed'] == 0


def test_false_nullspace_claim_is_reported_as_unverified_without_changing_input(tmp_path):
    a = sparse.diags([0., 1., 2., 3., 4., 5.], format='csr')
    path = matrix_file(tmp_path, a)
    cfg = config(problem={'nullspace':{'basis_indices':[1], 'interpretation':'expected_nullspace'}})
    core.run_solver(path, cfg, tmp_path / 'run')
    diagnostics = json.loads((tmp_path / 'run/diagnostics.json').read_text())
    assert diagnostics['nullspace']['verified_dimension'] == 0
    assert diagnostics['nullspace']['metadata_status'] == 'NOT_VERIFIED'
    assert diagnostics['nullspace']['physical_classification'] == 'unverified'


def test_backend_plan_describes_actual_capabilities_and_no_fake_outer_iterations():
    from emsolver.numerics import algorithm_plan
    plan = algorithm_plan(config())
    assert plan['actual_method'] == 'arpack'
    assert plan['precision'] == 'float64_or_complex128'
    assert plan['monitoring']['outer_iteration_count'] is False
    assert plan['checkpoint']['exact_resume'] is False
    assert plan['linear_solver']['adaptive_tolerance'] is False


@pytest.mark.parametrize('complex_path', [False, True])
def test_real_block_iteration_matches_independent_tridiagonal_reference(tmp_path, complex_path):
    from scipy.linalg import eigvalsh_tridiagonal
    n = 70
    diagonal = np.linspace(-4., 5., n)
    off = np.full(n-1, 0.13j if complex_path else 0.13)
    a = sparse.diags([off.conjugate(), diagonal, off], [-1, 0, 1], format='csr')
    path = matrix_file(tmp_path, a)
    events = []
    cfg = config(solver={'method':'block_shift_invert', 'subspace_max':20, 'max_outer_iterations':300,
                         'block':{'residual_check_interval':2}})
    result = core.run_solver(path, cfg, tmp_path / 'block', event_callback=events.append)
    reference = eigvalsh_tridiagonal(diagonal, np.abs(off))
    expected = reference[np.lexsort((reference, np.abs(reference-0.15)))[:3]]
    assert result['validation']['numerical_status'] == 'PASSED'
    assert result['eigenvalues'] == pytest.approx(expected, abs=1e-9)
    assert result['metrics']['method'] == 'block_shift_invert'
    assert result['metrics']['outer_iterations'] > 0
    assert result['metrics']['matrix_vector_products'] > 0
    assert any(event['event'] == 'block_residual_checked' for event in events)
    assert any(event['event'] == 'modes_soft_locked' for event in events)
    assert (tmp_path / 'block/subspace.checkpoint.npz').is_file()


def test_block_iteration_recovers_singular_shift_and_preserves_selected_repeated_subspace(tmp_path):
    repeated = [sparse.csr_matrix([[0., 1.], [1., 0.]])]*6
    tail = sparse.diags([np.ones(39)*0.05, np.linspace(3., 8., 40), np.ones(39)*0.05], [-1, 0, 1])
    a = sparse.block_diag([*repeated, tail], format='csr')
    path = matrix_file(tmp_path, a)
    cfg = config(target={'k':6, 'sigma':1., 'cluster_policy':'cluster_complete'},
                 solver={'method':'block_shift_invert', 'subspace_max':24, 'max_outer_iterations':150})
    result = core.run_solver(path, cfg, tmp_path / 'repeated')
    assert result['n_accepted'] == 6
    assert result['eigenvalues'] == pytest.approx(np.ones(6), abs=1e-10)
    assert result['attempts'][0]['status'] == 'FAILED'
    assert len(result['attempts']) > 1
    x = np.load(tmp_path / 'repeated/eigenvectors.npy')
    assert np.linalg.norm(x.conj().T @ x-np.eye(6)) < 1e-10
    assert np.linalg.norm(x[12:]) < 1e-8


def test_block_iteration_never_densifies_original_or_shifted_sparse_matrices(tmp_path, monkeypatch):
    n = 60
    a = sparse.diags([np.ones(n-1)*0.1, np.linspace(-2., 5., n), np.ones(n-1)*0.1], [-1, 0, 1], format='csr')
    path = matrix_file(tmp_path, a)
    def forbidden(*args, **kwargs):
        raise AssertionError('original matrix densification')
    monkeypatch.setattr(sparse.csr_matrix, 'toarray', forbidden)
    monkeypatch.setattr(sparse.csc_matrix, 'toarray', forbidden)
    result = core.run_solver(path, config(solver={'method':'block_shift_invert'}), tmp_path / 'sparse')
    assert result['validation']['numerical_status'] == 'PASSED'


def test_checkpoint_warm_start_validates_hash_version_and_is_not_claimed_as_exact_resume(tmp_path):
    n = 50
    a = sparse.diags([np.ones(n-1)*0.1, np.linspace(-2., 5., n), np.ones(n-1)*0.1], [-1, 0, 1], format='csr')
    path = matrix_file(tmp_path, a)
    cfg = config(solver={'method':'block_shift_invert'})
    core.run_solver(path, cfg, tmp_path / 'first')
    checkpoint = tmp_path / 'first/subspace.checkpoint.npz'
    result = core.run_solver(path, cfg, tmp_path / 'warm', initial_subspace_path=checkpoint)
    assert result['metrics']['warm_start'] is True
    strategy = json.loads((tmp_path / 'warm/strategy.json').read_text())
    assert strategy['checkpoint']['exact_resume'] is False
    assert strategy['checkpoint']['warm_start'] is True
    changed = config(target={'k':3,'sigma':0.2}, solver={'method':'block_shift_invert'})
    with pytest.raises(ValueError, match='CHECKPOINT_CONFIG_MISMATCH'):
        core.run_solver(path, changed, tmp_path / 'changed', initial_subspace_path=checkpoint)
    changed_a = a.copy()
    changed_a.data *= 2
    sparse.save_npz(path, changed_a)
    with pytest.raises(ValueError, match='CHECKPOINT_INPUT_MISMATCH'):
        core.run_solver(path, cfg, tmp_path / 'input-changed', initial_subspace_path=checkpoint)


def test_block_iteration_maximum_iteration_failure_does_not_publish_false_convergence(tmp_path):
    n = 60
    a = sparse.diags([np.ones(n-1)*0.1, np.linspace(-2., 5., n), np.ones(n-1)*0.1], [-1, 0, 1], format='csr')
    path = matrix_file(tmp_path, a)
    cfg = config(solver={'method':'block_shift_invert', 'max_outer_iterations':1,
                         'adaptive_shift':{'enabled':False}})
    result = core.run_solver(path, cfg, tmp_path / 'limited')
    assert result['status'] == 'FAILED'
    assert result['n_accepted'] == 0
    assert result['metrics']['outer_iterations'] == 1
    assert result['attempts'][0]['status'] == 'MAX_ITERATIONS'


def test_checkpoint_rejects_backend_version_tampering_and_forged_allocations(tmp_path):
    import io
    import zipfile
    n = 25
    a = sparse.diags([np.ones(n-1)*0.1, np.linspace(-2., 5., n), np.ones(n-1)*0.1], [-1, 0, 1], format='csr')
    path = matrix_file(tmp_path, a)
    cfg = config(solver={'method':'block_shift_invert'})
    core.run_solver(path, cfg, tmp_path / 'first')
    with np.load(tmp_path / 'first/subspace.checkpoint.npz', allow_pickle=False) as archive:
        basis, metadata = archive['basis'], json.loads(archive['metadata'].item())
    metadata['scipy_version'] = 'forged'
    np.savez(tmp_path / 'bad-version.npz', basis=basis, metadata=np.asarray(json.dumps(metadata)))
    with pytest.raises(ValueError, match='CHECKPOINT_VERSION_MISMATCH'):
        core.run_solver(path, cfg, tmp_path / 'wrong-version', initial_subspace_path=tmp_path / 'bad-version.npz')
    header = io.BytesIO()
    np.lib.format.write_array_header_1_0(header, {'descr':'<f8','fortran_order':False,'shape':(10**12, 3)})
    with zipfile.ZipFile(tmp_path / 'allocation.npz', 'w') as archive:
        archive.writestr('basis.npy', header.getvalue())
        stream = io.BytesIO()
        np.save(stream, np.asarray('{}'), allow_pickle=False)
        archive.writestr('metadata.npy', stream.getvalue())
    with pytest.raises(ValueError, match='INPUT_BUDGET|INVALID_ARCHIVE'):
        core.run_solver(path, cfg, tmp_path / 'forged', initial_subspace_path=tmp_path / 'allocation.npz')


def test_block_cancellation_on_residual_event_stops_before_publication(tmp_path):
    n = 50
    a = sparse.diags([np.ones(n-1)*0.1, np.linspace(-2., 5., n), np.ones(n-1)*0.1], [-1, 0, 1], format='csr')
    path = matrix_file(tmp_path, a)
    state = {'cancel':False}
    def event(item):
        if item['event'] == 'block_residual_checked':
            state['cancel'] = True
    with pytest.raises(InterruptedError):
        core.run_solver(path, config(solver={'method':'block_shift_invert'}), tmp_path / 'cancel',
                        event_callback=event, cancel_check=lambda:state['cancel'])
    assert not (tmp_path / 'cancel/manifest.json').exists()


def test_bounded_runtime_history_reports_retention_without_losing_callback_events(monkeypatch):
    cfg = config()
    observed = []
    runtime = core._Runtime(cfg, observed.append, None)
    monkeypatch.setattr(runtime, 'check', lambda:None)
    for index in range(4100):
        runtime.emit('SOLVING', 'bounded_test', iteration=index)
    assert len(observed) == 4100
    assert len(runtime.events) == 4096
    assert runtime.events_emitted == 4100
    assert runtime.events[0]['iteration'] == 4


def test_standalone_adapter_normalizes_integer_matrices_and_rejects_k_at_dimension():
    from emsolver.numerics import create_backend
    backend = create_backend(config(target={'k':2,'sigma':0.}))
    backend.prepare(sparse.diags(np.arange(10), format='csr', dtype=np.int64))
    backend.solve()
    result = backend.finalize()
    assert result.vectors.dtype == np.float64
    invalid = create_backend(config(target={'k':10, 'sigma':0.}))
    with pytest.raises(ValueError, match='BACKEND_K_LIMIT'):
        invalid.prepare(sparse.eye(10))


def test_runtime_throttles_memory_sampling_but_cancellation_remains_immediate(monkeypatch):
    from types import SimpleNamespace
    state = {'calls':0, 'cancel':False}
    class Process:
        def memory_info(self):
            state['calls'] += 1
            return SimpleNamespace(rss=100)
        def children(self, recursive):
            return []
    runtime = core._Runtime(config(), None, lambda:state['cancel'])
    runtime.process = Process()
    monkeypatch.setattr(core.time, 'monotonic', lambda:runtime.start+0.001)
    runtime.check()
    runtime.check()
    assert state['calls'] == 1
    state['cancel'] = True
    with pytest.raises(InterruptedError):
        runtime.check()


def test_normalization_reports_original_stored_entries_and_bounded_row_histogram(tmp_path):
    path = tmp_path / 'duplicates.npz'
    # Duplicate cancellation produces two stored zeros, which are removed.
    np.savez(path, format=np.asarray(b'coo'), shape=np.array([4, 4]),
             data=np.array([1., -1., 0., 2., 2., 3.]), row=np.array([0, 0, 1, 1, 2, 3]), col=np.array([0, 0, 1, 1, 2, 3]))
    a = core.load_matrix(path)
    report = core.inspect_matrix(a)
    assert report['normalization']['input_stored_nnz'] == 6
    assert report['normalization']['duplicates_merged'] == 2
    assert report['normalization']['zero_entries_removed'] == 1
    assert report['normalization']['final_nnz'] == 3
    histogram = report['row_nnz_histogram']
    assert len(histogram['counts']) <= 64
    assert sum(histogram['counts']) == 4
    assert len(histogram['bin_edges']) == len(histogram['counts'])+1


def test_normalization_records_triangle_reconstruction_and_index_error_location(tmp_path):
    path = matrix_file(tmp_path, sparse.csr_matrix([[1., 2.], [0., 3.]]))
    report = core.inspect_matrix(core.load_matrix(path, storage='upper'))
    assert report['normalization']['canonical_nnz_before_reconstruction'] == 3
    assert report['normalization']['reconstructed_nnz'] == 4
    bad = tmp_path / 'invalid-index.npz'
    np.savez(bad, format=np.asarray(b'csr'), shape=np.array([4, 4]), data=np.ones(2),
             indices=np.array([0, 8]), indptr=np.array([0, 1, 1, 1, 2]))
    with pytest.raises(ValueError, match=r'indices\[1\]'):
        core.load_matrix(bad)


def test_explicit_bounded_fallback_uses_frozen_complete_arpack_profile(tmp_path):
    from emsolver.numerics import algorithm_plan
    n = 70
    a = sparse.diags([np.ones(n-1)*0.1, np.linspace(-2., 5., n), np.ones(n-1)*0.1], [-1, 0, 1], format='csr')
    path = matrix_file(tmp_path, a)
    cfg = config(solver={'method':'block_shift_invert', 'max_outer_iterations':1,
                         'fallback_profiles':['block_to_arpack_v1']})
    plan = algorithm_plan(cfg)
    assert len(plan['execution_profiles']) == 2
    fallback = plan['execution_profiles'][1]['config']
    assert fallback['solver']['method'] == 'arpack'
    assert fallback['solver']['transform'] == 'shift_invert'
    assert fallback['solver']['linear']['method'] == 'sparse_lu'
    assert fallback['target'] == cfg['target']
    assert fallback['validation'] == cfg['validation']
    events = []
    result = core.run_solver(path, cfg, tmp_path / 'fallback', event_callback=events.append)
    assert result['metrics']['method'] == 'arpack'
    assert result['metrics']['fallbacks_executed'] == 1
    assert any(item['event'] == 'fallback_started' for item in events)
    assert any(item['method'] == 'block_shift_invert' for item in result['attempts'])
    assert any(item['method'] == 'arpack' for item in result['attempts'])


@pytest.mark.parametrize('profiles', [['unknown'], ['block_to_arpack_v1']*2])
def test_unvalidated_or_unbounded_fallback_profiles_are_rejected(profiles):
    from emsolver.numerics import algorithm_plan
    with pytest.raises(ValueError, match='BACKEND_UNAVAILABLE'):
        algorithm_plan(config(solver={'method':'block_shift_invert', 'fallback_profiles':profiles}))


def test_block_reordering_warm_start_restores_original_coordinates(tmp_path):
    n = 48
    a = sparse.diags([np.ones(n-1)*0.1, np.linspace(-2., 5., n), np.ones(n-1)*0.1], [-1, 0, 1], format='csr')
    path = matrix_file(tmp_path, a)
    cfg = config(problem={'reordering':'reverse_cuthill_mckee'}, solver={'method':'block_shift_invert'},
                 validation={'absolute_residual_max':1e-10})
    first = core.run_solver(path, cfg, tmp_path / 'first')
    warm = core.run_solver(path, cfg, tmp_path / 'warm', initial_subspace_path=tmp_path / 'first/subspace.checkpoint.npz')
    assert warm['eigenvalues'] == pytest.approx(first['eigenvalues'], abs=1e-9)
    vectors = np.load(tmp_path / 'warm/eigenvectors.npy')
    for value, vector in zip(warm['eigenvalues'], vectors.T):
        assert np.linalg.norm(a@vector-value*vector) < 1e-8
    assert core.verify_run(path, tmp_path / 'warm')['artifact_integrity'] == 'PASSED'


@pytest.mark.parametrize('complex_path', [False, True])
def test_generic_block_targets_match_small_independent_dense_oracle(tmp_path, complex_path):
    # A small independent reference is test-only; the production path receives
    # only sparse input and never builds a dense original matrix.
    rng = np.random.default_rng(6041)
    raw = rng.standard_normal((36, 36))
    if complex_path:
        raw = raw+1j*rng.standard_normal((36, 36))
    a = (raw+raw.conj().T)/2
    expected = np.linalg.eigvalsh(a)
    expected = expected[np.lexsort((expected, np.abs(expected-0.15)))[:3]]
    path = matrix_file(tmp_path, sparse.csr_matrix(a))
    result = core.run_solver(path, config(solver={'method':'block_shift_invert'}), tmp_path / 'generic')
    assert result['eigenvalues'] == pytest.approx(expected, abs=1e-8)
    assert result['status'] == 'PARTIAL'
    assert result['validation']['numerical_status'] == 'PASSED'
    assert result['validation']['target_certification'] == 'UNVERIFIED'


@pytest.mark.parametrize('enabled', ['false', 0, 1, None])
def test_observability_switch_requires_a_boolean(enabled):
    with pytest.raises(ValueError):
        config(observability={'enabled':enabled})


def test_observability_switch_suppresses_secondary_events_without_changing_numerics(tmp_path):
    a = sparse.diags([np.ones(39)*0.1, np.linspace(-2., 5., 40), np.ones(39)*0.1], [-1, 0, 1], format='csr')
    path = matrix_file(tmp_path, a)
    records = []
    for enabled in (True, False):
        events = []
        cfg = config(solver={'method':'block_shift_invert'}, observability={'enabled':enabled})
        result = core.run_solver(path, cfg, tmp_path / str(enabled), event_callback=events.append)
        assert any(event['event'] == 'independent_validation_completed' for event in events)
        assert any(event['event'] == 'artifacts_written' for event in events)
        assert any(event['event'] == 'block_residual_checked' for event in events) is enabled
        assert result['metrics']['secondary_observation_enabled'] is enabled
        assert (result['metrics']['events_suppressed'] > 0) is (not enabled)
        records.append(result)
    assert records[0]['eigenvalues'] == records[1]['eigenvalues']
    assert records[0]['residuals'] == records[1]['residuals']
    assert records[0]['metrics']['outer_iterations'] == records[1]['metrics']['outer_iterations']


@pytest.mark.parametrize('complex_path', [False, True])
def test_jd_correction_equation_adapts_inner_accuracy_with_final_double_validation(tmp_path, complex_path):
    from scipy.linalg import eigvalsh_tridiagonal
    n = 30
    diagonal = np.linspace(-3., 4., n)
    off = np.full(n-1, 0.08j if complex_path else 0.08)
    a = sparse.diags([off.conjugate(), diagonal, off], [-1, 0, 1], format='csr')
    path = matrix_file(tmp_path, a)
    cfg = config(target={'k':2,'sigma':0.15}, solver={'method':'jacobi_davidson','transform':'precond','subspace_max':26,
                 'max_outer_iterations':100,'linear':{'method':'gmres','preconditioner':'none','max_iterations':300,
                 'adaptive_accuracy':{'enabled':True,'rtol_max':1e-2,'rtol_min':1e-10}}})
    result = core.run_solver(path, cfg, tmp_path / 'jd')
    roots = eigvalsh_tridiagonal(diagonal, np.abs(off))
    expected = roots[np.lexsort((roots, np.abs(roots-0.15)))[:2]]
    assert result['eigenvalues'] == pytest.approx(expected, abs=1e-7)
    assert result['validation']['numerical_status'] == 'PASSED'
    assert result['metrics']['linear_iterations'] > 0
    assert result['metrics']['inner_tolerance_min_used'] < result['metrics']['inner_tolerance_max_used']
    assert result['strategy']['linear_solver']['adaptive_tolerance'] is True
    assert result['strategy']['transform'] == 'projected_correction_equation'
    assert result['metrics']['factorizations'] == 0


def test_adaptive_accuracy_cannot_relax_arpack_or_exact_block_inverse_contract():
    from emsolver.numerics import algorithm_plan
    for method in ('arpack', 'block_shift_invert'):
        with pytest.raises(ValueError, match='UNSUPPORTED_INNER_ACCURACY'):
            algorithm_plan(config(solver={'method':method,'linear':{'adaptive_accuracy':{'enabled':True}}}))


def test_periodic_checkpoint_survives_cooperative_cancel_and_initializes_new_task(tmp_path):
    a = sparse.diags([np.ones(59)*0.1,np.linspace(-3.,4.,60),np.ones(59)*0.1],[-1,0,1],format='csr')
    path = matrix_file(tmp_path, a)
    cfg = config(solver={'method':'block_shift_invert','block':{'checkpoint_interval':2}})
    state = {'cancel':False}
    def event(item):
        if item['event'] == 'checkpoint_written':
            state['cancel'] = True
    with pytest.raises(InterruptedError):
        core.run_solver(path,cfg,tmp_path/'cancelled',event_callback=event,cancel_check=lambda:state['cancel'])
    checkpoint = tmp_path/'cancelled/subspace.checkpoint.npz'
    assert checkpoint.is_file()
    assert not (tmp_path/'cancelled/manifest.json').exists()
    with np.load(checkpoint,allow_pickle=False) as archive:
        metadata = json.loads(archive['metadata'].item())
    assert metadata['progress']['iteration'] >= 2
    assert metadata['exact_resume'] is False
    result = core.run_solver(path,cfg,tmp_path/'new',initial_subspace_path=checkpoint)
    assert result['metrics']['warm_start'] is True
    assert result['validation']['numerical_status'] == 'PASSED'


def jd_config(**linear):
    return config(target={'k':2,'sigma':0.15},solver={'method':'jacobi_davidson','transform':'precond','subspace_max':26,
                  'linear':{'method':'gmres','preconditioner':'none',**linear}})


def test_jd_fixed_accuracy_is_available_and_bad_correction_profiles_fail_early(tmp_path):
    from emsolver.numerics import algorithm_plan
    a = sparse.diags([np.ones(29)*0.08,np.linspace(-3.,4.,30),np.ones(29)*0.08],[-1,0,1],format='csr')
    path = matrix_file(tmp_path,a)
    result = core.run_solver(path,jd_config(),tmp_path/'fixed')
    assert result['n_accepted'] == 2
    assert result['validation']['numerical_status'] == 'PASSED'
    assert result['metrics']['inner_accuracy_adaptive'] is False
    for field,value in [('transform','shift_invert'),('method','sparse_lu'),('preconditioner','auto_budgeted')]:
        cfg = jd_config()
        if field == 'transform': cfg['solver'][field] = value
        else: cfg['solver']['linear'][field] = value
        with pytest.raises(ValueError,match='UNSUPPORTED_'):
            algorithm_plan(cfg)


def test_jd_inner_iteration_limit_cannot_create_false_accepted_modes(tmp_path):
    a = sparse.diags([np.ones(29)*0.08,np.linspace(-3.,4.,30),np.ones(29)*0.08],[-1,0,1],format='csr')
    path = matrix_file(tmp_path,a)
    result = core.run_solver(path,jd_config(max_iterations=1),tmp_path/'limited')
    assert result['status'] == 'FAILED'
    assert result['n_accepted'] == 0
    assert result['attempts'][0]['status'] == 'INNER_NOT_CONVERGED'
    assert result['metrics']['linear_iterations'] == 1
    assert result['metrics']['inner_failures'] == 1


def test_jd_periodic_checkpoint_warm_start_restores_original_order_after_cancel(tmp_path):
    a = sparse.diags([np.ones(29)*0.08,np.linspace(-3.,4.,30),np.ones(29)*0.08],[-1,0,1],format='csr')
    path = matrix_file(tmp_path,a)
    cfg = jd_config(adaptive_accuracy={'enabled':True})
    cfg['problem']['reordering'] = 'reverse_cuthill_mckee'
    cfg['solver']['block']['checkpoint_interval'] = 1
    state={'cancel':False}
    def event(item):
        if item['event']=='checkpoint_written':state['cancel']=True
    with pytest.raises(InterruptedError):
        core.run_solver(path,cfg,tmp_path/'cancel',event_callback=event,cancel_check=lambda:state['cancel'])
    result = core.run_solver(path,cfg,tmp_path/'new',initial_subspace_path=tmp_path/'cancel/subspace.checkpoint.npz')
    assert result['n_accepted']==2
    assert result['validation']['numerical_status']=='PASSED'
    assert core.verify_run(path,tmp_path/'new')['artifact_integrity']=='PASSED'


def test_preexisting_checkpoint_does_not_authorize_overwriting_an_output_directory(tmp_path):
    path=matrix_file(tmp_path,sparse.diags(np.arange(10.),format='csr'))
    out=tmp_path/'used'
    out.mkdir()
    (out/'subspace.checkpoint.npz').write_bytes(b'original checkpoint')
    with pytest.raises(ValueError,match='OUTPUT_EXISTS'):
        core.run_solver(path,config(),out)
    assert (out/'subspace.checkpoint.npz').read_bytes()==b'original checkpoint'


def test_full_internal_checkpoint_is_included_in_disk_budget_before_publication(tmp_path):
    n=2000
    a=sparse.diags([np.ones(n-1)*.01,np.linspace(-2.,5.,n),np.ones(n-1)*.01],[-1,0,1],format='csr')
    path=matrix_file(tmp_path,a)
    cfg=config(target={'k':1,'sigma':.15},solver={'method':'block_shift_invert','subspace_max':150,
               'max_outer_iterations':40,'block':{'oversampling':128}},resources={'threads':1,'disk_limit_gib':.0015})
    with pytest.raises(RuntimeError,match='RESOURCE_EXHAUSTED'):
        core.run_solver(path,cfg,tmp_path/'limited-disk')
    assert not (tmp_path/'limited-disk/manifest.json').exists()


def test_actual_disk_usage_is_rechecked_after_export_and_before_manifest(tmp_path):
    path=matrix_file(tmp_path,sparse.diags(np.arange(10.),format='csr'))
    out=tmp_path/'run'
    def event(item):
        if item['event']=='artifacts_written':
            (out/'unexpected.bin').write_bytes(b'x'*(4*1024**2))
    with pytest.raises(RuntimeError,match='RESOURCE_EXHAUSTED'):
        core.run_solver(path,config(resources={'disk_limit_gib':.002}),out,event_callback=event)
    assert not (out/'manifest.json').exists()


def test_event_history_enforces_serialized_byte_budget_as_well_as_count(monkeypatch):
    runtime=core._Runtime(config(),None,None)
    monkeypatch.setattr(runtime,'check',lambda:None)
    for index in range(1000):
        runtime.emit('SOLVING','bounded_payload',iteration=index,metrics={'payload':'x'*10000})
    assert runtime.retained_event_bytes <= runtime.event_history_byte_limit
    assert len(runtime.events)<1000
    assert runtime.events_emitted==1000


@pytest.mark.parametrize('complex_path',[False,True])
def test_jd_jacobi_preconditioning_and_float32_corrections_preserve_fp64_acceptance(tmp_path,complex_path):
    from scipy.linalg import eigvalsh_tridiagonal
    n=30
    diagonal=np.linspace(-3.,4.,n)
    off=np.full(n-1,.08j if complex_path else .08)
    a=sparse.diags([off.conjugate(),diagonal,off],[-1,0,1],format='csr')
    path=matrix_file(tmp_path,a)
    cfg=jd_config(preconditioner='jacobi',precision='mixed_float32',adaptive_accuracy={'enabled':True})
    result=core.run_solver(path,cfg,tmp_path/'mixed')
    roots=eigvalsh_tridiagonal(diagonal,np.abs(off))
    expected=roots[np.lexsort((roots,np.abs(roots-.15)))[:2]]
    assert result['eigenvalues']==pytest.approx(expected,abs=1e-7)
    assert result['validation']['numerical_status']=='PASSED'
    assert result['metrics']['float32_linear_solves']>0
    assert result['metrics']['preconditioner_applications']>0
    vectors=np.load(tmp_path/'mixed/eigenvectors.npy')
    assert vectors.dtype==np.dtype('complex128' if complex_path else 'float64')


def test_float32_backend_false_convergence_promotes_and_rechecks_fp64(tmp_path,monkeypatch):
    from emsolver.numerics import correction
    a=sparse.diags([np.ones(29)*.08,np.linspace(-3.,4.,30),np.ones(29)*.08],[-1,0,1],format='csr')
    path=matrix_file(tmp_path,a)
    actual=correction.sla.gmres
    def inaccurate(operator,right,**kwargs):
        if np.dtype(operator.dtype).itemsize<=4:
            return np.zeros_like(right),0
        return actual(operator,right,**kwargs)
    monkeypatch.setattr(correction.sla,'gmres',inaccurate)
    result=core.run_solver(path,jd_config(precision='mixed_float32',adaptive_accuracy={'enabled':True}),tmp_path/'promoted')
    assert result['n_accepted']==2 and result['validation']['numerical_status']=='PASSED'
    assert result['metrics']['precision_promotions']>0
    assert result['metrics']['float32_failures']>0
    assert result['metrics']['float64_linear_solves']>0


def test_strict_inner_tolerance_bypasses_float32_and_exact_inverse_rejects_mixed_precision(tmp_path):
    from emsolver.numerics import algorithm_plan
    a=sparse.diags([np.ones(29)*.08,np.linspace(-3.,4.,30),np.ones(29)*.08],[-1,0,1],format='csr')
    path=matrix_file(tmp_path,a)
    result=core.run_solver(path,jd_config(precision='mixed_float32',adaptive_accuracy={'enabled':True,'rtol_max':1e-7}),tmp_path/'strict')
    assert result['metrics']['float32_linear_solves']==0
    assert result['metrics']['float64_linear_solves']>0
    assert result['metrics']['precision_promotions']>0
    for method in ('arpack','block_shift_invert'):
        with pytest.raises(ValueError,match='UNSUPPORTED_PRECISION'):
            algorithm_plan(config(solver={'method':method,'linear':{'precision':'mixed_float32'}}))


def test_false_success_in_both_inner_precisions_cannot_produce_accepted_modes(tmp_path,monkeypatch):
    from emsolver.numerics import correction
    path=matrix_file(tmp_path,sparse.diags([np.ones(29)*.08,np.linspace(-3.,4.,30),np.ones(29)*.08],[-1,0,1],format='csr'))
    monkeypatch.setattr(correction.sla,'gmres',lambda operator,right,**kwargs:(np.zeros_like(right),0))
    result=core.run_solver(path,jd_config(precision='mixed_float32',adaptive_accuracy={'enabled':True}),tmp_path/'false-inner')
    assert result['status']=='FAILED' and result['n_accepted']==0
    assert result['metrics']['float32_failures']==1 and result['metrics']['precision_promotions']==1
    assert result['attempts'][0]['error_code']=='LINEAR_NO_CONVERGENCE'


def test_arpack_retry_uses_observed_refined_ritz_gap_and_preserves_original_target(tmp_path,monkeypatch):
    from scipy.sparse import linalg as sla
    a=sparse.diags([np.ones(39)*.08,np.linspace(-3.,4.,40),np.ones(39)*.08],[-1,0,1],format='csr')
    path=matrix_file(tmp_path,a)
    actual=core.sla.eigsh;calls={'n':0}
    def partial_once(*args,**kwargs):
        values,vectors=actual(*args,**kwargs)
        calls['n']+=1
        if calls['n']==1:raise sla.ArpackNoConvergence('controlled partial',values[:2],vectors[:,:2])
        return values,vectors
    monkeypatch.setattr(core.sla,'eigsh',partial_once)
    result=core.run_solver(path,config(target={'k':4,'sigma':.15},solver={'adaptive_shift':{'policy':'observed_ritz_gap_v2'}}),tmp_path/'feedback')
    assert len(result['attempts'])==2
    decision=result['attempts'][1]['shift_decision']
    assert decision['reason']=='observed_ritz_gap' and decision['observed_values']>=2
    assert all(attempt['sigma_target']==.15 for attempt in result['attempts'])
    assert result['validation']['numerical_status']=='PASSED'


def test_block_retry_learns_ritz_feedback_after_a_real_linear_failure(tmp_path,monkeypatch):
    from emsolver.numerics import block
    a=sparse.diags([np.ones(59)*.08,np.linspace(-3.,4.,60),np.ones(59)*.08],[-1,0,1],format='csr')
    path=matrix_file(tmp_path,a)
    actual=block.sla.splu;factorizations={'n':0}
    class FailAfterFirstSolve:
        def __init__(self,factor):self.factor=factor;self.calls=0
        def solve(self,right):
            self.calls+=1
            if self.calls>1:raise RuntimeError('LINEAR_RESIDUAL_FAILED: controlled boundary')
            return self.factor.solve(right)
    def factor(*args,**kwargs):
        lu=actual(*args,**kwargs);factorizations['n']+=1
        return FailAfterFirstSolve(lu) if factorizations['n']==1 else lu
    monkeypatch.setattr(block.sla,'splu',factor)
    result=core.run_solver(path,config(solver={'method':'block_shift_invert','adaptive_shift':{'policy':'observed_ritz_gap_v2'}}),tmp_path/'block-feedback')
    assert result['attempts'][0]['status']=='FAILED'
    assert result['attempts'][1]['shift_decision']['reason']=='observed_ritz_gap'
    assert result['attempts'][1]['shift_decision']['failures_observed']>=1
    assert result['n_accepted']==3 and result['validation']['numerical_status']=='PASSED'


def test_legacy_shift_policy_retains_pinned_scale_sequence_and_new_default_is_versioned():
    from emsolver.numerics.shift_control import create_shift_policy
    assert config()['solver']['adaptive_shift']['policy']=='observed_ritz_gap_v2'
    policy=create_shift_policy(.5,1e-6,4,'feedback_bounded_v1')
    shifts=[]
    for _ in range(4):
        shifts.append(policy.next_shift())
        policy.observe(np.array([.3,.6,.9]),error_code='LINEAR_RESIDUAL_FAILED')
    assert shifts==[.5,.500001,.499999,.50001]
    assert policy.last_decision['observed_values']==0
    assert policy.last_decision['controller_version']=='feedback_bounded_v1'


def test_owned_solver_events_report_actual_lock_indices_and_stagnation_state(tmp_path):
    path=matrix_file(tmp_path,sparse.diags([np.ones(39)*.08,np.linspace(-3.,4.,40),np.ones(39)*.08],[-1,0,1],format='csr'))
    events=[]
    core.run_solver(path,config(solver={'method':'block_shift_invert'}),tmp_path/'events',event_callback=events.append)
    checks=[item for item in events if item['event']=='block_residual_checked']
    assert checks and all(isinstance(item['locked_indices'],list) for item in checks)
    assert all(item['stalled_checks']>=0 and item['stagnation_threshold']==20 for item in checks)
    assert all(len(item['locked_indices'])==item['metrics']['soft_locked_modes'] for item in checks)


def test_physical_work_shift_overflow_is_rejected_before_factorization(tmp_path,monkeypatch):
    a=sparse.diags([np.ones(7)*.05,np.full(8,2.),np.ones(7)*.05],[-1,0,1],format='csr')
    path=matrix_file(tmp_path,a)
    calls=[]
    def failed(matrix):
        calls.append(1)
        raise RuntimeError('FACTORIZATION_FAILED: controlled failure')
    monkeypatch.setattr(core.sla,'splu',failed)
    result=core.run_solver(path,config(target={'k':1,'sigma':np.finfo(float).max}),tmp_path/'huge')
    errors=[item.get('error_code') for item in result['attempts']]
    assert 'NUMERICAL_SCALE_LIMIT' in errors
    assert len(calls)<len(result['attempts'])
    assert all(item['sigma_work'] is None or np.isfinite(item['sigma_work']) for item in result['attempts'])


@pytest.mark.parametrize('method',['arpack','auto','block_shift_invert','jacobi_davidson'])
def test_diagonal_fast_path_reports_actual_algorithm_through_plan_events_and_result(tmp_path,method):
    from emsolver.numerics import algorithm_plan
    solver={'method':method}
    if method=='jacobi_davidson':
        solver.update(transform='precond',linear={'method':'gmres','preconditioner':'jacobi','precision':'mixed_float32',
                      'adaptive_accuracy':{'enabled':True}},block={'checkpoint_interval':2})
    cfg=config(target={'k':2,'sigma':0.},solver=solver)
    a=sparse.diags([-3.,-.1,0.,.2,2.,4.],format='csr')
    stats=core.inspect_matrix(a)
    plan=algorithm_plan(cfg,matrix_statistics=stats)
    assert plan['requested_method']==method and plan['actual_method']=='structural_diagonal'
    path=matrix_file(tmp_path,a)
    events=[]
    result=core.run_solver(path,cfg,tmp_path/'diagonal',event_callback=events.append)
    strategy=result['strategy']
    assert strategy['actual_method']=='structural_diagonal' and strategy['requested_method']==method
    assert strategy['implementation']['candidate_solver']=='analytic_diagonal_selection'
    assert strategy['transform']=='none' and strategy['linear_solver']['method']=='not_used'
    assert strategy['linear_solver']['preconditioner']=='not_used'
    assert strategy['linear_solver']['adaptive_tolerance'] is False
    assert strategy['linear_solver']['mixed_precision'] is False
    assert strategy['shift_policy']['applies'] is False
    assert strategy['checkpoint']['warm_start'] is False
    assert strategy['monitoring']['outer_iteration_residual_history'] is False
    assert result['metrics']['method']=='structural_diagonal'
    assert result['metrics']['factorizations']==result['metrics']['outer_iterations']==result['metrics']['linear_solves']==0
    assert result['metrics']['linear_iterations']==0
    assert result['metrics']['unavailable_metrics']['linear_iterations'] is None
    prepared=next(item for item in events if item['event']=='backend_prepared')
    assert prepared['metrics']['strategy']['actual_method']=='structural_diagonal'
    assert json.loads((tmp_path/'diagonal/strategy.json').read_text())==strategy
    assert not (tmp_path/'diagonal/subspace.checkpoint.npz').exists()


def test_preflight_without_diagonal_evidence_preserves_requested_iterative_strategy():
    from emsolver.numerics import algorithm_plan
    cfg=config(solver={'method':'block_shift_invert'})
    for statistics in (None,{'is_diagonal':False},{'is_diagonal':'true'}):
        assert algorithm_plan(cfg,matrix_statistics=statistics)['actual_method']=='block_shift_invert'
