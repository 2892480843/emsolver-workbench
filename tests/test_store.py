import base64
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import threading

import pytest

from emsolver.store import Store


def legacy_database(path, rows):
    with sqlite3.connect(path) as database:
        database.execute('CREATE TABLE entities(kind TEXT,id TEXT,body TEXT NOT NULL,PRIMARY KEY(kind,id))')
        database.executemany('INSERT INTO entities(rowid,kind,id,body) VALUES(?,?,?,?)', rows)


def unpack_cursor(cursor):
    return json.loads(base64.urlsafe_b64decode(cursor + '=' * (-len(cursor) % 4)))


def pack_cursor(value):
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip('=')


def test_legacy_migration_preserves_raw_json_rowid_order_and_other_tables(tmp_path):
    path = tmp_path / 'old.sqlite3'
    original = [
        (4, 'jobs', 'later', '{ "id": "later", "status": "QUEUED", "project_id": "p", "label": "中文" }'),
        (2, 'jobs', 'earlier', '{"status":"SUCCEEDED", "id":"earlier"}'),
        (8, 'projects', 'p', '{"id":"p"}'),
    ]
    legacy_database(path, original)
    with sqlite3.connect(path) as database:
        database.execute('CREATE TABLE events(job_id TEXT,seq INTEGER,body TEXT NOT NULL,PRIMARY KEY(job_id,seq))')
        database.execute('INSERT INTO events VALUES(?,?,?)', ('later', 7, '{"seq":7,"event":"kept"}'))
    store = Store(path)
    assert store.db.execute('PRAGMA user_version').fetchone()[0] == 2
    assert store.db.execute('SELECT seq,kind,id,body FROM entities ORDER BY seq').fetchall() == sorted(original)
    assert [item['id'] for item in store.list('jobs')] == ['earlier', 'later']
    assert store.count('jobs', status='QUEUED', project_id='p') == 1
    assert store.events('later') == [{'seq': 7, 'event': 'kept'}]
    instance = store.instance_id
    store.close()
    reopened = Store(path)
    assert reopened.instance_id == instance
    assert reopened.count('jobs') == 2
    reopened.close()


def test_legacy_migration_failure_rolls_back_schema_and_raw_rows(tmp_path):
    path = tmp_path / 'invalid.sqlite3'
    rows = [(1, 'jobs', 'valid', '{"id":"valid"}'), (2, 'jobs', 'broken', '{invalid')]
    legacy_database(path, rows)
    with pytest.raises(ValueError):
        Store(path)
    with sqlite3.connect(path) as database:
        assert database.execute('PRAGMA user_version').fetchone()[0] == 0
        assert [row[1] for row in database.execute('PRAGMA table_info(entities)')] == ['kind', 'id', 'body']
        assert database.execute('SELECT rowid,kind,id,body FROM entities ORDER BY rowid').fetchall() == rows
        assert database.execute("SELECT name FROM sqlite_master WHERE name='entities_schema2'").fetchall() == []


def test_migration_rolls_back_already_copied_batches_and_table_swap(tmp_path, monkeypatch):
    path = tmp_path / 'multiple-batches.sqlite3'
    rows = [(number, 'jobs', str(number), json.dumps({'id': str(number)})) for number in range(1, 503)]
    rows[-1] = (502, 'jobs', '502', '{invalid')
    legacy_database(path, rows)
    with pytest.raises(ValueError):
        Store(path)
    with sqlite3.connect(path) as database:
        assert database.execute('SELECT rowid,kind,id,body FROM entities ORDER BY rowid').fetchall() == rows
        database.execute('UPDATE entities SET body=? WHERE id=?', ('{"id":"502"}', '502'))
    migrate = Store._migrate_entities

    def fail_after_swap(self):
        migrate(self)
        raise RuntimeError('Simulated failure after table replacement')

    monkeypatch.setattr(Store, '_migrate_entities', fail_after_swap)
    with pytest.raises(RuntimeError, match='after table replacement'):
        Store(path)
    with sqlite3.connect(path) as database:
        assert database.execute('PRAGMA user_version').fetchone()[0] == 0
        assert [row[1] for row in database.execute('PRAGMA table_info(entities)')] == ['kind', 'id', 'body']
        assert database.execute('SELECT COUNT(*) FROM entities').fetchone()[0] == 502
        assert database.execute("SELECT name FROM sqlite_master WHERE name='entities_schema2'").fetchall() == []


