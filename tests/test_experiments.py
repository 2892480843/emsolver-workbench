import copy
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from emsolver.errors import ServiceError
from emsolver.contracts import content_hash
from emsolver.experiments import ExperimentService, ExperimentSpec, config_diff, summarize_batch
from emsolver.service import Service


@pytest.fixture
def prepared(tmp_path):
    service = Service(tmp_path, start_runner=False)
    project = service.create_project('Experiments')
    matrix = service.generate_sample(project['id'], n=40)
    spec = {'project_id': project['id'], 'matrix_ids': [matrix['id']],
            'base_config': {'resources': {'walltime_seconds': 10, 'disk_limit_gib': 2}},
            'k_values': [2], 'repeats': 2}
    yield service, ExperimentService(service), project, matrix, spec
    service.close()


def database_snapshot(service):
    return {name: service.store.db.execute(f'SELECT * FROM {name}').fetchall()
            for name in ('entities', 'audit', 'events', 'idempotency')}


def test_plan_deduplicates_axes_and_uses_first_cell_of_each_matrix_as_baseline(prepared):
    service, experiments, project, matrix, spec = prepared
    second = service.generate_sample(project['id'], n=50)
    spec.update(matrix_ids=[matrix['id'], matrix['id'], second['id']],
                k_values=[2, 2, 3], sigma_values=[0, 0.0], repeats=2)
    untouched = copy.deepcopy(spec)
    plan = experiments.plan(spec)
    assert spec == untouched
    assert [cell['id'] for cell in plan['cells']] == ['c001', 'c002', 'c003', 'c004']
    assert plan['definition']['matrix_ids'] == [matrix['id'], second['id']]
    assert plan['definition']['k_values'] == [2, 3]
    assert plan['budget']['total_runs'] == 8
    assert plan['budget']['total_walltime_seconds'] == 80
    assert plan['budget']['total_disk_gib'] == 16
    assert plan['budget']['peak_memory_limit_gib'] == 16
    assert plan['cells'][0]['configuration_diff'] == plan['cells'][2]['configuration_diff'] == []
    assert plan['cells'][1]['configuration_diff'] == [{'path': 'target.k', 'before': 2, 'after': 3}]
    assert service.store.count('configs') == service.store.count('plans') == 4
    assert service.store.count('jobs') == 0
    plan['cells'][0]['config']['target']['k'] = 30
    assert service.store.get('experiment_plans', plan['id'])['cells'][0]['config']['target']['k'] == 2


@pytest.mark.parametrize('patch', [
    {'k_values': [True]}, {'threads_values': [True]}, {'adaptive_shift_values': [1]},
    {'sigma_values': [float('nan')]}, {'sigma_values': [float('inf')]},
    {'memory_limit_gib_values': [0]}, {'threads_values': [257]}, {'k_values': [10001]},
    {'repeats': True}, {'matrix_ids': []}, {'unknown': 1},
    {'budget': {'max_runs': True}}, {'budget': {'max_total_disk_gib': float('inf')}},
    {'base_config': {'output': {'eigenvectors': 'true'}}},
    {'base_config': {'resources': {'walltime_seconds': True}}},
    {'base_config': {'target': {'mode': 'interval_all', 'interval': {'lower': 0, 'upper': 1}}}},
])
def test_strict_invalid_spec_leaves_every_table_unchanged(prepared, patch):
    service, experiments, _, _, spec = prepared
    before = database_snapshot(service)
    with pytest.raises(ServiceError):
        experiments.plan({**spec, **patch})
    assert database_snapshot(service) == before


@pytest.mark.parametrize('patch', [
    {'budget': {'max_runs': 1}},
    {'budget': {'max_total_walltime_seconds': 19}},
    {'budget': {'max_total_disk_gib': 3}},
    {'k_values': list(range(1, 101)), 'sigma_values': [0, 1]},
])
def test_budget_and_product_rejected_before_any_freeze(prepared, monkeypatch, patch):
    service, experiments, _, _, spec = prepared
    def no_write(**kwargs):
        pytest.fail('Budget must be checked before a configuration or solve plan is written')
    monkeypatch.setattr(service, 'create_plan', no_write)
    before = database_snapshot(service)
    with pytest.raises(ServiceError):
        experiments.plan({**spec, **patch})
    assert database_snapshot(service) == before


def test_invalid_later_cell_rolls_back_config_plan_and_audit(prepared):
    service, experiments, _, _, spec = prepared
    before = database_snapshot(service)
    with pytest.raises(ServiceError) as failure:
        experiments.plan({**spec, 'k_values': [2, 40]})
    assert failure.value.code == 'INVALID_K'
    assert database_snapshot(service) == before


