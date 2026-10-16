import copy
import hashlib
import json
import sqlite3
import zipfile

import pytest
from fastapi.testclient import TestClient

from emsolver.api import create_app
from emsolver.service import Service, ServiceError


def prepare(service):
    project = service.create_project('Experiments')
    matrix = service.generate_sample(project['id'], n=32)
    config = {'problem': {'matrix_id': matrix['id']}, 'target': {'k': 2},
              'resources': {'threads': 1, 'walltime_seconds': 5, 'disk_limit_gib': .05}}
    return project, matrix, config


def spec(project, matrix, config):
    return {'project_id': project['id'], 'matrix_ids': [matrix['id']], 'base_config': config,
            'k_values': [2, 3], 'sigma_values': [0], 'threads_values': [1],
            'memory_limit_gib_values': [16], 'adaptive_shift_values': [True], 'repeats': 2}


def test_experiment_api_freezes_previews_submits_once_and_pages_lightweight(tmp_path):
    with TestClient(create_app(tmp_path, start_runner=False)) as client:
        service = client.app.state.service
        project, matrix, config = prepare(service)
        response = client.post('/api/v1/experiments/plans', json=spec(project, matrix, config))
        assert response.status_code == 201, response.text
        frozen = response.json()
        assert len(frozen['cells']) == 2 and service.store.count('jobs') == 0
        assert client.get('/api/v1/experiments/plans/' + frozen['id']).json() == frozen
        first = client.post('/api/v1/experiments/submit', json={'experiment_plan_id': frozen['id']})
        assert first.status_code == 201, first.text
        batch = first.json()
        again = client.post('/api/v1/experiments/submit', json={'experiment_plan_id': frozen['id']}).json()
        assert batch['id'] == again['id'] and service.store.count('jobs') == 4
        assert len(batch['groups']) == 2
        for job in batch['runs']:
            assert job['experiment']['batch_id'] == batch['id']
            assert job['experiment']['repeat'] in {1, 2}
        page = client.get('/api/v1/benchmarks/page', params={'limit': 1, 'project_id': project['id']}).json()
        assert page['total'] == 1 and 'runs' not in page['items'][0] and 'cells' not in page['items'][0]
        assert page['items'][0]['summary']['total'] == 4
        assert client.get('/api/v1/benchmarks/page', params={'cursor': 'broken'}).status_code == 400
        assert client.get('/api/v1/benchmarks/page', params={'limit': 201}).status_code == 422
        cancelled = client.post('/api/v1/benchmarks/' + batch['id'] + '/cancel').json()
        assert cancelled['summary']['failed'] == 4
        assert all(job['status'] == 'CANCELLED' for job in cancelled['runs'])
        assert client.post('/api/v1/benchmarks/' + batch['id'] + '/cancel').status_code == 200
        assert client.get('/api/v1/benchmarks').json()[0]['jobs'] == batch['jobs']


@pytest.mark.parametrize('changes', [
    {'target': {'sigma': 1}}, {'problem': {'storage': 'upper'}},
    {'validation': {'residual_scaled_max': 1e-2}},
    {'resources': {'threads': 2}}, {'resources': {'memory_limit_gib': 32}},
    {'resources': {'walltime_seconds': 6}}, {'resources': {'disk_limit_gib': 1}},
    {'output': {'eigenvectors': False}}, {'solver': {'unknown': True}},
    {'solver': None}, {'solver': {'subspace_max': True}},
])
def test_advice_rejects_protected_or_invalid_changes_without_writes(tmp_path, changes):
    service = Service(tmp_path, start_runner=False)
    _, _, config = prepare(service)
    before = service.store.count('configs')
    with pytest.raises(ServiceError):
        service.apply_advice(config=config, changes=changes)
    assert service.store.count('configs') == before
    service.close()


def test_advice_server_returns_diff_and_retains_frozen_original(tmp_path):
    with TestClient(create_app(tmp_path, start_runner=False)) as client:
        service = client.app.state.service
        _, _, config = prepare(service)
        original = copy.deepcopy(config)
        response = client.post('/api/v1/configs/advice', json={
            'config': config, 'changes': {'solver': {'subspace_max': 60}}})
        assert response.status_code == 201, response.text
        item = response.json()
        assert config == original
        assert item['configuration_diff'] == [{'path': 'solver.subspace_max', 'before': 100, 'after': 60}]
        assert item['config']['resources']['walltime_seconds'] == 5
        assert item['derivation']['source'] == 'rules_advice'
        assert client.get('/api/v1/configs/' + item['id']).json() == item
        assert client.post('/api/v1/configs/advice', json={'config': config, 'job_id': 'missing', 'changes': {}}).status_code == 400


def test_experiment_backup_restores_new_entities_and_idempotent_membership(tmp_path):
    source = Service(tmp_path / 'source', start_runner=False)
    project, matrix, config = prepare(source)
    plan = source.create_experiment_plan(spec(project, matrix, config))
    batch = source.submit_experiment(plan['id'])
    backup = source.backup()
    source.restore_backup(backup, tmp_path / 'restored')
    restored = Service(tmp_path / 'restored', start_runner=False)
    assert restored._get('experiment_plans', plan['id'])['hash'] == plan['hash']
    assert restored.submit_experiment(plan['id'])['jobs'] == batch['jobs']
    assert restored.store.count('jobs') == 4
    restored.close()
    source.close()


def test_experiment_report_keeps_conditions_diff_and_all_failed_runs(tmp_path):
    service = Service(tmp_path, start_runner=False)
    project, matrix, config = prepare(service)
    plan = service.create_experiment_plan(spec(project, matrix, config))
    batch = service.submit_experiment(plan['id'])
    service.cancel_benchmark(batch['id'])
    report = service.create_report(benchmark_id=batch['id'])
    text = service.report_path(report['id']).read_text(encoding='utf-8')
    assert '参数组合' in text and '重复编号' in text and '配置差异' in text
    for job_id in batch['jobs']:
        assert job_id in text
    assert 'certified_timing_denominator' in text
    assert 'target.k' in text
    assert plan['hash'] in text
    service.close()