@pytest.mark.parametrize('declaration', [
    '(kind TEXT,id TEXT,body TEXT NOT NULL,extra TEXT)',
    '(kind TEXT,id TEXT,body TEXT NOT NULL)',
])
def test_unrecognized_unversioned_schema_is_not_rebuilt(tmp_path, declaration):
    path = tmp_path / 'unrecognized.sqlite3'
    with sqlite3.connect(path) as database:
        database.execute('CREATE TABLE entities' + declaration)
        original = database.execute("SELECT sql FROM sqlite_master WHERE name='entities'").fetchone()[0]
    with pytest.raises(ValueError, match='schema'):
        Store(path)
    with sqlite3.connect(path) as database:
        assert database.execute('PRAGMA user_version').fetchone()[0] == 0
        assert database.execute("SELECT sql FROM sqlite_master WHERE name='entities'").fetchone()[0] == original


def test_two_connections_migrate_once_and_share_instance_id(tmp_path):
    path = tmp_path / 'concurrent.sqlite3'
    legacy_database(path, [(number, 'jobs', str(number), json.dumps({'id': str(number), 'status': 'QUEUED'})) for number in range(1, 1001)])
    barrier = threading.Barrier(2)

    def open_store(_):
        barrier.wait()
        return Store(path)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = list(executor.map(open_store, range(2)))
    try:
        assert first.instance_id == second.instance_id
        assert first.count('jobs', status='QUEUED') == second.count('jobs') == 1000
        assert first.db.execute('SELECT MIN(seq),MAX(seq) FROM entities').fetchone() == (1, 1000)
    finally:
        first.close()
        second.close()


def test_future_schema_is_rejected_without_changes(tmp_path):
    path = tmp_path / 'future.sqlite3'
    legacy_database(path, [(3, 'jobs', 'a', '{"id":"a"}')])
    with sqlite3.connect(path) as database:
        database.execute('PRAGMA user_version=99')
    with pytest.raises(ValueError, match='version|schema'):
        Store(path)
    with sqlite3.connect(path) as database:
        assert database.execute('PRAGMA user_version').fetchone()[0] == 99
        assert database.execute('SELECT rowid,body FROM entities').fetchall() == [(3, '{"id":"a"}')]


def test_projection_updates_are_atomic_and_keep_sequence_and_detached_documents(tmp_path):
    store = Store(tmp_path / 'store.sqlite3')
    source = {'id': 'a', 'status': 'QUEUED', 'project_id': 'p', 'nested': {'value': 1}}
    store.put('jobs', 'a', source)
    sequence = store.db.execute("SELECT seq FROM entities WHERE id='a'").fetchone()[0]
    source['nested']['value'] = 9
    assert store.get('jobs', 'a')['nested']['value'] == 1
    store.put('jobs', 'a', {'id': 'a', 'status': 'RUNNING', 'project_id': 'q'})
    assert store.db.execute("SELECT seq,status,project_id FROM entities WHERE id='a'").fetchone() == (sequence, 'RUNNING', 'q')
    assert store.count('jobs', status='QUEUED') == 0
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.put('jobs', 'a', {'id': 'a', 'status': 'FAILED', 'project_id': 'z'})
            store.append_event('a', {'event': 'rolled-back'})
            raise RuntimeError('abort')
    assert store.first('jobs', status='RUNNING', project_id='q')['id'] == 'a'
    assert store.events('a') == []
    store.put('misc', 'legacy-list-body', ['valid', 'JSON'])
    assert store.get('misc', 'legacy-list-body') == ['valid', 'JSON']
    store.close()