def test_foreign_and_archived_assets_do_not_freeze(prepared):
    service, experiments, project, matrix, spec = prepared
    foreign = service.create_project('Foreign')
    with pytest.raises(ServiceError) as failure:
        experiments.plan({**spec, 'project_id': foreign['id']})
    assert failure.value.code == 'PROJECT_MISMATCH'
    service.archive_matrix(matrix['id'])
    with pytest.raises(ServiceError) as failure:
        experiments.plan(spec)
    assert failure.value.code == 'MATRIX_ARCHIVED'
    service.update_project(project['id'], archived=True)
    with pytest.raises(ServiceError) as failure:
        experiments.plan(spec)
    assert failure.value.code == 'PROJECT_ARCHIVED'
    assert service.store.count('configs') == service.store.count('plans') == 0


def test_submit_links_repeats_and_is_idempotent_after_archival(prepared):
    service, experiments, project, matrix, spec = prepared
    plan = experiments.plan({**spec, 'k_values': [2, 3]})
    batch = experiments.submit(plan['id'])
    assert batch['id'] == 'b_' + plan['id'].removeprefix('ep_')
    assert batch['experiment_plan_id'] == plan['id'] and batch['experiment_hash'] == plan['hash']
    assert len(batch['jobs']) == len(batch['runs']) == 4
    assert [run['experiment'] for run in batch['runs']] == [
        {'batch_id': batch['id'], 'cell_id': cell, 'repeat': repeat}
        for cell in ('c001', 'c002') for repeat in (1, 2)]
    assert batch['matrix_ids'] == [matrix['id']] and len(batch['plan_hashes']) == 2
    service.archive_matrix(matrix['id'])
    service.update_project(project['id'], archived=True)
    before = database_snapshot(service)
    assert experiments.submit(plan['id'])['jobs'] == batch['jobs']
    assert database_snapshot(service) == before


@pytest.mark.parametrize('kind,field', [
    ('experiment_plans', 'cells'), ('plans', 'config'), ('configs', 'config'),
])
def test_tampered_frozen_plan_or_config_rejects_submit(prepared, kind, field):
    service, experiments, _, _, spec = prepared
    plan = experiments.plan(spec)
    solve_plan = service.store.get('plans', plan['cells'][0]['plan_id'])
    identity = {'experiment_plans': plan['id'], 'plans': solve_plan['id'], 'configs': solve_plan['config_id']}[kind]
    value = service.store.get(kind, identity)
    if field == 'cells': value['cells'][0]['parameters']['k'] = 3
    else: value['config']['target']['k'] = 3
    service.store.put(kind, identity, value)
    before = database_snapshot(service)
    with pytest.raises(ServiceError) as failure:
        experiments.submit(plan['id'])
    assert failure.value.status == 409
    assert database_snapshot(service) == before


@pytest.mark.parametrize('mutation', ['definition_budget', 'axis_config', 'diff', 'unnormalized_definition'])
def test_resealed_plan_rejects_definition_config_and_baseline_mismatches(prepared, mutation):
    service, experiments, _, _, spec = prepared
    plan = experiments.plan(spec)
    if mutation == 'definition_budget':
        plan['definition']['base_config']['resources']['walltime_seconds'] = 0.1
        plan['budget']['total_walltime_seconds'] = 0.1 * plan['budget']['total_runs']
    elif mutation == 'axis_config':
        plan['definition']['sigma_values'] = [0.5]
        plan['cells'][0]['parameters']['sigma'] = 0.5
    elif mutation == 'diff':
        plan['cells'][0]['configuration_diff'] = [{'path': 'target.k', 'before': 1, 'after': 2}]
    else:
        plan['definition']['k_values'].append(plan['definition']['k_values'][0])
    plan['hash'] = content_hash({key: value for key, value in plan.items() if key != 'hash'})
    service.store.put('experiment_plans', plan['id'], plan)
    before = database_snapshot(service)
    with pytest.raises(ServiceError) as failure:
        experiments.submit(plan['id'])
    assert failure.value.code == 'EXPERIMENT_PLAN_MISMATCH'
    assert database_snapshot(service) == before


def test_shared_frozen_validation_uses_only_lookup_and_preserves_snapshot(prepared):
    from emsolver.experiments import validate_frozen_plan
    service, experiments, _, _, spec = prepared
    plan = experiments.plan({**spec, 'k_values': [2, 3]})
    snapshot = copy.deepcopy(plan)
    entities = {(kind, item['id']): item for kind in ('plans', 'configs', 'matrices')
                for item in service.store.list(kind)}
    for (kind, _), item in entities.items():
        if kind == 'matrices':
            item.pop('_path')
            item['archived'] = True
    assert validate_frozen_plan(plan, lambda kind, identity: entities.get((kind, identity))) == snapshot
    assert plan == snapshot


