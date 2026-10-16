import threading
import time

from emsolver.store import Store
from emsolver.telemetry import SecondaryTelemetry, render_metrics


def test_buffer_is_bounded_drops_are_explicit_and_samples_are_detached():
    telemetry = SecondaryTelemetry(capacity=2, start_worker=False)
    sample = {'event': 'linear_progress', 'iteration': 1, 'input_path': '/Users/private/input.npz'}
    assert telemetry.emit(sample)
    sample['iteration'] = 999
    assert telemetry.emit({'iteration': 2})
    assert telemetry.emit({'iteration': 3}) is False
    assert telemetry.counters()['dropped'] == 1
    rows = telemetry.drain()
    assert rows[0]['iteration'] == 1 and 'input_path' not in rows[0]
    assert telemetry.counters()['queued'] == 0
    telemetry.close()


def test_stuck_exporter_never_blocks_computation_or_close():
    entered, release = threading.Event(), threading.Event()
    def exporter(_):
        entered.set()
        release.wait(5)
    telemetry = SecondaryTelemetry(exporter=exporter, capacity=2)
    try:
        telemetry.emit({'iteration': 1})
        assert entered.wait(1)
        started = time.monotonic()
        for index in range(100): telemetry.emit({'iteration': index})
        telemetry.close(timeout=.01)
        assert time.monotonic() - started < .3
        assert telemetry.counters()['dropped'] >= 98
    finally:
        release.set()


def test_exporter_failures_do_not_escape_and_critical_events_remain_canonical(tmp_path):
    entered = threading.Event()
    def exporter(_):
        entered.set()
        raise RuntimeError('private exporter failure')
    telemetry = SecondaryTelemetry(exporter=exporter)
    store = Store(tmp_path / 'data.sqlite3')
    try:
        event = store.append_event('private-job', {'event': 'FAILED', 'error_code': 'NOT_CONVERGED'})
        telemetry.emit(event)
        telemetry.emit({'event': 'linear_progress', 'iteration': 1})
        assert entered.wait(1)
        deadline = time.monotonic() + 1
        while telemetry.counters()['exporter_failures'] == 0 and time.monotonic() < deadline: time.sleep(.005)
        assert telemetry.counters()['exporter_failures'] == 1
        assert store.events('private-job')[0]['event'] == 'FAILED'
    finally:
        telemetry.close()
        store.close()


def test_openmetrics_uses_sql_counts_and_contains_no_private_labels(tmp_path, monkeypatch):
    store = Store(tmp_path / 'data.sqlite3')
    try:
        for index, status in enumerate(('FAILED', 'SUCCEEDED', 'RUNNING', 'private-status')):
            store.put('jobs', str(index), {'id': str(index), 'project_id': 'private-project', 'status': status,
                                         'name': '/Users/private/matrix'})
        monkeypatch.setattr(store, 'list', lambda *_: (_ for _ in ()).throw(AssertionError('must not load job documents')))
        telemetry = SecondaryTelemetry(capacity=1, start_worker=False)
        telemetry.emit({'iteration': 1})
        telemetry.emit({'iteration': 2})
        rendered = render_metrics(store, tmp_path, telemetry)
        assert 'emsolver_jobs{status="FAILED"} 1' in rendered
        assert 'emsolver_jobs{status="UNKNOWN"} 1' in rendered
        assert 'emsolver_free_disk_bytes ' in rendered
        assert 'emsolver_telemetry_dropped_total 1' in rendered
        assert 'private' not in rendered and rendered.endswith('# EOF\n')
        telemetry.close()
    finally:
        store.close()


def test_actual_paired_observation_benchmark_records_both_switches_and_correctness():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / 'scripts' / 'benchmark_observability.py'
    spec = importlib.util.spec_from_file_location('observability_benchmark', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.run(repeats=2, n=40)
    assert len(result['runs']) == 4
    assert [row['enabled'] for row in result['runs']] == [False, True, True, False]
    assert all(row['seconds'] > 0 and row['status'] in {'SUCCEEDED', 'PARTIAL'} for row in result['runs'])
    assert all(row['correctness']['independent_numerical_status'] == 'PASSED' for row in result['runs'])
    assert all(pair['numerically_equivalent'] for pair in result['pairs'])
    assert result['summary']['enabled_to_disabled_ratio'] > 0
    assert result['switch'] == 'observability.enabled'