def test_first_count_select_only_decode_requested_rows_and_use_indexes(tmp_path, monkeypatch):
    import emsolver.store as module
    store = Store(tmp_path / 'store.sqlite3')
    with store.transaction():
        for number in range(200):
            store.put('jobs', str(number), {'id': str(number), 'status': 'QUEUED' if number >= 190 else 'SUCCEEDED', 'project_id': 'p'})
    original_loads = module.json.loads
    decoded = []

    def loads(value, *args, **kwargs):
        decoded.append(value)
        return original_loads(value, *args, **kwargs)

    monkeypatch.setattr(module.json, 'loads', loads)
    assert store.count('jobs', status=('QUEUED', 'RUNNING'), project_id='p') == 10
    assert decoded == []
    assert store.first('jobs', status='QUEUED')['id'] == '190'
    assert len(decoded) == 1
    assert [item['id'] for item in store.select('jobs', status='QUEUED', limit=3)] == ['190', '191', '192']
    assert len(decoded) == 4
    plans = [
        ('SELECT body FROM entities WHERE kind=? ORDER BY seq LIMIT 1', ('jobs',)),
        ('SELECT body FROM entities WHERE kind=? AND status=? ORDER BY seq LIMIT 1', ('jobs', 'QUEUED')),
        ('SELECT body FROM entities WHERE kind=? AND project_id=? ORDER BY seq LIMIT 1', ('jobs', 'p')),
        ('SELECT body FROM entities WHERE kind=? AND project_id=? AND status=? ORDER BY seq LIMIT 1', ('jobs', 'p', 'QUEUED')),
    ]
    for query, parameters in plans:
        plan = ' '.join(str(row) for row in store.db.execute('EXPLAIN QUERY PLAN ' + query, parameters))
        assert 'SEARCH' in plan and 'INDEX' in plan
        assert 'TEMP B-TREE' not in plan
    store.close()


def test_page_freezes_insert_upper_bound_and_remains_stable_after_update_and_vacuum(tmp_path):
    store = Store(tmp_path / 'store.sqlite3')
    for number in range(6):
        store.put('jobs', str(number), {'id': str(number), 'status': 'QUEUED', 'project_id': 'p'})
    first = store.page('jobs', status='QUEUED', project_id='p', limit=2)
    assert first['total'] == 6
    assert [item['id'] for item in first['items']] == ['0', '1']
    assert 'seq' not in first['items'][0]
    store.put('jobs', '6', {'id': '6', 'status': 'QUEUED', 'project_id': 'p'})
    store.put('jobs', '2', {'id': '2', 'status': 'QUEUED', 'project_id': 'p', 'updated': True})
    store.db.execute('VACUUM')
    second = store.page('jobs', status='QUEUED', project_id='p', limit=2, cursor=first['next_cursor'])
    third = store.page('jobs', status='QUEUED', project_id='p', limit=2, cursor=second['next_cursor'])
    assert [item['id'] for page in (first, second, third) for item in page['items']] == list('012345')
    assert second['total'] == third['total'] == 6
    assert second['items'][0]['updated'] is True
    assert third['next_cursor'] is None
    assert store.page('jobs', limit=200)['total'] == 7
    store.close()


def test_page_count_and_items_share_a_read_snapshot(tmp_path):
    path = tmp_path / 'store.sqlite3'
    store, writer = Store(path), Store(path)
    for number in range(3):
        store.put('jobs', str(number), {'id': str(number), 'status': 'QUEUED'})
    changed = False

    def mutate_between_count_and_items(statement):
        nonlocal changed
        if not changed and statement.lstrip().startswith('SELECT seq,body'):
            changed = True
            writer.put('jobs', '1', {'id': '1', 'status': 'FAILED'})

    store.db.set_trace_callback(mutate_between_count_and_items)
    page = store.page('jobs', status='QUEUED', limit=3)
    store.db.set_trace_callback(None)
    assert changed
    assert page['total'] == len(page['items']) == 3
    assert store.count('jobs', status='QUEUED') == 2
    writer.close()
    store.close()