def test_submit_rechecks_staleness_and_rolls_back_prior_jobs_events_and_audit(prepared, monkeypatch):
    service, experiments, _, _, spec = prepared
    plan = experiments.plan({**spec, 'k_values': [2, 3]})
    original = service.submit_job
    attempts = []
    def fail_second(plan_id, *args, **kwargs):
        attempts.append(plan_id)
        if len(attempts) == 2:
            raise ServiceError('STALE_PLAN', status=409)
        return original(plan_id, *args, **kwargs)
    monkeypatch.setattr(service, 'submit_job', fail_second)
    before = database_snapshot(service)
    with pytest.raises(ServiceError) as failure:
        experiments.submit(plan['id'])
    assert failure.value.code == 'STALE_PLAN' and len(attempts) == 2
    assert database_snapshot(service) == before


def test_changed_runtime_capabilities_reject_frozen_plan(prepared, monkeypatch):
    service, experiments, _, _, spec = prepared
    plan = experiments.plan(spec)
    before = database_snapshot(service)
    capabilities = service.capabilities()
    monkeypatch.setattr(service, 'capabilities', lambda: {**capabilities, 'changed': True})
    with pytest.raises(ServiceError) as failure:
        experiments.submit(plan['id'])
    assert failure.value.code == 'STALE_PLAN'
    assert database_snapshot(service) == before


def test_changed_input_file_rejects_frozen_plan_without_submission(prepared):
    from pathlib import Path
    service, experiments, _, matrix, spec = prepared
    plan = experiments.plan(spec)
    asset = service.store.get('matrices', matrix['id'])
    path = Path(asset['_path'])
    path.chmod(0o600)
    path.write_bytes(b'changed input')
    before = database_snapshot(service)
    with pytest.raises(ServiceError) as failure:
        experiments.submit(plan['id'])
    assert failure.value.code == 'STALE_PLAN'
    assert database_snapshot(service) == before


def test_plan_audit_failure_rolls_back_all_frozen_entities(prepared, monkeypatch):
    service, experiments, _, _, spec = prepared
    before = database_snapshot(service)
    def fail_audit(*args, **kwargs):
        raise OSError('audit failed')
    monkeypatch.setattr(service.store, 'audit', fail_audit)
    with pytest.raises(OSError, match='audit failed'):
        experiments.plan(spec)
    assert database_snapshot(service) == before


def test_concurrent_submissions_atomically_recheck_queue_capacity(prepared):
    service, experiments, _, _, spec = prepared
    plans = [experiments.plan(spec) for _ in range(2)]
    for index in range(98):
        service.store.put('jobs', f'existing-{index}', {'id': f'existing-{index}', 'status': 'QUEUED'})
    barrier = threading.Barrier(2)
    def submit(plan):
        barrier.wait(timeout=3)
        try: return experiments.submit(plan['id'])
        except ServiceError as failure: return failure
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, plans))
    assert sum(isinstance(value, dict) for value in results) == 1
    assert service.jobs.queued_count() == 100
    assert service.store.count('benchmarks') == 1
    failures = [value for value in results if isinstance(value, ServiceError)]
    assert failures[0].code == 'BENCHMARK_QUEUE_LIMIT'


def test_separate_service_connections_serialize_queue_capacity(prepared):
    service, experiments, _, _, spec = prepared
    plans = [experiments.plan(spec) for _ in range(2)]
    for index in range(98):
        service.store.put('jobs', f'existing-{index}', {'id': f'existing-{index}', 'status': 'QUEUED'})
    second = Service(service.root, start_runner=False)
    try:
        barrier = threading.Barrier(2)
        def submit(pair):
            domain, plan = pair
            barrier.wait(timeout=3)
            try: return domain.submit(plan['id'])
            except ServiceError as failure: return failure
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(submit, [(experiments, plans[0]), (ExperimentService(second), plans[1])]))
        assert sum(isinstance(value, dict) for value in results) == 1
        assert service.jobs.queued_count() == 100
        assert service.store.count('benchmarks') == 1
    finally:
        second.close()


def test_diff_is_sorted_detached_and_records_added_and_removed_fields():
    before = {'z': [1, 2], 'target': {'k': 2, 'sigma': 0}, 'old': 3}
    after = {'z': [1, 3], 'target': {'k': 3, 'sigma': 0}, 'new': True}
    result = config_diff(before, after)
    assert result == [{'path': 'new', 'before': None, 'after': True},
                      {'path': 'old', 'before': 3, 'after': None},
                      {'path': 'target.k', 'before': 2, 'after': 3},
                      {'path': 'z', 'before': [1, 2], 'after': [1, 3]}]
    result[-1]['after'].append(4)
    assert after['z'] == [1, 3]


