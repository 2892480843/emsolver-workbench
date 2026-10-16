import copy
import json
from pathlib import Path

import pytest

from emsolver.protocols import freeze_protocol, execute_protocol


def specification():
    return {'schema_version': '1.0', 'repeats': 1,
            'base_config': {'target': {'k': 2, 'sigma': .1}, 'solver': {'subspace_max': 10},
                            'resources': {'threads': 1, 'walltime_seconds': 10}},
            'cases': [{'id': 'development-case', 'split': 'development', 'input': {'kind': 'complex', 'n': 20, 'seed': 1}},
                      {'id': 'holdout-case', 'split': 'holdout', 'input': {'kind': 'complex', 'n': 20, 'seed': 2}}],
            'variants': [{'id': 'baseline'}]}


def source(tmp_path):
    root = tmp_path / 'source'
    (root / 'emsolver').mkdir(parents=True)
    (root / 'emsolver' / 'module.py').write_text('version = 1\n', encoding='utf-8')
    return root


def test_real_protocol_executes_frozen_development_and_holdout_rows(tmp_path):
    root = source(tmp_path)
    plan = freeze_protocol(specification(), tmp_path / 'frozen', source_root=root)
    assert plan['hash'] and len(plan['rows']) == 2
    result = execute_protocol(tmp_path / 'frozen' / 'protocol.json', tmp_path / 'execution', source_root=root)
    assert result['summary']['prescribed_runs'] == 2
    assert result['summary']['executed_runs'] == 2
    assert all(row['status'] in {'SUCCEEDED', 'PARTIAL'} for row in result['runs'])
    assert all(row['validation']['numerical_status'] == 'PASSED' for row in result['runs'])
    assert (tmp_path / 'execution' / 'report.html').is_file()
    with pytest.raises(ValueError, match='OUTPUT_EXISTS'):
        execute_protocol(tmp_path / 'frozen' / 'protocol.json', tmp_path / 'execution', source_root=root)


def test_unsupported_ablations_failures_and_timeouts_stay_in_denominator(tmp_path):
    root = source(tmp_path)
    spec = specification()
    spec['repeats'] = 2
    spec['variants'] += [{'id': kind, 'ablation': kind} for kind in ('preconditioning', 'subspace_control', 'mixed_precision')]
    plan = freeze_protocol(spec, tmp_path / 'frozen', source_root=root)
    assert len(plan['rows']) == 16
    calls = []
    def executor(matrix, config, output):
        calls.append((matrix, config))
        return {'status': 'TIMED_OUT' if len(calls) % 2 else 'FAILED', 'error_code': 'WALLTIME_EXCEEDED',
                'validation': {'numerical_status': 'NOT_VALIDATED', 'target_certification': 'UNVERIFIED'}, 'metrics': {}}
    result = execute_protocol(tmp_path / 'frozen' / 'protocol.json', tmp_path / 'executed', source_root=root, executor=executor)
    assert len(calls) == 4
    assert result['summary']['prescribed_runs'] == 16
    assert result['summary']['blocked_runs'] == 12
    assert result['summary']['timeout_runs'] == 2 and result['summary']['failed_runs'] == 2
    assert all(row['blocked_reason'] for row in result['runs'] if row['status'] == 'BLOCKED')


@pytest.mark.parametrize('target', ['input', 'config', 'source', 'plan_hash'])
def test_stale_or_tampered_protocol_is_rejected_before_output_creation(tmp_path, target):
    root = source(tmp_path)
    plan = freeze_protocol(specification(), tmp_path / 'frozen', source_root=root)
    path = tmp_path / 'frozen' / 'protocol.json'
    if target == 'input':
        input_path = tmp_path / 'frozen' / plan['inputs'][0]['path']
        input_path.chmod(0o600)
        input_path.write_bytes(b'changed')
    elif target == 'config':
        plan['rows'][0]['config']['target']['k'] = 3
        path.chmod(0o600)
        path.write_text(json.dumps(plan), encoding='utf-8')
    elif target == 'source':
        (root / 'emsolver' / 'module.py').write_text('version = 2\n', encoding='utf-8')
    else:
        plan['hash'] = '0' * 64
        path.chmod(0o600)
        path.write_text(json.dumps(plan), encoding='utf-8')
    with pytest.raises(ValueError): execute_protocol(path, tmp_path / 'execution', source_root=root)
    assert not (tmp_path / 'execution').exists()