def test_public_read_transaction_keeps_snapshot_and_reuses_outer_write(tmp_path):
    path = tmp_path / 'store.sqlite3'
    store, writer = Store(path), Store(path)
    store.put('jobs', 'first', {'id': 'first'})
    with store.read_transaction():
        assert store.count('jobs') == 1
        writer.put('jobs', 'second', {'id': 'second'})
        with store.read_transaction():
            assert store.count('jobs') == 1
        assert store.db.in_transaction
    assert not store.db.in_transaction
    assert store.count('jobs') == 2
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.put('jobs', 'rolled-back', {'id': 'rolled-back'})
            with store.read_transaction():
                assert store.count('jobs') == 3
            assert store.db.in_transaction
            raise RuntimeError('rollback outer write')
    assert store.count('jobs') == 2
    with pytest.raises(RuntimeError):
        with store.read_transaction():
            assert store.count('jobs') == 2
            raise RuntimeError('close failed read')
    assert not store.db.in_transaction
    writer.close()
    store.close()


@pytest.mark.parametrize('change', [
    {'v': True}, {'v': 99}, {'instance': 'different'}, {'kind': 'projects'},
    {'status': ['RUNNING']}, {'project_id': 'other'}, {'after': True},
    {'after': -1}, {'after': 1.5}, {'after': float('nan')}, {'after': 2**80},
    {'upper': -1}, {'upper': True}, {'upper': float('inf')}, {'unexpected': 1},
])
def test_page_rejects_invalid_or_mismatched_cursor_fields(tmp_path, change):
    store = Store(tmp_path / 'store.sqlite3')
    for number in range(2):
        store.put('jobs', str(number), {'id': str(number), 'status': 'QUEUED', 'project_id': 'p'})
    cursor = store.page('jobs', status='QUEUED', project_id='p', limit=1)['next_cursor']
    value = unpack_cursor(cursor)
    value.update(change)
    with pytest.raises(ValueError):
        store.page('jobs', status='QUEUED', project_id='p', limit=1, cursor=pack_cursor(value))
    for invalid in ('', '?', 'x' * 1025, True):
        with pytest.raises(ValueError):
            store.page('jobs', cursor=invalid)
    store.close()


def test_backup_clone_and_fresh_restore_have_expected_instance_boundaries(tmp_path):
    source = Store(tmp_path / 'source.sqlite3')
    for number in range(3):
        source.put('jobs', str(number), {'id': str(number), 'status': 'QUEUED'})
    cursor = source.page('jobs', limit=1)['next_cursor']
    with sqlite3.connect(tmp_path / 'backup.sqlite3') as snapshot:
        source.db.backup(snapshot)
    backup = Store(tmp_path / 'backup.sqlite3')
    assert backup.instance_id == source.instance_id
    assert backup.page('jobs', cursor=cursor)['total'] == 3
    restored = Store(tmp_path / 'restored.sqlite3')
    with restored.transaction():
        for kind, identity, raw in backup.db.execute('SELECT kind,id,body FROM entities ORDER BY seq'):
            restored.put(kind, identity, json.loads(raw))
    assert restored.list('jobs') == source.list('jobs')
    assert restored.count('jobs', status='QUEUED') == 3
    assert restored.instance_id != source.instance_id
    with pytest.raises(ValueError):
        restored.page('jobs', cursor=cursor)
    restored.close()
    backup.close()
    source.close()