def test_diff_expands_added_and_removed_nested_fields():
    assert config_diff({'old': {'enabled': True}}, {'new': {'k': 2}}) == [
        {'path': 'new.k', 'before': None, 'after': 2},
        {'path': 'old.enabled', 'before': True, 'after': None}]


def test_summary_keeps_all_statuses_and_separates_finite_certified_times():
    jobs = [{'id': str(index), 'status': status, 'metrics': {'walltime_seconds': seconds},
             'experiment': {'cell_id': 'c001' if index < 3 else 'c002'}}
            for index, (status, seconds) in enumerate([
                ('SUCCEEDED', 4), ('FAILED', 20), ('PARTIAL', 8), ('CANCELLED', None),
                ('TIMED_OUT', 10), ('SUCCEEDED', float('nan')), ('SUCCEEDED', float('inf')),
                ('RUNNING', True), ('SUCCEEDED', -1)])]
    batch = {'cells': [{'id': 'c001', 'matrix_id': 'm', 'parameters': {'k': 2}, 'configuration_diff': []},
                       {'id': 'c002', 'matrix_id': 'm', 'parameters': {'k': 3},
                        'configuration_diff': [{'path': 'target.k', 'before': 2, 'after': 3}]}]}
    value = summarize_batch(batch, jobs)
    summary = value['summary']
    assert summary['total'] == 9 and summary['terminal'] == 8
    assert summary['failed'] == 3 and summary['partial'] == 1 and summary['certified'] == 4
    assert summary['timing_denominator'] == 4 and summary['time_median_seconds'] == 9
    assert summary['time_range_seconds'] == [4, 20]
    assert summary['certified_timing_denominator'] == 1
    assert summary['certified_time_median_seconds'] == 4
    assert summary['certified_time_range_seconds'] == [4, 4]
    assert [group['job_ids'] for group in value['groups']] == [['0', '1', '2'], ['3', '4', '5', '6', '7', '8']]
    assert value['groups'][0]['summary']['total'] == 3
    assert 'speedup' not in value['groups'][0]
    legacy = summarize_batch({}, jobs[:3])
    assert legacy['groups'] == [] and legacy['summary']['timing_denominator'] == 3


def test_request_model_remains_strict_for_api_callers(prepared):
    _, _, _, _, spec = prepared
    with pytest.raises(ValueError): ExperimentSpec.model_validate({**spec, 'repeats': '2'})


def test_default_base_config_is_fully_normalized(prepared):
    _, experiments, _, _, spec = prepared
    plan = experiments.plan({key: value for key, value in spec.items() if key != 'base_config'})
    assert plan['definition']['base_config']['target']['mode'] == 'nearest_k'
    assert plan['definition']['base_config']['resources']['walltime_seconds'] == 1800


def test_summary_medians_remain_finite_for_large_finite_measurements():
    jobs = [{'id': str(index), 'status': 'SUCCEEDED', 'metrics': {'walltime_seconds': 1e308}}
            for index in range(2)]
    summary = summarize_batch({}, jobs)['summary']
    assert summary['time_median_seconds'] == summary['certified_time_median_seconds'] == 1e308


def test_frozen_v030_defaults_remain_compatible_without_rewriting_hashes(prepared):
    from emsolver.experiments import validate_frozen_plan
    from emsolver.service import digest
    service, experiments, _, _, spec=prepared
    plan=experiments.plan({**spec,'k_values':[2,3]})
    def legacy(config):
        value=copy.deepcopy(config)
        for key in ('reordering','nullspace'):value['problem'].pop(key,None)
        value['solver'].pop('block',None)
        value['solver']['linear'].pop('adaptive_accuracy',None)
        value['observability'].pop('enabled',None)
        return value
    plan['definition']['base_config']=legacy(plan['definition']['base_config'])
    for cell in plan['cells']:
        cell['config']=legacy(cell['config'])
        frozen=service.store.get('plans',cell['plan_id'])
        config=service.store.get('configs',frozen['config_id'])
        config['config']=copy.deepcopy(cell['config']);config['hash']=digest(config['config'])
        service.store.put('configs',config['id'],config)
        frozen['config']=copy.deepcopy(cell['config'])
        frozen['hash']=digest({'config':config['hash'],'matrix':cell['matrix_hash'],'capabilities':frozen['capabilities']})
        service.store.put('plans',frozen['id'],frozen)
        cell['plan_hash']=frozen['hash']
    plan['hash']=content_hash({key:value for key,value in plan.items() if key!='hash'})
    original=copy.deepcopy(plan)
    assert validate_frozen_plan(plan,service.store.get)==original
    assert plan==original
    plan['definition']['base_config']['validation']['residual_scaled_max']=1e-4
    plan['hash']=content_hash({key:value for key,value in plan.items() if key!='hash'})
    with pytest.raises(ServiceError):validate_frozen_plan(plan,service.store.get)
