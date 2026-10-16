import json
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from emsolver.events import EventJournal, WorkerEventReader
from emsolver.store import Store


@pytest.fixture
def journal(tmp_path):
    store = Store(tmp_path / 'metadata.sqlite3')
    try:
        yield EventJournal(tmp_path, store)
    finally:
        store.close()


def append_events(journal, count, job_id='j_events'):
    with journal.store.transaction():
        for index in range(count):
            journal.append(job_id, {'stage': 'solving', 'event': 'ITERATION', 'iteration': index})


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]


def test_worker_reader_waits_for_complete_utf8_lines_and_never_replays(tmp_path):
    path = tmp_path / 'worker.events.jsonl'
    reader = WorkerEventReader(path)
    assert reader.drain() == []
    first = json.dumps({'stage': 'solving', 'event': '进展'}, ensure_ascii=False).encode()
    cut = first.index('进'.encode()) + 1
    path.write_bytes(first[:cut])
    assert reader.drain() == []
    with path.open('ab') as handle:
        handle.write(first[cut:] + b'\n' + b'{"event":"SECOND"}')
    assert reader.drain() == [{'stage': 'solving', 'event': '进展'}]
    assert reader.drain() == []
    with path.open('ab') as handle:
        handle.write(b'\n')
    assert reader.drain() == [{'event': 'SECOND'}]
    assert reader.drain() == []


def test_worker_reader_skips_invalid_complete_lines_and_filters_worker_fields(tmp_path):
    path = tmp_path / 'worker.events.jsonl'
    safe = {
        'stage': 'solving', 'event': 'ITERATION', 'elapsed_seconds': 1.5,
        'iteration': 3, 'residual': 0.1, 'residuals': [0.1], 'sigma_work': 0,
        'n_accepted': 1, 'error_code': None, 'attempt': 2, 'metrics': {'rss': 5},
    }
    incoming = {**safe, 'message': '/private/input.npy', 'input_path': '/private/input.npy',
                'seq': 100, 'time': 'untrusted', 'eigenvalues': [42]}
    path.write_bytes(b'not-json\n[]\nnull\n{"residual":NaN}\n\xff\n' +
                     json.dumps(incoming).encode() + b'\n')
    reader = WorkerEventReader(path)
    assert reader.drain() == [safe]
    assert reader.drain() == []


def test_final_drain_imports_events_written_as_worker_exits(tmp_path, journal):
    path = tmp_path / 'worker.events.jsonl'
    path.write_text('{"event":"STARTED"}\n', encoding='utf-8')
    reader = WorkerEventReader(path)
    for event in reader.drain():
        journal.append('j_events', event)
    with path.open('a', encoding='utf-8') as handle:
        handle.write('{"stage":"validated","event":"FINAL_WORKER_EVENT"}\n')
    for event in reader.drain():
        journal.append('j_events', event)
    assert [item['event'] for item in journal.tail('j_events')] == ['STARTED', 'FINAL_WORKER_EVENT']
    assert reader.drain() == []


def test_append_is_canonical_database_only_and_follows_outer_rollback(journal):
    with pytest.raises(RuntimeError, match='rollback'):
        with journal.store.transaction():
            journal.append('j_events', {'event': 'QUEUED'})
            assert len(journal.store.events('j_events')) == 1
            raise RuntimeError('rollback')
    assert journal.store.events('j_events') == []
    assert not (journal.root / 'runs').exists()
    persisted = journal.append('j_events', {'event': 'QUEUED'})
    assert persisted['seq'] == 1
    assert persisted['time']
    assert journal.store.events('j_events') == [persisted]
    assert not (journal.root / 'runs').exists()


def test_append_rejects_nonfinite_json_without_creating_events_or_files(journal):
    with pytest.raises(ValueError):
        journal.append('j_events', {'event': 'INVALID', 'residual': float('nan')})
    assert journal.store.events('j_events') == []
    assert not (journal.root / 'runs').exists()


def test_pages_have_bounded_replay_continuation_and_tail_ascending(journal):
    append_events(journal, 5)
    first = journal.page('j_events', limit=2)
    assert [item['seq'] for item in first['items']] == [1, 2]
    assert first['next_after'] == 2 and first['has_more'] is True
    second = journal.page('j_events', after=first['next_after'], limit=2)
    assert [item['seq'] for item in second['items']] == [3, 4]
    assert second['next_after'] == 4 and second['has_more'] is True
    final = journal.page('j_events', after=second['next_after'], limit=2)
    assert [item['seq'] for item in final['items']] == [5]
    assert final['next_after'] == 5 and final['has_more'] is False
    assert journal.page('j_events', after=10, limit=2) == {
        'items': [], 'next_after': 10, 'has_more': False,
    }
    assert [item['seq'] for item in journal.tail('j_events', limit=2)] == [4, 5]
    assert journal.tail('j_empty') == []