def test_case_labels_and_input_paths_never_select_a_strategy(tmp_path):
    root = source(tmp_path)
    spec = specification()
    first = freeze_protocol(spec, tmp_path / 'first', source_root=root)
    renamed = copy.deepcopy(spec)
    renamed['cases'][0]['id'] = 'publicly-renamed-case'
    renamed['cases'][1]['id'] = 'another-path-independent-label'
    second = freeze_protocol(renamed, tmp_path / 'second', source_root=root)
    assert [row['config'] for row in first['rows']] == [row['config'] for row in second['rows']]
    assert [row['sha256'] for row in first['inputs']] == [row['sha256'] for row in second['inputs']]


def test_holdout_split_is_explicit_disjoint_and_fair_variants_preserve_contract(tmp_path):
    root = source(tmp_path)
    spec = specification()
    spec['cases'][1]['split'] = 'development'
    with pytest.raises(ValueError): freeze_protocol(spec, tmp_path / 'no_holdout', source_root=root)
    spec = specification()
    spec['cases'][1]['input'] = copy.deepcopy(spec['cases'][0]['input'])
    with pytest.raises(ValueError): freeze_protocol(spec, tmp_path / 'same_data', source_root=root)
    spec = specification()
    spec['variants'].append({'id': 'relaxed', 'config': {'validation': {'residual_scaled_max': 1e-2}}})
    with pytest.raises(ValueError, match='UNFAIR_VARIANT'): freeze_protocol(spec, tmp_path / 'unfair', source_root=root)


def test_block_subspace_ablation_disables_restarts_without_relaxing_the_cap(tmp_path):
    root = source(tmp_path)
    spec = specification()
    spec['base_config']['solver']['method'] = 'block_shift_invert'
    spec['variants'].append({'id': 'no-restart', 'ablation': 'subspace_control'})
    plan = freeze_protocol(spec, tmp_path / 'frozen', source_root=root)
    enabled, disabled = plan['rows'][:2]
    assert disabled['status'] == 'READY'
    assert disabled['config']['solver']['block']['max_restarts'] == 0
    assert enabled['config']['solver']['block']['max_restarts'] > 0
    assert enabled['config']['solver']['subspace_max'] == disabled['config']['solver']['subspace_max']
    assert 'hard' in disabled['ablation_meaning']


def test_named_ablation_cannot_hide_unrelated_algorithm_changes(tmp_path):
    root = source(tmp_path)
    spec = specification()
    spec['variants'].append({'id': 'confounded', 'ablation': 'adaptive_shift',
                             'config': {'solver': {'max_outer_iterations': 7}}})
    with pytest.raises(ValueError, match='ABLATION_NOT_ISOLATED'):
        freeze_protocol(spec, tmp_path / 'unfair', source_root=root)


def test_jd_preconditioner_and_restart_ablation_require_capability_gate(tmp_path, monkeypatch):
    from emsolver import numerics
    actual = numerics.capabilities()
    declared = copy.deepcopy(actual)
    declared['scipy']['methods'].append('jacobi_davidson')
    declared['jacobi_davidson'] = {'supported': True, 'preconditioners': ['none', 'jacobi'], 'bounded_restarts': True}
    monkeypatch.setattr(numerics, 'capabilities', lambda: declared)
    spec = specification()
    spec['base_config']['solver'].update(method='jacobi_davidson', transform='precond',
                                         linear={'method': 'gmres', 'preconditioner': 'jacobi'})
    spec['variants'] += [{'id': 'no-jacobi', 'ablation': 'preconditioning'},
                         {'id': 'no-restart', 'ablation': 'subspace_control'}]
    plan = freeze_protocol(spec, tmp_path / 'frozen', source_root=source(tmp_path))
    baseline, unpreconditioned, no_restart = plan['rows'][:3]
    assert unpreconditioned['status'] == 'READY'
    assert unpreconditioned['config']['solver']['linear']['preconditioner'] == 'none'
    assert baseline['config']['solver']['linear']['preconditioner'] == 'jacobi'
    assert no_restart['status'] == 'READY'
    assert no_restart['config']['solver']['block']['max_restarts'] == 0


