"""Projection queries preserve totals, isolation, and transaction ownership."""
import json
import importlib.util
from pathlib import Path

import pytest

from emsolver.jobs import ACTIVE, JobRepository, JobStatus
from emsolver.store import Store
from emsolver.telemetry import render_metrics


def test_status_counts_include_unknown_and_null_without_decoding_documents(tmp_path, monkeypatch):
    store = Store(tmp_path / 'state.sqlite3')
    try:
        for index, status in enumerate(('RUNNING', 'RUNNING', 'FUTURE', None, 42)):
            store.put('jobs', str(index), {'status': status, 'project_id': 'p'})
        store.put('jobs', 'other', {'status': 'FAILED', 'project_id': 'q'})
        store.put('imports', 'unrelated', {'status': 'RUNNING', 'project_id': 'p'})
        monkeypatch.setattr(json, 'loads', lambda *_args, **_kwargs: pytest.fail('decoded a document'))
        assert store.status_counts('jobs', project_id='p') == {'RUNNING': 2, 'FUTURE': 1, None: 2}
        assert store.status_counts('jobs') == {'RUNNING': 2, 'FUTURE': 1, None: 2, 'FAILED': 1}
        assert store.status_counts('jobs', project_id='missing') == {}
    finally:
        store.close()


@pytest.mark.parametrize('project_id', [None, 'p', 'missing'])
def test_summary_preserves_known_states_and_total_for_unknown_states(tmp_path, project_id):
    store = Store(tmp_path / 'state.sqlite3')
    try:
        for state in JobStatus:
            store.put('jobs', state.value, {'status': state.value, 'project_id': 'p'})
        for index, status in enumerate(('FUTURE', None, 17)):
            store.put('jobs', f'unknown-{index}', {'status': status, 'project_id': 'p'})
        store.put('jobs', 'other', {'status': 'SUCCEEDED', 'project_id': 'q'})
        store.put('projects', 'p', {'id': 'p'})
        store.put('projects', 'q', {'id': 'q'})
        store.put('matrices', 'p', {'project_id': 'p'})
        store.put('matrices', 'q', {'project_id': 'q'})
        summary = JobRepository(store).summary(project_id)
        expected = {state.value: 0 if project_id == 'missing' else 1 for state in JobStatus}
        if project_id is None:
            expected['SUCCEEDED'] += 1
        assert summary == {
            'projects': 2,
            'matrices': 2 if project_id is None else int(project_id == 'p'),
            'jobs': 13 if project_id is None else 12 if project_id == 'p' else 0,
            'active_jobs': 0 if project_id == 'missing' else len(ACTIVE),
            'certified_jobs': expected['SUCCEEDED'],
            'job_status_counts': expected,
        }
    finally:
        store.close()


def test_summary_uses_bounded_projection_queries_and_no_json_decode(tmp_path, monkeypatch):
    store = Store(tmp_path / 'state.sqlite3')
    try:
        for index in range(200):
            store.put('jobs', str(index), {'status': 'SUCCEEDED', 'project_id': 'p'})
        statements = []
        store.db.set_trace_callback(statements.append)
        monkeypatch.setattr(json, 'loads', lambda *_args, **_kwargs: pytest.fail('decoded a document'))
        assert JobRepository(store).summary('p')['jobs'] == 200
        selects = [query for query in statements if query.lstrip().upper().startswith('SELECT')]
        assert len(selects) <= 3
        assert all('body' not in query for query in selects)
    finally:
        store.db.set_trace_callback(None)
        store.close()


def test_summary_counters_share_a_snapshot_across_independent_writer(tmp_path):
    path = tmp_path / 'state.sqlite3'
    reader, writer = Store(path), Store(path)
    try:
        reader.put('jobs', 'first', {'status': 'QUEUED', 'project_id': 'p'})
        reader.put('projects', 'p', {'id': 'p'})
        reader.put('matrices', 'first', {'project_id': 'p'})
        changed = False

        def mutate_after_status_query(statement):
            nonlocal changed
            if not changed and statement.startswith('SELECT COUNT(*)'):
                changed = True
                with writer.transaction():
                    writer.put('jobs', 'first', {'status': 'SUCCEEDED', 'project_id': 'p'})
                    writer.put('projects', 'q', {'id': 'q'})
                    writer.put('matrices', 'second', {'project_id': 'p'})

        reader.db.set_trace_callback(mutate_after_status_query)
        summary = JobRepository(reader).summary('p')
        reader.db.set_trace_callback(None)
        assert changed
        assert summary['active_jobs'] == summary['jobs'] == summary['projects'] == summary['matrices'] == 1
        assert summary['certified_jobs'] == 0
        assert JobRepository(reader).summary('p')['matrices'] == 2
        assert JobRepository(reader).summary('p')['certified_jobs'] == 1
    finally:
        reader.db.set_trace_callback(None)
        writer.close()
        reader.close()