def test_old_sqlite_backup_migrates_without_losing_auxiliary_records(tmp_path):
    path, archive = tmp_path / 'old.sqlite3', tmp_path / 'old-backup.sqlite3'
    raw = '{ "id":"j", "status":"QUEUED", "project_id":"p" }'
    legacy_database(path, [(9, 'jobs', 'j', raw)])
    with sqlite3.connect(path) as source:
        source.execute('CREATE TABLE events(job_id TEXT,seq INTEGER,body TEXT NOT NULL,PRIMARY KEY(job_id,seq))')
        source.execute('CREATE TABLE audit(seq INTEGER PRIMARY KEY AUTOINCREMENT,body TEXT NOT NULL)')
        source.execute('CREATE TABLE idempotency(key TEXT PRIMARY KEY,digest TEXT NOT NULL,job_id TEXT NOT NULL)')
        source.execute('INSERT INTO events VALUES(?,?,?)', ('j', 4, '{"seq":4,"event":"QUEUED"}'))
        source.execute('INSERT INTO audit(body) VALUES(?)', ('{"action":"job.submit"}',))
        source.execute('INSERT INTO idempotency VALUES(?,?,?)', ('retry', 'digest', 'j'))
    with sqlite3.connect(path) as source, sqlite3.connect(archive) as snapshot:
        source.backup(snapshot)
    migrated = Store(archive)
    assert migrated.db.execute('SELECT seq,body FROM entities').fetchone() == (9, raw)
    assert migrated.count('jobs', status='QUEUED', project_id='p') == 1
    assert migrated.events('j') == [{'seq': 4, 'event': 'QUEUED'}]
    assert migrated.audit() == [{'action': 'job.submit'}]
    assert migrated.db.execute('SELECT key,digest,job_id FROM idempotency').fetchall() == [('retry', 'digest', 'j')]
    migrated.put('jobs', 'next', {'id': 'next'})
    assert migrated.db.execute("SELECT seq FROM entities WHERE id='next'").fetchone()[0] > 9
    migrated.close()
    with sqlite3.connect(path) as original:
        assert original.execute('PRAGMA user_version').fetchone()[0] == 0
        assert original.execute('SELECT body FROM entities').fetchone()[0] == raw


@pytest.mark.parametrize('limit', [0, -1, True, 1.1, '2', 1001])
def test_select_rejects_invalid_limits(tmp_path, limit):
    store = Store(tmp_path / 'store.sqlite3')
    with pytest.raises(ValueError):
        store.select('jobs', limit=limit)
    store.close()


@pytest.mark.parametrize('kwargs', [
    {'limit': 201}, {'limit': True}, {'status': ()}, {'status': []},
    {'status': ('QUEUED', 1)}, {'project_id': True},
])
def test_page_rejects_invalid_arguments(tmp_path, kwargs):
    store = Store(tmp_path / 'store.sqlite3')
    with pytest.raises(ValueError):
        store.page('jobs', **kwargs)
    store.close()


def test_filter_values_are_bound_and_status_tuples_are_equivalent(tmp_path):
    store = Store(tmp_path / 'store.sqlite3')
    suspicious = "p' OR 1=1 --"
    store.put('jobs', 'a', {'id': 'a', 'status': 'QUEUED', 'project_id': suspicious})
    store.put('jobs', 'b', {'id': 'b', 'status': 'RUNNING', 'project_id': suspicious})
    store.put('jobs', 'c', {'id': 'c', 'status': 'FAILED', 'project_id': 'other'})
    page = store.page('jobs', status=('RUNNING', 'QUEUED'), project_id=suspicious, limit=1)
    continued = store.page('jobs', status=('QUEUED', 'RUNNING', 'RUNNING'), project_id=suspicious, cursor=page['next_cursor'])
    assert [item['id'] for item in continued['items']] == ['b']
    assert continued['total'] == 2
    assert store.count('jobs', project_id="x' OR 1=1 --") == 0
    store.close()


def test_event_bounds_descending_and_legacy_replay(tmp_path):
    store = Store(tmp_path / 'store.sqlite3')
    assert store.event_max_seq('empty') == 0
    for number in range(7):
        store.append_event('a', {'event': str(number)})
    store.append_event('b', {'event': 'separate'})
    assert store.event_max_seq('a') == 7
    assert [item['seq'] for item in store.events('a')] == list(range(1, 8))
    assert [item['seq'] for item in store.events('a', 2, limit=2, until=6)] == [3, 4]
    assert [item['seq'] for item in store.events('a', 2, limit=2, until=6, descending=True)] == [6, 5]
    assert store.events('a', 7) == []
    assert store.events('a', 5, until=2) == []
    assert len(store.events('a', limit=1001)) == 7
    store.close()


@pytest.mark.parametrize('kwargs', [
    {'after': True}, {'after': -1}, {'after': 1.5}, {'after': 2**80},
    {'until': True}, {'until': -1}, {'limit': 0}, {'limit': True},
    {'limit': 1002}, {'descending': 1},
])
def test_events_reject_invalid_bounds(tmp_path, kwargs):
    store = Store(tmp_path / 'store.sqlite3')
    with pytest.raises(ValueError):
        store.events('a', **kwargs)
    store.close()
