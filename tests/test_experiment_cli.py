import json
from pathlib import Path

import pytest
import yaml

from emsolver.cli import main
from emsolver.service import Service


@pytest.fixture
def experiment(tmp_path):
    root = tmp_path / 'state'
    service = Service(root, start_runner=False)
    project = service.create_project('CLI experiments')
    matrix = service.generate_sample(project['id'], n=32)
    # Starting a runner would recover this row before any CLI action executes.
    service.store.put('jobs', 'observed', {
        'id': 'observed', 'status': 'RUNNING', 'stage': 'solving', 'metrics': {}})
    service.close()
    spec = {
        'project_id': project['id'], 'matrix_ids': [matrix['id']],
        'base_config': {'resources': {'threads': 1, 'walltime_seconds': 60,
                                      'disk_limit_gib': 1}},
        'k_values': [2, 3], 'sigma_values': [0], 'threads_values': [1],
        'memory_limit_gib_values': [16], 'adaptive_shift_values': [True],
        'repeats': 2,
        'budget': {'max_runs': 4, 'max_total_walltime_seconds': 240,
                   'max_total_disk_gib': 4},
    }
    path = tmp_path / 'experiment.yaml'
    path.write_text(yaml.safe_dump(spec), encoding='utf-8')
    return root, path, spec


def invoke(root, capsys, *args):
    assert main(['--data-dir', str(root), 'experiments', *args, '--json']) == 0
    captured = capsys.readouterr()
    assert captured.err == ''
    return json.loads(captured.out)


def test_cli_experiment_plan_freezes_without_starting_or_recovering_workers(experiment, capsys):
    root, path, _ = experiment
    plan = invoke(root, capsys, 'plan', '--spec', str(path))
    assert plan['id'].startswith('ep_') and plan['hash']
    assert [cell['parameters']['k'] for cell in plan['cells']] == [2, 3]
    assert plan['budget']['total_runs'] == 4
    assert plan['budget']['total_walltime_seconds'] == 240
    assert plan['budget']['total_disk_gib'] == 4
    service = Service(root, start_runner=False)
    try:
        assert service.store.count('experiment_plans') == 1
        assert service.store.count('jobs') == 1
        assert service.get_job('observed')['status'] == 'RUNNING'
        assert list((root / 'runs').iterdir()) == []
    finally:
        service.close()


def test_cli_experiment_submit_status_and_cancel_are_offline_and_idempotent(experiment, capsys):
    root, path, _ = experiment
    plan = invoke(root, capsys, 'plan', '--spec', str(path))
    batch = invoke(root, capsys, 'submit', '--plan', plan['id'])
    again = invoke(root, capsys, 'submit', '--plan', plan['id'])
    assert again['id'] == batch['id'] and again['jobs'] == batch['jobs']
    assert len(batch['jobs']) == 4
    assert all(run['status'] == 'QUEUED' for run in batch['runs'])
    status = invoke(root, capsys, 'status', '--batch', batch['id'])
    assert status['summary']['total'] == 4 and len(status['groups']) == 2
    assert all(run['status'] == 'QUEUED' for run in status['runs'])
    cancelled = invoke(root, capsys, 'cancel', '--batch', batch['id'])
    repeated = invoke(root, capsys, 'cancel', '--batch', batch['id'])
    assert cancelled['jobs'] == repeated['jobs'] == batch['jobs']
    assert cancelled['summary']['failed'] == 4
    assert all(run['status'] == 'CANCELLED' for run in repeated['runs'])
    service = Service(root, start_runner=False)
    try:
        assert service.store.count('jobs') == 5
        assert service.get_job('observed')['status'] == 'RUNNING'
        for job_id in batch['jobs']:
            assert [event['event'] for event in service.events(job_id)] == ['QUEUED', 'CANCELLED']
        assert not list((root / 'runs').glob('*/worker.spec.json'))
    finally:
        service.close()