def test_batch_cancel_rolls_back_every_state_when_audit_fails(tmp_path, monkeypatch):
    service = Service(tmp_path, start_runner=False)
    project, matrix, _ = prepare(service)
    batch = service.create_benchmark(project['id'], [matrix['id']], k=2, repeats=2)
    original = service.store.audit
    def fail(action, *args, **kwargs):
        if action == 'benchmark.cancel':
            raise OSError('injected')
        return original(action, *args, **kwargs)
    monkeypatch.setattr(service.store, 'audit', fail)
    with pytest.raises(OSError):
        service.cancel_benchmark(batch['id'])
    assert all(service.get_job(job_id)['status'] == 'QUEUED' for job_id in batch['jobs'])
    assert all(len(service.events(job_id)) == 1 for job_id in batch['jobs'])
    service.close()


@pytest.mark.parametrize('mutation', ['plan_hash', 'cell_reference', 'job_membership',
    'crossed_cell', 'duplicate_repeat', 'missing_membership', 'wrong_job_config', 'boolean_threads'])
def test_restore_rejects_corrupt_experiment_links_even_with_valid_archive_checksum(tmp_path, mutation):
    service = Service(tmp_path / 'source', start_runner=False)
    project, matrix, config = prepare(service)
    plan = service.create_experiment_plan(spec(project, matrix, config))
    batch = service.submit_experiment(plan['id'])
    original = service.backup()
    database = tmp_path / 'changed.sqlite3'
    with zipfile.ZipFile(original) as archive:
        database.write_bytes(archive.read('metadata.sqlite3'))
    with sqlite3.connect(database) as connection:
        kind, identity = ('experiment_plans', plan['id']) if mutation in {'plan_hash','cell_reference'} else ('jobs', batch['jobs'][0])
        item = json.loads(connection.execute('SELECT body FROM entities WHERE kind=? AND id=?', (kind, identity)).fetchone()[0])
        if mutation == 'plan_hash':
            item['hash'] = '0' * 64
        elif mutation == 'cell_reference':
            item['cells'][0]['plan_id'] = 'plan_missing'
            from emsolver.contracts import content_hash
            item['hash'] = content_hash({key: value for key, value in item.items() if key != 'hash'})
        elif mutation == 'job_membership':
            item['experiment']['cell_id'] = 'missing'
        elif mutation == 'crossed_cell':
            item['experiment']['cell_id'] = 'c002'
        elif mutation == 'duplicate_repeat':
            item['experiment']['repeat'] = 2
        elif mutation == 'missing_membership':
            item.pop('experiment')
        elif mutation == 'wrong_job_config':
            item['config']['target']['k'] = 3
        else:
            item['config']['resources']['threads'] = True
        connection.execute('UPDATE entities SET body=? WHERE kind=? AND id=?', (json.dumps(item), kind, identity))
    # The source snapshot retains WAL mode; close its last connection so the
    # committed mutation is checkpointed into the standalone archive member.
    connection.close()
    corrupt = tmp_path / 'corrupt.zip'
    with zipfile.ZipFile(original) as source, zipfile.ZipFile(corrupt, 'w') as target:
        manifest = json.loads(source.read('backup-manifest.json'))
        raw = database.read_bytes()
        manifest['files']['metadata.sqlite3'] = hashlib.sha256(raw).hexdigest()
        for member in source.namelist():
            target.writestr(member, json.dumps(manifest) if member == 'backup-manifest.json' else raw if member == 'metadata.sqlite3' else source.read(member))
    with pytest.raises(ServiceError):
        service.restore_backup(corrupt, tmp_path / 'rejected')
    assert not (tmp_path / 'rejected').exists()
    assert not list(tmp_path.glob('.*.tmp'))
    service.close()


def test_report_escapes_untrusted_experiment_parameters(tmp_path):
    from emsolver.reporting import build_benchmark_report
    text = build_benchmark_report({'id': 'batch', 'runs': [{'id': 'job', 'status': 'FAILED',
        'experiment': {'cell_id': '<script>cell</script>', 'repeat': 1},
        'config': {'target': {'sigma': '<img src=x onerror=alert(1)>'}}}],
        'groups': [{'configuration_diff': [{'path': '<script>path</script>'}]}]}, tmp_path / 'escaped.html').read_text()
    assert '<script>' not in text and '<img src=x' not in text
    assert '&lt;script&gt;' in text


def test_regular_job_cannot_capture_an_experiment_idempotency_key(tmp_path):
    service = Service(tmp_path, start_runner=False)
    project, matrix, config = prepare(service)
    specification = spec(project, matrix, config)
    specification.update(k_values=[2], repeats=1)
    plan = service.create_experiment_plan(specification)
    batch_id = 'b_' + plan['id'].removeprefix('ep_')
    service.submit_job(plan['cells'][0]['plan_id'], idempotency_key=f'{batch_id}:c001:1')
    before = {table: service.store.db.execute(f'SELECT * FROM {table}').fetchall()
              for table in ('entities', 'events', 'audit', 'idempotency')}
    with pytest.raises(ServiceError) as failure:
        service.submit_experiment(plan['id'])
    assert failure.value.code == 'IDEMPOTENCY_CONFLICT'
    after = {table: service.store.db.execute(f'SELECT * FROM {table}').fetchall()
             for table in before}
    assert before == after
    assert service.store.count('benchmarks') == 0
    service.close()