def test_summary_reuses_outer_write_and_leaves_rollback_with_caller(tmp_path):
    store = Store(tmp_path / 'state.sqlite3')
    try:
        with pytest.raises(RuntimeError, match='rollback'):
            with store.transaction():
                store.put('jobs', 'first', {'status': 'RUNNING'})
                assert JobRepository(store).summary()['active_jobs'] == 1
                assert store.db.in_transaction
                raise RuntimeError('rollback')
        assert not store.db.in_transaction
        assert JobRepository(store).summary()['jobs'] == 0
    finally:
        store.close()


@pytest.mark.parametrize('project_id', [None, 'p'])
def test_status_aggregation_uses_covering_indexes_without_sorting(tmp_path, project_id):
    store = Store(tmp_path / 'state.sqlite3')
    try:
        statements = []
        store.db.set_trace_callback(statements.append)
        store.status_counts('jobs', project_id=project_id)
        store.db.set_trace_callback(None)
        query = next(value for value in statements if value.startswith('SELECT'))
        plan = ' '.join(row[3] for row in store.db.execute('EXPLAIN QUERY PLAN ' + query))
        assert 'COVERING INDEX' in plan
        assert 'TEMP B-TREE' not in plan
    finally:
        store.db.set_trace_callback(None)
        store.close()


def test_scoped_list_only_decodes_requested_project_in_sequence_order(tmp_path, monkeypatch):
    store = Store(tmp_path / 'state.sqlite3')
    try:
        for identity, project_id in (('a', 'p'), ('b', 'q'), ('c', 'p')):
            store.put('imports', identity, {'id': identity, 'project_id': project_id})
        original_loads = json.loads
        decoded = []

        def loads(raw, *args, **kwargs):
            value = original_loads(raw, *args, **kwargs)
            decoded.append(value['id'])
            return value

        monkeypatch.setattr(json, 'loads', loads)
        assert [row['id'] for row in store.list('imports', project_id='p')] == ['a', 'c']
        assert decoded == ['a', 'c']
        assert store.list('imports', project_id="p' OR 1=1 --") == []
        assert store.list('imports', project_id='missing') == []
        assert [row['id'] for row in store.list('imports')] == ['a', 'b', 'c']
    finally:
        store.close()


@pytest.mark.parametrize('method', ['status_counts', 'list'])
@pytest.mark.parametrize('project_id', ['', True, 2, []])
def test_projection_queries_reject_invalid_project_scopes(tmp_path, method, project_id):
    store = Store(tmp_path / 'state.sqlite3')
    try:
        with pytest.raises(ValueError, match='project_id'):
            getattr(store, method)('jobs', project_id=project_id)
    finally:
        store.close()


def test_metrics_fold_all_noncanonical_states_into_unknown(tmp_path, monkeypatch):
    store = Store(tmp_path / 'state.sqlite3')
    try:
        for index, status in enumerate(('RUNNING', 'FUTURE', None, 42, 'UNKNOWN')):
            store.put('jobs', str(index), {'status': status})
        monkeypatch.setattr(json, 'loads', lambda *_args, **_kwargs: pytest.fail('decoded a document'))
        rendered = render_metrics(store, tmp_path)
        assert 'emsolver_jobs{status="RUNNING"} 1' in rendered
        assert 'emsolver_jobs{status="UNKNOWN"} 4' in rendered
        assert 'FUTURE' not in rendered
    finally:
        store.close()


def test_query_diagnostic_checks_equivalent_summaries_without_loading_job_bodies():
    path = Path(__file__).resolve().parents[1] / 'scripts' / 'benchmark_store.py'
    spec = importlib.util.spec_from_file_location('store_benchmark', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    report = module.run(rows=40, repeats=2)
    assert report['summary_equal'] == {'global': True, 'project': True}
    assert report['scoped_project_jobs'] == 1
    for scope in ('global', 'project'):
        legacy, current = f'legacy_summary_{scope}', f'aggregated_summary_{scope}'
        assert report['select_statements'][legacy] == 12
        assert report['select_statements'][current] <= 3
        assert report['decoded_json_records'][legacy] == report['decoded_json_records'][current] == 0
        assert len(report['samples_ms'][current]) == report['repeats']