def test_cli_experiment_list_pages_summaries_and_filters_project(experiment, capsys):
    root, path, spec = experiment
    first = invoke(root, capsys, 'plan', '--spec', str(path))
    first_batch = invoke(root, capsys, 'submit', '--plan', first['id'])
    second = invoke(root, capsys, 'plan', '--spec', str(path))
    second_batch = invoke(root, capsys, 'submit', '--plan', second['id'])
    service = Service(root, start_runner=False)
    foreign = service.create_project('Other project')
    service.close()
    page = invoke(root, capsys, 'list', '--project', spec['project_id'], '--limit', '1')
    assert page['total'] == 2 and page['next_cursor']
    assert page['items'][0]['id'] == first_batch['id']
    assert 'runs' not in page['items'][0] and 'cells' not in page['items'][0]
    next_page = invoke(root, capsys, 'list', '--project', spec['project_id'],
                       '--limit', '1', '--cursor', page['next_cursor'])
    assert next_page['items'][0]['id'] == second_batch['id']
    assert next_page['next_cursor'] is None
    assert invoke(root, capsys, 'list', '--project', foreign['id'])['items'] == []
    assert invoke(root, capsys, 'list')['total'] == 2
    service = Service(root, start_runner=False)
    try:
        assert service.get_job('observed')['status'] == 'RUNNING'
        assert all(service.get_job(job_id)['status'] == 'QUEUED'
                   for batch in (first_batch, second_batch) for job_id in batch['jobs'])
    finally:
        service.close()


@pytest.mark.parametrize(('args', 'missing'), [
    ([], 'action'), (['plan'], '--spec'), (['submit'], '--plan'),
    (['status'], '--batch'), (['cancel'], '--batch'),
])
def test_cli_experiment_missing_arguments_are_actionable_parser_errors(tmp_path, capsys, args, missing):
    with pytest.raises(SystemExit) as failure:
        main(['--data-dir', str(tmp_path / 'state'), 'experiments', *args])
    assert failure.value.code == 2
    error = capsys.readouterr().err
    assert missing in error and 'required' in error
    assert not (tmp_path / 'state').exists()


@pytest.mark.parametrize('content', [
    '', '[]', 'just a scalar', 'matrix_ids: [',
    '!!python/object/apply:os.system ["touch unsafe"]',
])
def test_cli_experiment_rejects_non_object_or_unsafe_yaml_before_creating_state(tmp_path, capsys, content):
    path = tmp_path / 'invalid.yaml'
    path.write_text(content, encoding='utf-8')
    root = tmp_path / 'state'
    assert main(['--data-dir', str(root), 'experiments', 'plan', '--spec', str(path), '--json']) == 2
    error = json.loads(capsys.readouterr().err)
    assert error['code'] == 'INVALID_CONFIG' and error['message']
    assert not root.exists()


def test_cli_experiment_rejects_large_spec_with_bounded_read(tmp_path, capsys):
    path = tmp_path / 'large.yaml'
    path.write_bytes(b'#' * (1024 * 1024 + 1))
    root = tmp_path / 'state'
    assert main(['--data-dir', str(root), 'experiments', 'plan', '--spec', str(path), '--json']) == 2
    error = json.loads(capsys.readouterr().err)
    assert '1048576' in error['message'] and 'spec' in error['message'].lower()
    assert not root.exists()


def test_example_experiment_spec_has_valid_bounded_resources(experiment, capsys, tmp_path):
    root, _, prepared = experiment
    example = Path(__file__).resolve().parents[1] / 'examples' / 'experiment.yaml'
    spec = yaml.safe_load(example.read_text(encoding='utf-8'))
    spec.update(project_id=prepared['project_id'], matrix_ids=prepared['matrix_ids'])
    assert spec['base_config']['resources'] == {
        'threads': 1, 'walltime_seconds': 60, 'disk_limit_gib': 1}
    assert spec['threads_values'] == [1]
    path = tmp_path / 'example.yaml'
    path.write_text(yaml.safe_dump(spec), encoding='utf-8')
    assert invoke(root, capsys, 'plan', '--spec', str(path))['budget']['total_runs'] <= 100
