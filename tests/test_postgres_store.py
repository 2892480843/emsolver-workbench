"""PostgreSQL contract tests; set EMSOLVER_TEST_DATABASE_URL for real database checks."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from emsolver.persistence import open_store
from emsolver.postgres_store import PostgresStore, migrate_sqlite_to_postgres
from emsolver.store import Store


class _PrivateDsn(str):
    def __repr__(self):
        return '<isolated PostgreSQL DSN>'


@pytest.mark.parametrize('url', ['sqlite:///secret', 'http://localhost/db', 'postgresql+psycopg://localhost/db'])
def test_factory_rejects_unsupported_database_urls_without_echoing_them(tmp_path, monkeypatch, url):
    monkeypatch.setenv('EMSOLVER_DATABASE_URL', url)
    with pytest.raises(ValueError, match='Unsupported database URL') as caught:
        open_store(tmp_path / 'state.sqlite3')
    assert url not in str(caught.value)


def test_factory_defaults_to_local_sqlite(tmp_path, monkeypatch):
    monkeypatch.delenv('EMSOLVER_DATABASE_URL', raising=False)
    store = open_store(tmp_path / 'state.sqlite3')
    try:
        assert type(store) is Store
    finally:
        store.close()


@pytest.mark.parametrize('schema', ['public', 'pg_catalog', 'pg_test', 'bad-name', 'x; DROP SCHEMA public', '', 'X', 'a' * 64])
def test_schema_configuration_is_validated_before_connecting(schema):
    with pytest.raises(ValueError, match='schema'):
        PostgresStore('postgresql://not-used', schema=schema)


@pytest.fixture
def pg_schema():
    dsn = os.environ.get('EMSOLVER_TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('Set EMSOLVER_TEST_DATABASE_URL to run isolated real PostgreSQL tests')
    psycopg = pytest.importorskip('psycopg')
    schema = 'test_emsolver_' + uuid.uuid4().hex
    try:
        yield _PrivateDsn(dsn), schema
    finally:
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(psycopg.sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(psycopg.sql.Identifier(schema)))


@pytest.fixture
def pg_store(pg_schema):
    store = PostgresStore(pg_schema[0], schema=pg_schema[1])
    try:
        yield store
    finally:
        store.close()


def test_documents_projections_pages_raw_queries_and_reopen(pg_store, pg_schema):
    store = pg_store
    for index in range(4):
        store.put('jobs', str(index), {'id': str(index), 'status': 'QUEUED', 'project_id': 'p', 'matrix_id': 'm'})
    first = store.page('jobs', project_id='p', limit=2)
    sequence = store.db.execute('SELECT seq FROM entities WHERE id=?', ('0',)).fetchone()[0]
    store.put('jobs', '0', {'id': '0', 'status': 'RUNNING', 'project_id': 'q'})
    assert store.db.execute('SELECT seq,status,project_id FROM entities WHERE id=?', ('0',)).fetchone() == (sequence, 'RUNNING', 'q')
    assert store.count('jobs', status=('QUEUED', 'RUNNING')) == 4
    assert store.status_counts('jobs') == {'QUEUED': 3, 'RUNNING': 1}
    assert store.db.execute("SELECT count(*) FROM entities WHERE json_extract(body,'$.matrix_id')=?", ('m',)).fetchone()[0] == 3
    assert store.db.execute("SELECT '?' AS literal,? AS value,'50%' AS percent", ('safe',)).fetchone() == ('?', 'safe', '50%')
    store.put('jobs', 'late', {'id': 'late', 'project_id': 'p'})
    assert [item['id'] for item in store.page('jobs', project_id='p', limit=2, cursor=first['next_cursor'])['items']] == ['2', '3']
    reopened = PostgresStore(pg_schema[0], schema=pg_schema[1])
    try:
        assert reopened.instance_id == store.instance_id
        assert reopened.get('jobs', 'late')['id'] == 'late'
    finally:
        reopened.close()


def test_read_snapshot_and_outer_rollback_are_consistent(pg_store, pg_schema):
    writer = PostgresStore(pg_schema[0], schema=pg_schema[1])
    try:
        pg_store.put('jobs', 'a', {'status': 'QUEUED'})
        with pg_store.read_transaction():
            assert pg_store.count('jobs') == 1
            writer.put('jobs', 'b', {'status': 'RUNNING'})
            with pg_store.read_transaction():
                assert pg_store.count('jobs') == 1
        assert pg_store.count('jobs') == 2
        with pytest.raises(RuntimeError, match='rollback'):
            with pg_store.transaction():
                pg_store.put('jobs', 'c', {'status': 'QUEUED'})
                pg_store.append_event('c', {'message': 'uncommitted'})
                pg_store.audit('created', 'c')
                with pg_store.read_transaction():
                    assert pg_store.count('jobs') == 3
                raise RuntimeError('rollback')
        assert pg_store.get('jobs', 'c') is None
        assert pg_store.events('c') == []
        assert pg_store.audit() == []
        assert not pg_store.db.in_transaction
    finally:
        writer.close()


def test_multiple_connections_serialize_legacy_read_modify_write_and_events(pg_store, pg_schema):
    pg_store.put('usage', 'p', {'value': 0})
    barrier = threading.Barrier(4)
    def update(_):
        store = PostgresStore(pg_schema[0], schema=pg_schema[1])
        try:
            barrier.wait(timeout=15)
            for _ in range(8):
                with store.transaction():
                    value = store.get('usage', 'p')['value']
                    store.put('usage', 'p', {'value': value + 1})
                    store.append_event('job', {'message': str(value)})
        finally:
            store.close()
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(update, range(4)))
    assert pg_store.get('usage', 'p') == {'value': 32}
    assert [event['seq'] for event in pg_store.events('job')] == list(range(1, 33))


def test_one_store_uses_separate_thread_connections_and_close_is_final(pg_store):
    barrier = threading.Barrier(3)
    def connection_id(_):
        pid = pg_store.db.execute('SELECT pg_backend_pid()').fetchone()[0]
        barrier.wait(timeout=10)
        return pid
    with ThreadPoolExecutor(max_workers=3) as executor:
        assert len(set(executor.map(connection_id, range(3)))) == 3
    pg_store.close()
    with pytest.raises(RuntimeError, match='closed'):
        pg_store.count('jobs')


def _source(tmp_path):
    path = tmp_path / 'source.sqlite3'
    store = Store(path)
    store.put('jobs', 'a', {'id': 'a', 'status': 'QUEUED', 'project_id': 'p'})
    store.put('jobs', 'b', {'id': 'b', 'status': 'RUNNING', 'project_id': 'p'})
    cursor = store.page('jobs', limit=1)['next_cursor']
    raw = '{ "id" : "a", "status": "QUEUED", "project_id": "p", "text": "中文" }'
    with store.transaction():
        store.db.execute('UPDATE entities SET body=? WHERE id=?', (raw, 'a'))
        store.db.execute('INSERT INTO idempotency VALUES(?,?,?)', ('key', 'digest', 'a'))
        store.db.execute('UPDATE sqlite_sequence SET seq=42 WHERE name=?', ('entities',))
        store.append_event('a', {'message': 'first'})
        store.append_event('a', {'message': 'second'})
        store.audit('created', 'a')
    store.close()
    return path, raw, cursor


def test_migration_preserves_exact_rows_sequences_auxiliary_records_and_source(pg_schema, tmp_path):
    path, raw, cursor = _source(tmp_path)
    original = hashlib.sha256(path.read_bytes()).hexdigest()
    dry = migrate_sqlite_to_postgres(path, pg_schema[0], schema=pg_schema[1], dry_run=True)
    assert dry['dry_run'] is True and dry['counts']['entities'] == 2
    report = migrate_sqlite_to_postgres(path, pg_schema[0], schema=pg_schema[1])
    assert report['counts'] == {'entities': 2, 'events': 2, 'audit': 1, 'idempotency': 1}
    assert hashlib.sha256(path.read_bytes()).hexdigest() == original
    store = PostgresStore(pg_schema[0], schema=pg_schema[1])
    try:
        assert store.db.execute('SELECT body FROM entities WHERE id=?', ('a',)).fetchone()[0] == raw
        assert store.db.execute('SELECT digest,job_id FROM idempotency WHERE key=?', ('key',)).fetchone() == ('digest', 'a')
        assert [row['seq'] for row in store.events('a')] == [1, 2]
        assert len(store.audit()) == 1
        with pytest.raises(ValueError, match='instance'):
            store.page('jobs', limit=1, cursor=cursor)
        store.put('jobs', 'c', {'id': 'c'})
        assert store.db.execute('SELECT seq FROM entities WHERE id=?', ('c',)).fetchone()[0] == 43
        with pytest.raises(ValueError, match='empty'):
            migrate_sqlite_to_postgres(path, pg_schema[0], schema=pg_schema[1])
        snapshot = tmp_path / 'snapshot.sqlite3'
        store.export_sqlite(snapshot)
        clone = Store(snapshot)
        try:
            assert clone.db.execute('SELECT body FROM entities WHERE id=?', ('a',)).fetchone()[0] == raw
            assert clone.instance_id == store.instance_id
            assert len(clone.events('a')) == 2
        finally:
            clone.close()
    finally:
        store.close()


def test_invalid_source_does_not_create_target_schema(pg_schema, tmp_path):
    path, _, _ = _source(tmp_path)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE entities SET body='not-json' WHERE id='b'")
    with pytest.raises(ValueError, match='JSON'):
        migrate_sqlite_to_postgres(path, pg_schema[0], schema=pg_schema[1])
    import psycopg
    with psycopg.connect(pg_schema[0]) as connection:
        assert connection.execute('SELECT 1 FROM pg_namespace WHERE nspname=%s', (pg_schema[1],)).fetchone() is None


def test_sql_compatibility_preserves_percent_literals(pg_store):
    assert pg_store.db.execute("SELECT '%s',?,'100%' -- ?\n", ('bound',)).fetchone() == ('%s', 'bound', '100%')
    assert pg_store.db.execute("SELECT $$? %s$$,? /* ? */", ('bound',)).fetchone() == ('? %s', 'bound')


def test_export_never_replaces_existing_file(pg_store, tmp_path):
    path = tmp_path / 'existing.sqlite3'
    path.write_bytes(b'preserve')
    with pytest.raises(FileExistsError):
        pg_store.export_sqlite(path)
    assert path.read_bytes() == b'preserve'


def test_legacy_sqlite_migration_preserves_rowid_and_missing_auxiliary_tables(pg_schema, tmp_path):
    path = tmp_path / 'legacy.sqlite3'
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE entities(kind TEXT,id TEXT,body TEXT NOT NULL,PRIMARY KEY(kind,id))')
        connection.execute('INSERT INTO entities(rowid,kind,id,body) VALUES(?,?,?,?)', (7, 'jobs', 'a', '{ "id":"a", "project_id":"p" }'))
    original = path.read_bytes()
    result = migrate_sqlite_to_postgres(path, pg_schema[0], schema=pg_schema[1])
    assert result['counts'] == {'entities': 1, 'events': 0, 'audit': 0, 'idempotency': 0}
    store = PostgresStore(pg_schema[0], schema=pg_schema[1])
    try:
        assert store.db.execute('SELECT seq,project_id FROM entities').fetchone() == (7, 'p')
        store.put('jobs', 'b', {'id': 'b'})
        assert store.db.execute('SELECT seq FROM entities WHERE id=?', ('b',)).fetchone()[0] == 8
    finally:
        store.close()
    assert path.read_bytes() == original


def test_migration_cli_dry_run_and_report_do_not_expose_credentials(pg_schema, tmp_path):
    path, _, _ = _source(tmp_path)
    report = tmp_path / 'migration-report.json'
    result = subprocess.run([sys.executable, 'scripts/migrate_sqlite_to_postgres.py', str(path), '--dry-run', '--report', str(report)],
                            env={**os.environ, 'EMSOLVER_DATABASE_URL': pg_schema[0], 'EMSOLVER_DATABASE_SCHEMA': pg_schema[1]},
                            text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['counts']['entities'] == 2
    assert json.loads(report.read_text())['dry_run'] is True
    assert pg_schema[0] not in result.stdout + result.stderr + report.read_text()


def test_cluster_worker_and_attempt_records_survive_export_and_migration(pg_store, pg_schema, tmp_path):
    from types import SimpleNamespace
    from emsolver.cluster import ClusterCoordinator
    ClusterCoordinator(SimpleNamespace(store=pg_store))
    pg_store.put('jobs', 'job', {'id': 'job', 'status': 'RUNNING'})
    with pg_store.transaction():
        pg_store.db.execute('INSERT INTO cluster_workers VALUES(?,?,?,?,?,?,?,?,?,?)',
                            ('w', 'Worker', 'tokenhash', '["p"]', '{"cpu":1}', '["scipy"]', 1.0, None, 2.0, None))
        pg_store.db.execute('INSERT INTO cluster_job_attempts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                            ('job', 1, 'w', 'jobs', 'leasehash', 'ACTIVE', 1.0, 9.0, 2.0, None, None, '{}'))
    path = tmp_path / 'cluster.sqlite3'
    pg_store.export_sqlite(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute('SELECT id,token_hash FROM cluster_workers').fetchone() == ('w', 'tokenhash')
        assert connection.execute('SELECT job_id,attempt,lease_hash,state FROM cluster_job_attempts').fetchone() == ('job', 1, 'leasehash', 'ACTIVE')
    destination = 'test_emsolver_' + uuid.uuid4().hex
    try:
        result = migrate_sqlite_to_postgres(path, pg_schema[0], schema=destination)
        assert result['counts']['cluster_workers'] == result['counts']['cluster_job_attempts'] == 1
        clone = PostgresStore(pg_schema[0], schema=destination)
        try:
            assert clone.db.execute('SELECT worker_id,state FROM cluster_job_attempts').fetchone() == ('w', 'ACTIVE')
        finally:
            clone.close()
    finally:
        import psycopg
        with psycopg.connect(pg_schema[0], autocommit=True) as connection:
            connection.execute(psycopg.sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(psycopg.sql.Identifier(destination)))


def test_database_errors_do_not_echo_values_and_leave_transaction_usable(pg_store):
    with pytest.raises(RuntimeError, match='PostgreSQL metadata operation failed') as caught:
        with pg_store.transaction():
            pg_store.db.execute('SELECT CAST(? AS INTEGER)', ('sensitive-document-value',))
    assert 'sensitive-document-value' not in str(caught.value)
    assert pg_store.count('jobs') == 0


def test_closed_thread_connection_is_replaced_without_replaying_a_write(pg_store):
    # Closing only this isolated Store's idle connection simulates libpq marking
    # a connection dead after an outage; the next operation needs a fresh one.
    previous = pg_store.db.execute('SELECT pg_backend_pid()').fetchone()[0]
    pg_store._connection().close()
    assert pg_store.count('jobs') == 0
    assert pg_store.db.execute('SELECT pg_backend_pid()').fetchone()[0] != previous


def test_lost_connection_inside_transaction_never_starts_an_independent_write(pg_store):
    with pytest.raises(RuntimeError):
        with pg_store.transaction():
            pg_store.put('jobs', 'before', {'id': 'before'})
            pg_store._connection().close()
            pg_store.put('jobs', 'after', {'id': 'after'})
    assert pg_store.get('jobs', 'before') is None
    assert pg_store.get('jobs', 'after') is None


def test_exited_request_threads_do_not_accumulate_database_sessions(pg_store):
    retired_pids = []
    failures = []

    def request():
        try:
            retired_pids.append(pg_store.db.execute('SELECT pg_backend_pid()').fetchone()[0])
        except Exception as error:
            failures.append(type(error).__name__)

    # Short-lived request threads must not each retain a PostgreSQL backend until
    # application shutdown. This observes server sessions, not the registry.
    for _ in range(12):
        thread = threading.Thread(target=request)
        thread.start()
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert not failures
    remaining = pg_store.db.execute(
        'SELECT count(*) FROM pg_stat_activity WHERE pid = ANY(?)',
        (retired_pids,),
    ).fetchone()[0]
    assert remaining <= 1
    assert pg_store.db.execute('SELECT 1').fetchone() == (1,)
