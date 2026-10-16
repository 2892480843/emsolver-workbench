"""Transactional JSON documents with indexed projections and stable sequences."""
from __future__ import annotations
import base64
import binascii
import json
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


SCHEMA_VERSION = 2
MAX_SEQUENCE = 2**63 - 1
ENTITY_SCHEMA = '''(
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    id TEXT NOT NULL,
    body TEXT NOT NULL,
    status TEXT,
    project_id TEXT,
    UNIQUE(kind,id)
)'''


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def _integer(value, name, minimum=0, maximum=MAX_SEQUENCE):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f'{name} must be an integer between {minimum} and {maximum}')
    return value


def _projections(body):
    if not isinstance(body, dict):
        return None, None
    return tuple(body.get(key) if isinstance(body.get(key), str) else None for key in ('status', 'project_id'))


def _filters(kind, status, project_id):
    if not isinstance(kind, str) or not kind:
        raise ValueError('kind must be a nonempty string')
    if project_id is not None and (not isinstance(project_id, str) or not project_id):
        raise ValueError('project_id must be a nonempty string')
    if status is not None:
        values = (status,) if isinstance(status, str) else status
        if not isinstance(values, tuple) or not values or any(not isinstance(value, str) or not value for value in values):
            raise ValueError('status must be a string or a nonempty tuple of strings')
        status = tuple(sorted(set(values)))
    clauses, parameters = ['kind=?'], [kind]
    if project_id is not None:
        clauses.append('project_id=?')
        parameters.append(project_id)
    if status is not None:
        clauses.append('status=?' if len(status) == 1 else 'status IN (' + ','.join('?' for _ in status) + ')')
        parameters.extend(status)
    return ' AND '.join(clauses), parameters, status


