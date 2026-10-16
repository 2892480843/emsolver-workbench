"""PostgreSQL implementation of the transactional JSON document Store.

The legacy service performs read/modify/write operations rather than row-level
compare-and-swap. Every outer write transaction therefore acquires one advisory
lock per database/schema. This intentionally serializes legacy writers across
processes and machines; snapshot readers proceed concurrently. Callers must keep
external work outside transactions. Connections belong to individual threads;
new owners reclaim connections left by exited threads. Remaining connections
are released by close(); instances must be created after process forks.

Only trusted application SQL is accepted by ``db``. The bounded compatibility
adapter supports qmark parameters and the JSON scalar paths used by this project;
it is not an arbitrary SQLite dialect emulator. JSON bodies remain TEXT so their
exact serialized bytes survive migration and backup.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import uuid

from .store import Store, SCHEMA_VERSION, MAX_SEQUENCE, _integer, _projections


POSTGRES_SCHEMA_VERSION = 1
_TABLE_COLUMNS = {
    'entities': ('seq', 'kind', 'id', 'body', 'status', 'project_id'),
    'events': ('job_id', 'seq', 'body'),
    'audit': ('seq', 'body'),
    'idempotency': ('key', 'digest', 'job_id'),
    'store_meta': ('key', 'value'),
}
_CLUSTER_COLUMNS = {
    'cluster_workers': ('id', 'name', 'token_hash', 'project_ids', 'resources', 'backends',
                        'created_at', 'revoked_at', 'last_seen_at', 'capabilities'),
    'cluster_job_attempts': ('job_id', 'attempt', 'worker_id', 'job_kind', 'lease_hash', 'state',
                             'claimed_at', 'expires_at', 'last_heartbeat_at', 'finished_at',
                             'completion_digest', 'metrics'),
}


def _create_cluster_tables(store):
    # The trusted coordinator owns this additive schema; source SQLite DDL is
    # never read or executed. Its constructor only initializes the tables.
    from types import SimpleNamespace
    from .cluster import ClusterCoordinator
    ClusterCoordinator(SimpleNamespace(store=store))


def _schema_name(value):
    # Lower-case ASCII identifiers avoid quoting ambiguities and server truncation.
    if (not isinstance(value, str) or not re.fullmatch(r'[a-z_][a-z0-9_]{0,62}', value)
            or value in {'public', 'information_schema'} or value.startswith('pg_')):
        raise ValueError('Invalid PostgreSQL schema; use a dedicated lowercase application schema')
    return value


def _driver():
    try:
        import psycopg
    except ImportError:
        raise RuntimeError('PostgreSQL requires psycopg 3; install emsolver-workbench[postgres]') from None
    return psycopg


def _database_url(value):
    if not isinstance(value, str) or not value.startswith(('postgresql://', 'postgres://')):
        raise ValueError('Unsupported database URL; use postgresql:// or postgres://')
    return value


def _query_error(error):
    # Server diagnostics can include rejected document values. Keep a stable
    # machine-readable diagnostic without exposing SQL, parameters or credentials.
    state = getattr(error, 'sqlstate', None)
    suffix = ' (SQLSTATE ' + state + ')' if isinstance(state, str) and re.fullmatch(r'[A-Z0-9]{5}', state) else ''
    return RuntimeError('PostgreSQL metadata operation failed' + suffix)


def _qmark_sql(statement, bound):
    """Translate placeholders outside SQL literals/comments and escape percents."""
    if not isinstance(statement, str):
        raise TypeError('SQL statement must be a string')
    # Escape original percents before inserting our own %s placeholders. Doing
    # this afterward would mistake a literal '%s' for a generated placeholder.
    if bound:
        statement = statement.replace('%', '%%')
    output, index, state, dollar = [], 0, None, None
    while index < len(statement):
        char = statement[index]
        pair = statement[index:index + 2]
        if state == 'comment':
            output.append(char)
            if char == '\n': state = None
        elif state == 'block':
            if pair == '*/':
                output.append(pair); index += 1; state = None
            else: output.append(char)
        elif state == 'dollar':
            if statement.startswith(dollar, index):
                output.append(dollar); index += len(dollar) - 1; state = None
            else: output.append(char)
        elif state in ("'", '"'):
            output.append(char)
            if char == state:
                if index + 1 < len(statement) and statement[index + 1] == state:
                    output.append(char); index += 1
                else: state = None
        elif pair in ('--', '/*'):
            output.append(pair); index += 1; state = 'comment' if pair == '--' else 'block'
        elif char in ("'", '"'):
            output.append(char); state = char
        elif char == '$' and (match := re.match(r'\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$', statement[index:])):
            dollar = match.group(); output.append(dollar); index += len(dollar) - 1; state = 'dollar'
        elif char == '?':
            output.append('%s' if bound else '?')
        else:
            output.append(char)
        index += 1
    return ''.join(output)


class _Database:
    """Expose the small connection interface used by legacy Store consumers."""
    def __init__(self, store):
        self.store = store
        self.local = threading.local()

    @property
    def in_transaction(self):
        connection = self.store._connection()
        return connection.info.transaction_status != self.store._psycopg.pq.TransactionStatus.IDLE

    def execute(self, statement, parameters=None):
        callback = getattr(self.local, 'trace', None)
        if callback is not None:
            callback(statement)
        query = _qmark_sql(statement, parameters is not None)
        try:
            return self.store._connection().execute(query, parameters)
        except self.store._psycopg.Error as error:
            raise _query_error(error) from None

    def executemany(self, statement, rows):
        cursor = self.store._connection().cursor()
        try:
            cursor.executemany(_qmark_sql(statement, True), rows)
        except self.store._psycopg.Error as error:
            cursor.close()
            raise _query_error(error) from None
        except BaseException:
            cursor.close()
            raise
        return cursor

    def set_trace_callback(self, callback):
        self.local.trace = callback


class PostgresStore(Store):
    dialect = 'postgresql'

    def __init__(self, dsn, *, schema='emsolver', path=None):
        self.schema = _schema_name(schema)
        self._dsn = _database_url(dsn)
        self._psycopg = _driver()
        self.path = Path(path) if path is not None else None
        self.lock = threading.RLock()
        self._connections_lock = threading.Lock()
        self._connections = []
        self._connection_owners = {}
        self._local = threading.local()
        self._closed = False
        self._pid = os.getpid()
        self._advisory_key = int.from_bytes(hashlib.sha256(('emsolver:' + schema).encode()).digest()[:8], 'big', signed=True)
        self.db = _Database(self)
        try:
            self._initialize_postgres()
        except BaseException:
            self.close()
            raise

    def _connection(self):
        if self._closed:
            raise RuntimeError('PostgreSQL store is closed')
        if os.getpid() != self._pid:
            raise RuntimeError('Create a new PostgreSQL store after forking')
        connection = getattr(self._local, 'connection', None)
        if connection is not None and connection.closed:
            if getattr(self._local, 'transaction_depth', 0):
                raise RuntimeError('PostgreSQL metadata connection lost during transaction')
            # Recover idle connections after an outage; failed operations are
            # never replayed because a write's commit outcome may be unknown.
            with self._connections_lock:
                if connection in self._connections: self._connections.remove(connection)
                self._connection_owners.pop(connection, None)
            self._local.connection = None
            connection = None
        if connection is None:
            with self._connections_lock:
                if self._closed:
                    raise RuntimeError('PostgreSQL store is closed')
                # AnyIO may retire idle request threads. Keeping their sessions
                # until shutdown eventually exhausts max_connections even with
                # little concurrency. Reap only exited owners; live transactions
                # retain their thread-affine connection and are never replayed.
                for stale, owner in list(self._connection_owners.items()):
                    if not owner.is_alive():
                        stale.close()
                        self._connections.remove(stale)
                        del self._connection_owners[stale]
            try:
                connection = self._psycopg.connect(self._dsn, autocommit=True, connect_timeout=10)
                connection.execute(self._psycopg.sql.SQL('SET search_path TO {}, pg_catalog').format(self._psycopg.sql.Identifier(self.schema)))
            except self._psycopg.Error:
                if connection is not None: connection.close()
                # libpq diagnostics can embed passwords or URLs. Do not propagate them.
                raise RuntimeError('Unable to connect to PostgreSQL metadata database') from None
            with self._connections_lock:
                if self._closed:
                    connection.close()
                    raise RuntimeError('PostgreSQL store is closed')
                self._connections.append(connection)
                self._connection_owners[connection] = threading.current_thread()
            self._local.connection = connection
        return connection

    @contextmanager
    def transaction(self):
        """Serialize legacy writers across machines, committing only on success."""
        with self.lock:
            connection = self._connection()
            if getattr(self._local, 'read_only', False):
                raise RuntimeError('Cannot start a write transaction inside a read snapshot')
            outer = not self.db.in_transaction
            with connection.transaction():
                depth = getattr(self._local, 'transaction_depth', 0)
                self._local.transaction_depth = depth + 1
                try:
                    if outer:
                        connection.execute('SELECT pg_advisory_xact_lock(%s)', (self._advisory_key,))
                    yield
                finally:
                    self._local.transaction_depth = depth

    @contextmanager
    def _write(self):
        if self.db.in_transaction:
            yield
        else:
            with self.transaction():
                yield

    @contextmanager
    def read_transaction(self):
        """Use one repeatable, read-only snapshot; reuse an existing write owner."""
        with self.lock:
            connection = self._connection()
            if self.db.in_transaction:
                yield
                return
            with connection.transaction():
                connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
                self._local.read_only = True
                self._local.transaction_depth = 1
                try:
                    yield
                finally:
                    self._local.read_only = False
                    self._local.transaction_depth = 0

    def _initialize_postgres(self):
        sql = self._psycopg.sql
        with self.transaction():
            connection = self._connection()
            connection.execute(sql.SQL('CREATE SCHEMA IF NOT EXISTS {}').format(sql.Identifier(self.schema)))
            # A separate version table keeps this independent of the SQLite format.
            connection.execute('CREATE TABLE IF NOT EXISTS emsolver_schema(version INTEGER PRIMARY KEY)')
            versions = connection.execute('SELECT version FROM emsolver_schema').fetchall()
            if versions and versions != [(POSTGRES_SCHEMA_VERSION,)]:
                raise ValueError('Unsupported PostgreSQL store schema version')
            if not versions:
                occupied = connection.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=%s AND table_name <> 'emsolver_schema'", (self.schema,)).fetchall()
                if occupied:
                    raise ValueError('PostgreSQL initialization requires an empty dedicated schema')
                connection.execute('INSERT INTO emsolver_schema VALUES(%s)', (POSTGRES_SCHEMA_VERSION,))
            for statement in (
                '''CREATE TABLE IF NOT EXISTS entities(
                    seq BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                    kind TEXT NOT NULL,id TEXT NOT NULL,body TEXT NOT NULL,
                    status TEXT,project_id TEXT,UNIQUE(kind,id))''',
                'CREATE TABLE IF NOT EXISTS events(job_id TEXT NOT NULL,seq BIGINT NOT NULL,body TEXT NOT NULL,PRIMARY KEY(job_id,seq))',
                'CREATE TABLE IF NOT EXISTS audit(seq BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,body TEXT NOT NULL)',
                'CREATE TABLE IF NOT EXISTS idempotency(key TEXT PRIMARY KEY,digest TEXT NOT NULL,job_id TEXT NOT NULL)',
                'CREATE TABLE IF NOT EXISTS store_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)',
                'CREATE INDEX IF NOT EXISTS entities_kind_seq ON entities(kind,seq)',
                'CREATE INDEX IF NOT EXISTS entities_kind_status_seq ON entities(kind,status,seq)',
                'CREATE INDEX IF NOT EXISTS entities_kind_project_seq ON entities(kind,project_id,seq)',
                'CREATE INDEX IF NOT EXISTS entities_kind_project_status_seq ON entities(kind,project_id,status,seq)',
            ):
                connection.execute(statement)
            # Only scalar dot paths occur in the application. Return TEXT for string
            # comparisons; numeric callers must explicitly cast (as SQLite also allows).
            connection.execute('''CREATE OR REPLACE FUNCTION json_extract(document TEXT, json_path TEXT)
                RETURNS TEXT LANGUAGE SQL IMMUTABLE STRICT PARALLEL SAFE
                SET search_path = pg_catalog
                AS $$ SELECT CASE WHEN json_path ~ '^\\$\\.[A-Za-z_][A-Za-z0-9_]*(\\.[A-Za-z_][A-Za-z0-9_]*)*$'
                    THEN document::jsonb #>> string_to_array(substr(json_path,3),'.')
                    ELSE NULL END $$''')
            connection.execute('INSERT INTO store_meta(key,value) VALUES(%s,%s) ON CONFLICT(key) DO NOTHING', ('instance_id', uuid.uuid4().hex))
            self.instance_id = connection.execute("SELECT value FROM store_meta WHERE key='instance_id'").fetchone()[0]
            if not isinstance(self.instance_id, str) or not re.fullmatch(r'[0-9a-f]{32}', self.instance_id):
                raise ValueError('Invalid store instance identity')

    def export_sqlite(self, target_path):
        """Write a portable Store v2 SQLite snapshot, preserving rows and identity.

        The target must not exist; source metadata is read in one PG snapshot.
        Worker revocations and attempt fencing records are included when present.
        """
        target_path = Path(target_path)
        # O_EXCL prevents accidental replacement of an existing backup/database.
        descriptor = os.open(target_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        target = None
        try:
            target = Store(target_path)
            with self.read_transaction():
                present = {row[0] for row in self._connection().execute('SELECT table_name FROM information_schema.tables WHERE table_schema=%s', (self.schema,))}
                columns_to_copy = dict(_TABLE_COLUMNS)
                if set(_CLUSTER_COLUMNS) <= present:
                    _create_cluster_tables(target)
                    columns_to_copy.update(_CLUSTER_COLUMNS)
                with target.transaction():
                    target.db.execute('DELETE FROM store_meta')
                    for table, columns in columns_to_copy.items():
                        cursor = self.db.execute('SELECT ' + ','.join(columns) + ' FROM ' + table + ' ORDER BY ' + _row_order(table, columns))
                        try:
                            while rows := cursor.fetchmany(500):
                                target.db.executemany('INSERT INTO ' + table + '(' + ','.join(columns) + ') VALUES(' + ','.join('?' for _ in columns) + ')', rows)
                        finally:
                            cursor.close()
                    # PostgreSQL identity sequences can have gaps after rollback/upsert.
                    for table in ('entities', 'audit'):
                        value = self._sequence_highwater(table)
                        target.db.execute('DELETE FROM sqlite_sequence WHERE name=?', (table,))
                        target.db.execute('INSERT INTO sqlite_sequence(name,seq) VALUES(?,?)', (table, value))
            target.close(); target = None
            return target_path
        except BaseException:
            if target is not None: target.close()
            for suffix in ('', '-wal', '-shm'):
                Path(str(target_path) + suffix).unlink(missing_ok=True)
            raise

    def _sequence_highwater(self, table):
        # Table names are private constants, never user-controlled identifiers.
        if table not in ('entities', 'audit'):
            raise ValueError('Invalid sequence table')
        sequence = self._connection().execute('SELECT pg_get_serial_sequence(%s,%s)', (self.schema + '.' + table, 'seq')).fetchone()[0]
        row = self._connection().execute(self._psycopg.sql.SQL('SELECT last_value,is_called FROM {}').format(self._psycopg.sql.Identifier(*sequence.split('.')))).fetchone()
        maximum = self.db.execute('SELECT COALESCE(MAX(seq),0) FROM ' + table).fetchone()[0]
        return max(maximum, row[0] if row[1] else 0)

    def close(self):
        with self.lock, self._connections_lock:
            if self._closed: return
            self._closed = True
            for connection in self._connections:
                connection.close()
            self._connections.clear()
            self._connection_owners.clear()


def _row_order(table, columns):
    return 'job_id,seq' if table == 'events' else 'job_id,attempt' if table == 'cluster_job_attempts' else columns[0]


def _source_rows(connection, table, columns):
    cursor = connection.execute('SELECT ' + ','.join(columns) + ' FROM ' + table + ' ORDER BY ' + _row_order(table, columns))
    try:
        while rows := cursor.fetchmany(500):
            yield from rows
    finally:
        cursor.close()


def _validate_source(connection):
    """Validate known data only; never install source schema, triggers or SQL."""
    version = connection.execute('PRAGMA user_version').fetchone()[0]
    if version not in (0, SCHEMA_VERSION):
        raise ValueError('Unsupported SQLite source schema version')
    if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
        raise ValueError('SQLite source integrity check failed')
    present = dict(connection.execute("SELECT name,type FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'"))
    entity_columns = tuple(row[1] for row in connection.execute('PRAGMA table_info(entities)'))
    legacy = version == 0 and entity_columns == ('kind', 'id', 'body')
    if not legacy and entity_columns != _TABLE_COLUMNS['entities']:
        raise ValueError('Unsupported SQLite entities schema')
    if present.get('entities') != 'table':
        raise ValueError('SQLite source entities must be a table')
    counts, highwater = {}, {}
    columns_to_copy = dict(_TABLE_COLUMNS)
    if set(_CLUSTER_COLUMNS) & present.keys():
        if not set(_CLUSTER_COLUMNS) <= present.keys():
            raise ValueError('Incomplete SQLite cluster schema')
        columns_to_copy.update(_CLUSTER_COLUMNS)
    for table, columns in columns_to_copy.items():
        if table not in present:
            if table == 'entities': raise ValueError('SQLite source entities missing')
            counts[table] = 0
            continue
        if present[table] != 'table':
            raise ValueError('SQLite source objects must be tables')
        actual = tuple(row[1] for row in connection.execute('PRAGMA table_info(' + table + ')'))
        expected = ('kind', 'id', 'body') if table == 'entities' and legacy else columns
        if actual != expected:
            raise ValueError('Unsupported SQLite source table schema: ' + table)
        selected = ('rowid', 'kind', 'id', 'body') if table == 'entities' and legacy else columns
        count = 0
        seen = set()
        maximum = 0
        for row in _source_rows(connection, table, selected):
            count += 1
            if table in ('entities', 'audit', 'events'):
                raw = row[3] if table == 'entities' else row[2] if table == 'events' else row[1]
                try:
                    if not isinstance(raw, str): raise ValueError()
                    value = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                    # Also reject exponent overflow while preserving the original text.
                    json.dumps(value, allow_nan=False)
                except (ValueError, TypeError, RecursionError, UnicodeError):
                    raise ValueError('Invalid JSON in SQLite source ' + table) from None
                sequence = row[1] if table == 'events' else row[0]
                _integer(sequence, 'source sequence', minimum=1)
                maximum = max(maximum, sequence)
                if table == 'entities':
                    key = (row[1], row[2])
                    if not all(isinstance(item, str) and item for item in key):
                        raise ValueError('Invalid SQLite entity identity')
                    if not legacy and row[4:6] != _projections(value):
                        raise ValueError('SQLite entity projections do not match JSON')
                elif table == 'events':
                    if not isinstance(row[0], str) or not row[0]: raise ValueError('Invalid SQLite event identity')
                    key = (row[0], sequence)
                else: key = sequence
            elif table == 'idempotency':
                if not all(isinstance(item, str) for item in row): raise ValueError('Invalid SQLite idempotency record')
                key = row[0]
            elif table in _CLUSTER_COLUMNS:
                values = dict(zip(columns, row))
                fields = ('project_ids', 'resources', 'backends', 'capabilities') if table == 'cluster_workers' else ('metrics',)
                for field in fields:
                    raw = values[field]
                    if raw is None and field == 'capabilities': continue
                    try:
                        if not isinstance(raw, str): raise ValueError()
                        json.dumps(json.loads(raw), allow_nan=False)
                    except (ValueError, TypeError, RecursionError):
                        raise ValueError('Invalid JSON in SQLite source ' + table) from None
                key = values['id'] if table == 'cluster_workers' else (values['job_id'], values['attempt'])
            else:
                if not all(isinstance(item, str) for item in row): raise ValueError('Invalid SQLite metadata record')
                key = row[0]
            if key in seen:
                raise ValueError('Duplicate SQLite source key')
            seen.add(key)
        counts[table] = count
        if table in ('entities', 'audit'):
            stored = connection.execute("SELECT seq FROM sqlite_sequence WHERE name=?", (table,)).fetchone() if 'sqlite_sequence' in dict(connection.execute('SELECT name,type FROM sqlite_schema')) else None
            if stored is not None:
                _integer(stored[0], 'source sequence highwater')
                maximum = max(maximum, stored[0])
            highwater[table] = maximum
    return legacy, present, counts, highwater, columns_to_copy


def _target_is_empty(connection, schema, psycopg):
    tables = connection.execute('SELECT table_name FROM information_schema.tables WHERE table_schema=%s', (schema,)).fetchall()
    allowed = set(_TABLE_COLUMNS) | set(_CLUSTER_COLUMNS) | {'emsolver_schema'}
    if any(row[0] not in allowed for row in tables):
        raise ValueError('Migration requires an empty PostgreSQL schema')
    if ('emsolver_schema',) in tables:
        versions = connection.execute(psycopg.sql.SQL('SELECT version FROM {}.emsolver_schema').format(psycopg.sql.Identifier(schema))).fetchall()
        if versions not in ([], [(POSTGRES_SCHEMA_VERSION,)]):
            raise ValueError('Unsupported PostgreSQL store schema version')
    for (table,) in tables:
        if table in {'emsolver_schema', 'store_meta'}:
            continue
        row = connection.execute(psycopg.sql.SQL('SELECT 1 FROM {}.{} LIMIT 1').format(psycopg.sql.Identifier(schema), psycopg.sql.Identifier(table))).fetchone()
        if row:
            raise ValueError('Migration requires an empty PostgreSQL schema')
    if ('store_meta',) in tables:
        rows = connection.execute(psycopg.sql.SQL('SELECT key FROM {}.store_meta').format(psycopg.sql.Identifier(schema))).fetchall()
        if any(row[0] != 'instance_id' for row in rows):
            raise ValueError('Migration requires an empty PostgreSQL schema')


def migrate_sqlite_to_postgres(source_path, dsn, *, schema='emsolver', dry_run=False):
    """Copy one read-only SQLite snapshot into an EMPTY dedicated PG schema.

    Original JSON text, row sequences, SQLite sequence high-water marks, events,
    audits and idempotency records are retained. A fresh instance identity rejects
    source pagination cursors. No source SQL or schema definition is executed.
    """
    schema = _schema_name(schema)
    dsn = _database_url(dsn)
    source_path = Path(source_path).resolve(strict=True)
    if not source_path.is_file(): raise ValueError('SQLite source must be a regular file')
    source = sqlite3.connect(source_path.as_uri() + '?mode=ro', uri=True)
    target = None
    try:
        source.execute('PRAGMA trusted_schema=OFF')
        source.execute('PRAGMA query_only=ON')
        source.execute('BEGIN')
        legacy, present, counts, highwater, columns_to_copy = _validate_source(source)
        report = {'dry_run': bool(dry_run), 'source': str(source_path), 'schema': schema,
                  'source_schema_version': source.execute('PRAGMA user_version').fetchone()[0],
                  'postgres_schema_version': POSTGRES_SCHEMA_VERSION,
                  'counts': {key: counts[key] for key in ('entities', 'events', 'audit', 'idempotency')}}
        report['counts'].update({key: counts[key] for key in _CLUSTER_COLUMNS if key in counts})
        psycopg = _driver()
        # Check emptiness before schema initialization. The write lock closes the
        # check/copy race with other EMSolver writers in this destination schema.
        try:
            control = psycopg.connect(dsn, autocommit=True, connect_timeout=10)
        except psycopg.Error:
            raise RuntimeError('Unable to connect to PostgreSQL metadata database') from None
        with control:
            with control.transaction():
                key = int.from_bytes(hashlib.sha256(('emsolver:' + schema).encode()).digest()[:8], 'big', signed=True)
                control.execute('SELECT pg_advisory_xact_lock(%s)', (key,))
                _target_is_empty(control, schema, psycopg)
                if dry_run:
                    return report
        target = PostgresStore(dsn, schema=schema)
        with target.transaction():
            _target_is_empty(target._connection(), schema, psycopg)
            if set(_CLUSTER_COLUMNS) <= present.keys():
                _create_cluster_tables(target)
            for table, columns in columns_to_copy.items():
                if table not in present: continue
                selected = ('rowid', 'kind', 'id', 'body') if table == 'entities' and legacy else columns
                batch = []
                for row in _source_rows(source, table, selected):
                    if table == 'store_meta' and row[0] == 'instance_id': continue
                    if table == 'entities' and legacy: row = (*row, *_projections(json.loads(row[3])))
                    batch.append(row)
                    if len(batch) == 500:
                        target.db.executemany('INSERT INTO ' + table + '(' + ','.join(columns) + ') VALUES(' + ','.join('?' for _ in columns) + ')', batch).close()
                        batch = []
                if batch:
                    target.db.executemany('INSERT INTO ' + table + '(' + ','.join(columns) + ') VALUES(' + ','.join('?' for _ in columns) + ')', batch).close()
            for table in ('entities', 'audit'):
                maximum = highwater.get(table, 0)
                target._connection().execute('SELECT setval(pg_get_serial_sequence(%s,%s),%s,%s)', (schema + '.' + table, 'seq', maximum or 1, maximum > 0))
        return report
    finally:
        if target is not None: target.close()
        source.close()