def test_maximum_page_uses_one_lookahead_row_without_exceeding_window(journal, monkeypatch):
    append_events(journal, 1001)
    real_events = journal.store.events
    calls = []
    def observed_read(job_id, after=0, **kwargs):
        calls.append(kwargs)
        return real_events(job_id, after, **kwargs)
    monkeypatch.setattr(journal.store, 'events', observed_read)
    page = journal.page('j_events', limit=1000)
    assert [item['seq'] for item in page['items']] == list(range(1, 1001))
    assert page['next_after'] == 1000 and page['has_more'] is True
    assert [item['seq'] for item in journal.tail('j_events', limit=1000)] == list(range(2, 1002))
    assert calls == [{'limit': 1001}, {'limit': 1000, 'descending': True}]


@pytest.mark.parametrize('after', [-1, True, 1.5, '0', None])
def test_page_rejects_invalid_after(journal, after):
    with pytest.raises(ValueError):
        journal.page('j_events', after=after)


@pytest.mark.parametrize('limit', [0, -1, 1001, True, 1.5, '2', None])
def test_replay_rejects_invalid_limits(journal, limit):
    with pytest.raises(ValueError):
        journal.page('j_events', limit=limit)
    with pytest.raises(ValueError):
        journal.tail('j_events', limit=limit)


def test_export_uses_500_row_batches_and_freezes_upper_sequence(journal, monkeypatch):
    append_events(journal, 1203)
    real_events = journal.store.events
    calls = []
    def interleaved_events(job_id, after=0, **kwargs):
        calls.append((after, kwargs))
        if len(calls) == 1:
            journal.append(job_id, {'event': 'ARRIVED_AFTER_EXPORT_STARTED'})
        return real_events(job_id, after, **kwargs)
    monkeypatch.setattr(journal.store, 'events', interleaved_events)
    exported = journal.export('j_events')
    assert exported == journal.root / 'runs' / 'j_events' / 'events.jsonl'
    rows = read_jsonl(exported)
    assert [item['seq'] for item in rows] == list(range(1, 1204))
    assert journal.store.event_max_seq('j_events') == 1204
    assert len(calls) == 3
    assert all(options['limit'] == 500 and options['until'] == 1203 for _, options in calls)
    assert [after for after, _ in calls] == [0, 500, 1000]
    assert not list(exported.parent.glob('*.tmp'))


def test_export_failure_preserves_previous_file_and_cleans_unique_temporary(journal, monkeypatch):
    append_events(journal, 501)
    exported = journal.root / 'runs' / 'j_events' / 'events.jsonl'
    exported.parent.mkdir(parents=True)
    exported.write_text('previous completed export\n', encoding='utf-8')
    real_events = journal.store.events
    def failed_read(job_id, after=0, **kwargs):
        if after:
            raise OSError('injected export failure')
        return real_events(job_id, after, **kwargs)
    monkeypatch.setattr(journal.store, 'events', failed_read)
    with pytest.raises(OSError, match='injected export failure'):
        journal.export('j_events')
    assert exported.read_text(encoding='utf-8') == 'previous completed export\n'
    assert list(exported.parent.iterdir()) == [exported]
    assert journal.store.event_max_seq('j_events') == 501


def test_concurrent_exports_use_unique_temporaries_and_publish_complete_file(journal, monkeypatch):
    append_events(journal, 40)
    barrier = threading.Barrier(2)
    real_events = journal.store.events
    def concurrent_read(job_id, after=0, **kwargs):
        if after == 0:
            barrier.wait(timeout=5)
        return real_events(job_id, after, **kwargs)
    monkeypatch.setattr(journal.store, 'events', concurrent_read)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(journal.export, ['j_events', 'j_events']))
    assert first == second
    assert [item['seq'] for item in read_jsonl(first)] == list(range(1, 41))
    assert list(first.parent.iterdir()) == [first]


def test_empty_export_publishes_an_empty_jsonl_file(journal):
    path = journal.export('j_empty')
    assert path.read_bytes() == b''


@pytest.mark.parametrize('job_id', ['../escape', 'a/b', '/absolute', '..', 'a\\b', '', None])
def test_export_rejects_path_traversal_before_creating_any_files(journal, job_id):
    with pytest.raises(ValueError):
        journal.export(job_id)
    assert not (journal.root / 'runs').exists()