def _cursor_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate cursor field')
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError(f'Invalid JSON constant: {value}')


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        try:
            self._initialize()
            self._enable_wal()
        except BaseException:
            self.db.close()
            raise

    def _enable_wal(self):
        # Concurrent first openers can both request the journal-mode lock;
        # SQLite may return BUSY immediately during that lock upgrade.
        deadline = time.monotonic() + 30
        while True:
            try:
                self.db.execute('PRAGMA journal_mode=WAL').fetchone()
                return
            except sqlite3.OperationalError as exc:
                if getattr(exc, 'sqlite_errorcode', 0) & 0xff != sqlite3.SQLITE_BUSY or time.monotonic() >= deadline:
                    raise
                time.sleep(.01)

    def _initialize(self):
        # Serialize version detection with migration so another opener cannot
        # observe or repeat a partially rebuilt schema.
        with self.transaction():
            version = self.db.execute('PRAGMA user_version').fetchone()[0]
            table_info = self.db.execute('PRAGMA table_info(entities)').fetchall()
            columns = [row[1] for row in table_info]
            if version not in (0, SCHEMA_VERSION):
                raise ValueError(f'Unsupported store schema version: {version}')
            if version == 0:
                if not columns:
                    self.db.execute('CREATE TABLE entities' + ENTITY_SCHEMA)
                elif columns == ['kind', 'id', 'body']:
                    declaration = [(row[1], row[2].upper(), row[3], row[5]) for row in table_info]
                    if declaration != [('kind', 'TEXT', 0, 1), ('id', 'TEXT', 0, 2), ('body', 'TEXT', 1, 0)]:
                        raise ValueError('Unsupported legacy entities schema')
                    self._migrate_entities()
                else:
                    raise ValueError('Unsupported legacy entities schema')
            elif columns != ['seq', 'kind', 'id', 'body', 'status', 'project_id']:
                raise ValueError('Invalid entities schema for version 2')
            statements = (
                'CREATE TABLE IF NOT EXISTS events(job_id TEXT,seq INTEGER,body TEXT NOT NULL,PRIMARY KEY(job_id,seq))',
                'CREATE TABLE IF NOT EXISTS audit(seq INTEGER PRIMARY KEY AUTOINCREMENT,body TEXT NOT NULL)',
                'CREATE TABLE IF NOT EXISTS idempotency(key TEXT PRIMARY KEY,digest TEXT NOT NULL,job_id TEXT NOT NULL)',
                'CREATE TABLE IF NOT EXISTS store_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)',
                'CREATE INDEX IF NOT EXISTS entities_kind_seq ON entities(kind,seq)',
                'CREATE INDEX IF NOT EXISTS entities_kind_status_seq ON entities(kind,status,seq)',
                'CREATE INDEX IF NOT EXISTS entities_kind_project_seq ON entities(kind,project_id,seq)',
                'CREATE INDEX IF NOT EXISTS entities_kind_project_status_seq ON entities(kind,project_id,status,seq)',
            )
            for statement in statements:
                self.db.execute(statement)
            self.db.execute('INSERT OR IGNORE INTO store_meta(key,value) VALUES(?,?)', ('instance_id', uuid.uuid4().hex))
            self.instance_id = self.db.execute('SELECT value FROM store_meta WHERE key=?', ('instance_id',)).fetchone()[0]
            if not isinstance(self.instance_id, str) or not re.fullmatch(r'[0-9a-f]{32}', self.instance_id):
                raise ValueError('Invalid store instance identity')
            self.db.execute('PRAGMA user_version=2')

    def _migrate_entities(self):
        self.db.execute('CREATE TABLE entities_schema2' + ENTITY_SCHEMA)
        source = self.db.execute('SELECT rowid,kind,id,body FROM entities ORDER BY rowid')
        while rows := source.fetchmany(500):
            migrated = []
            for sequence, kind, identity, raw in rows:
                _integer(sequence, 'legacy sequence', minimum=1)
                status, project_id = _projections(json.loads(raw))
                migrated.append((sequence, kind, identity, raw, status, project_id))
            self.db.executemany('INSERT INTO entities_schema2(seq,kind,id,body,status,project_id) VALUES(?,?,?,?,?,?)', migrated)
        source.close()
        self.db.execute('DROP TABLE entities')
        self.db.execute('ALTER TABLE entities_schema2 RENAME TO entities')

    @contextmanager
    def _write(self):
        if self.db.in_transaction:
            yield
        else:
            with self.db:
                yield

    @contextmanager
    def transaction(self):
        with self.lock:
            if self.db.in_transaction:
                yield
                return
            self.db.execute('BEGIN IMMEDIATE')
            try:
                yield
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    @contextmanager
    def read_transaction(self):
        """Keep related reads on one snapshot; reuse a caller's transaction."""
        with self.lock:
            if self.db.in_transaction:
                yield
                return
            self.db.execute('BEGIN')
            try:
                yield
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    def put(self, kind, identity, body):
        raw = encode(body)
        status, project_id = _projections(body)
        with self.lock, self._write():
            self.db.execute('''INSERT INTO entities(kind,id,body,status,project_id) VALUES(?,?,?,?,?)
                ON CONFLICT(kind,id) DO UPDATE SET body=excluded.body,status=excluded.status,project_id=excluded.project_id''',
                (kind, identity, raw, status, project_id))
        return body

    def get(self, kind, identity):
        with self.lock:
            row = self.db.execute('SELECT body FROM entities WHERE kind=? AND id=?',(kind,identity)).fetchone()
        return json.loads(row[0]) if row else None

    def list(self, kind, *, project_id=None):
        where, parameters, _ = _filters(kind, None, project_id)
        with self.lock:
            rows = self.db.execute('SELECT body FROM entities WHERE ' + where + ' ORDER BY seq', parameters).fetchall()
        return [json.loads(row[0]) for row in rows]

    def count(self, kind, *, status=None, project_id=None):
        where, parameters, _ = _filters(kind, status, project_id)
        with self.lock:
            return self.db.execute('SELECT COUNT(*) FROM entities WHERE ' + where, parameters).fetchone()[0]

    def status_counts(self, kind, *, project_id=None):
        """Count every status projection, including unknown strings and NULL."""
        where, parameters, _ = _filters(kind, None, project_id)
        with self.lock:
            rows = self.db.execute('SELECT status,COUNT(*) FROM entities WHERE ' + where + ' GROUP BY status', parameters).fetchall()
        return dict(rows)

    def first(self, kind, *, status=None, project_id=None):
        items = self.select(kind, status=status, project_id=project_id, limit=1)
        return items[0] if items else None

    def select(self, kind, *, status=None, project_id=None, limit=100):
        _integer(limit, 'limit', minimum=1, maximum=1000)
        where, parameters, _ = _filters(kind, status, project_id)
        with self.lock:
            rows = self.db.execute('SELECT body FROM entities WHERE ' + where + ' ORDER BY seq LIMIT ?', [*parameters, limit]).fetchall()
        return [json.loads(row[0]) for row in rows]

    def _decode_cursor(self, cursor, filters):
        if not isinstance(cursor, str) or not 1 <= len(cursor) <= 1024 or not re.fullmatch(r'[A-Za-z0-9_-]+', cursor):
            raise ValueError('Invalid page cursor')
        try:
            raw = base64.b64decode(cursor + '=' * (-len(cursor) % 4), altchars=b'-_', validate=True)
            value = json.loads(raw.decode('utf-8'), object_pairs_hook=_cursor_object, parse_constant=_invalid_constant)
        except (ValueError, UnicodeError, binascii.Error, RecursionError) as exc:
            raise ValueError('Invalid page cursor') from exc
        if not isinstance(value, dict) or set(value) != {'v', 'instance', 'kind', 'status', 'project_id', 'after', 'upper'}:
            raise ValueError('Invalid page cursor fields')
        if type(value['v']) is not int or value['v'] != 1 or value['instance'] != self.instance_id:
            raise ValueError('Page cursor version or instance mismatch')
        if any(value[key] != expected for key, expected in filters.items()):
            raise ValueError('Page cursor filters mismatch')
        after = _integer(value['after'], 'cursor after')
        upper = _integer(value['upper'], 'cursor upper')
        if after > upper:
            raise ValueError('Page cursor sequence bounds mismatch')
        return after, upper

    def _encode_cursor(self, filters, after, upper):
        value = {'v': 1, 'instance': self.instance_id, **filters, 'after': after, 'upper': upper}
        cursor = base64.urlsafe_b64encode(encode(value).encode('utf-8')).decode('ascii').rstrip('=')
        if len(cursor) > 1024:
            raise ValueError('Page cursor exceeds 1024 characters')
        return cursor

    def page(self, kind, *, status=None, project_id=None, limit=50, cursor=None):
        _integer(limit, 'limit', minimum=1, maximum=200)
        where, parameters, statuses = _filters(kind, status, project_id)
        filters = {'kind': kind, 'status': list(statuses) if statuses is not None else None, 'project_id': project_id}
        after, upper = self._decode_cursor(cursor, filters) if cursor is not None else (0, None)
        with self.read_transaction():
            if upper is None:
                upper = self.db.execute('SELECT COALESCE(MAX(seq),0) FROM entities WHERE kind=?', (kind,)).fetchone()[0]
            total = self.db.execute('SELECT COUNT(*) FROM entities WHERE ' + where + ' AND seq<=?', [*parameters, upper]).fetchone()[0]
            rows = self.db.execute('SELECT seq,body FROM entities WHERE ' + where + ' AND seq>? AND seq<=? ORDER BY seq LIMIT ?',
                [*parameters, after, upper, limit + 1]).fetchall()
        next_cursor = self._encode_cursor(filters, rows[limit - 1][0], upper) if len(rows) > limit else None
        return {'items': [json.loads(row[1]) for row in rows[:limit]], 'next_cursor': next_cursor, 'total': total}

    def append_event(self, job_id, body):
        with self.transaction():
            seq = self.db.execute('SELECT COALESCE(MAX(seq),0)+1 FROM events WHERE job_id=?',(job_id,)).fetchone()[0]
            body = dict(body,seq=seq,time=utcnow())
            self.db.execute('INSERT INTO events VALUES(?,?,?)',(job_id,seq,encode(body)))
        return body

    def event_max_seq(self, job_id):
        with self.lock:
            return self.db.execute('SELECT COALESCE(MAX(seq),0) FROM events WHERE job_id=?', (job_id,)).fetchone()[0]

    def events(self, job_id, after=0, *, limit=None, until=None, descending=False):
        _integer(after, 'after')
        if until is not None:
            _integer(until, 'until')
        if limit is not None:
            _integer(limit, 'limit', minimum=1, maximum=1001)
        if type(descending) is not bool:
            raise ValueError('descending must be a boolean')
        query = 'SELECT body FROM events WHERE job_id=? AND seq>?'
        parameters = [job_id, after]
        if until is not None:
            query += ' AND seq<=?'
            parameters.append(until)
        query += ' ORDER BY seq ' + ('DESC' if descending else 'ASC')
        if limit is not None:
            query += ' LIMIT ?'
            parameters.append(limit)
        with self.lock:
            rows = self.db.execute(query, parameters).fetchall()
        return [json.loads(row[0]) for row in rows]

    def audit(self, action=None, object_id=None, result='ok'):
        with self.lock:
            if action is None:
                return [json.loads(row[0]) for row in self.db.execute('SELECT body FROM audit ORDER BY seq DESC LIMIT 1000').fetchall()]
            with self._write():
                from .access import current_identity
                self.db.execute('INSERT INTO audit(body) VALUES(?)',(encode({'time':utcnow(),'actor':current_identity().subject,'action':action,'object_id':object_id,'result':result}),))

    def close(self):
        with self.lock:
            self.db.close()