def test_real_jd_ablations_execute_actual_none_float64_and_no_restart_configs(tmp_path):
    root = source(tmp_path)
    spec = specification()
    spec['base_config']['solver'].update(method='jacobi_davidson', transform='precond', max_outer_iterations=100,
                                         linear={'method': 'gmres', 'preconditioner': 'jacobi', 'precision': 'mixed_float32'})
    spec['variants'] += [{'id': 'no-preconditioner', 'ablation': 'preconditioning'},
                         {'id': 'no-mixed-precision', 'ablation': 'mixed_precision'},
                         {'id': 'no-restart', 'ablation': 'subspace_control'}]
    plan = freeze_protocol(spec, tmp_path / 'frozen', source_root=root)
    assert all(row['status'] == 'READY' for row in plan['rows'])
    assert plan['rows'][1]['config']['solver']['linear']['preconditioner'] == 'none'
    assert plan['rows'][2]['config']['solver']['linear']['precision'] == 'float64'
    assert plan['rows'][3]['config']['solver']['block']['max_restarts'] == 0
    result = execute_protocol(tmp_path / 'frozen' / 'protocol.json', tmp_path / 'executed', source_root=root)
    assert result['summary']['executed_runs'] == 8
    assert result['summary']['blocked_runs'] == 0
    assert all(row['status'] in {'SUCCEEDED', 'PARTIAL', 'FAILED', 'TIMED_OUT'} for row in result['runs'])


def test_jd_target_sigma_is_not_mislabeled_as_a_working_shift_ablation(tmp_path):
    spec = specification()
    spec['base_config']['solver'].update(method='jacobi_davidson', transform='precond',
                                         linear={'method': 'gmres', 'preconditioner': 'none'})
    spec['variants'].append({'id': 'not-applicable', 'ablation': 'adaptive_shift'})
    plan = freeze_protocol(spec, tmp_path / 'frozen', source_root=source(tmp_path))
    baseline, unsupported = plan['rows'][:2]
    assert baseline['status'] == 'READY'
    assert unsupported['status'] == 'BLOCKED'
    assert 'not applicable' in unsupported['blocked_reason'].lower()
    assert unsupported['config'] == baseline['config']


def test_structural_diagonal_inputs_block_all_unused_named_ablations_but_keep_baseline(tmp_path):
    from emsolver.numerics import algorithm_plan, inspect_matrix, load_matrix
    for method in ('block_shift_invert', 'jacobi_davidson'):
        spec = specification()
        spec['cases'][0]['input'] = {'kind': 'near_singular', 'n': 20, 'seed': 1}
        spec['cases'][1]['input'] = {'kind': 'near_singular', 'n': 21, 'seed': 2}
        spec['base_config']['solver']['method'] = method
        if method == 'jacobi_davidson':
            spec['base_config']['solver'].update(transform='precond', linear={
                'method': 'gmres', 'preconditioner': 'jacobi', 'precision': 'mixed_float32'})
        spec['variants'] += [{'id': kind, 'ablation': kind} for kind in
                            ('adaptive_shift', 'preconditioning', 'subspace_control', 'mixed_precision')]
        directory = tmp_path / method
        plan = freeze_protocol(spec, directory)
        for item in plan['inputs']:
            statistics = inspect_matrix(load_matrix(directory / item['path']))
            assert statistics['is_diagonal'] is True
            baseline = next(row for row in plan['rows'] if row['case_id'] == item['case_id'] and row['ablation'] is None)
            assert baseline['status'] == 'READY'
            assert algorithm_plan(baseline['config'], matrix_statistics=statistics)['actual_method'] == 'structural_diagonal'
        ablations = [row for row in plan['rows'] if row['ablation']]
        assert len(ablations) == 8
        assert all(row['status'] == 'BLOCKED' for row in ablations)
        assert all('structural_diagonal' in row['blocked_reason'] for row in ablations)
