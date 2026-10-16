"""Durable remote-worker admission, attempts and fenced result publication.

All decisions that change ownership run in the store's write transaction. The
store supplies serialization across connections (SQLite writer lock or the
PostgreSQL transaction lock); an in-process mutex alone cannot fence workers.
"""
from __future__ import annotations

import copy
from contextlib import nullcontext
import hashlib
import hmac
import json
import math
import secrets
import uuid

from .access import require_admin
from .errors import ServiceError
from .jobs import TERMINAL, transition
from .store import encode, utcnow


_WORKER_COLUMNS = ('id', 'name', 'token_hash', 'project_ids', 'resources',
                   'backends', 'created_at', 'revoked_at', 'last_seen_at', 'capabilities')
_ATTEMPT_COLUMNS = ('job_id', 'attempt', 'worker_id', 'lease_hash', 'state',
                    'claimed_at', 'expires_at', 'last_heartbeat_at', 'finished_at',
                    'completion_digest', 'metrics')
_METRIC_FIELDS = frozenset({'cpu_percent', 'rss_bytes', 'peak_rss_tree_bytes',
                            'elapsed_seconds', 'disk_bytes', 'memory_bytes',
                            'peak_rss_bytes', 'worker_walltime_seconds'})


def _hash(secret):
    return hashlib.sha256(secret.encode('utf-8')).hexdigest()


def _number(value, *, positive=False, integer=False):
    if type(value) not in (int, float) or (integer and type(value) is not int):
        return False
    try:
        return math.isfinite(value) and (value > 0 if positive else value >= 0)
    except OverflowError:
        return False


def _strings(value, *, empty=False):
    return (isinstance(value, list) and (empty or bool(value))
            and all(isinstance(item, str) and 0 < len(item) <= 200 for item in value)
            and len(set(value)) == len(value))


class ClusterCoordinator:
    """Coordinate one active job per enrolled worker without shared filesystems.

    ``finish(..., callback)`` invokes the no-argument callback while the lease is
    valid and the write transaction is held. The callback must persist a terminal
    job; exceptions roll back both publication metadata and attempt completion.
    A matching completed digest returns that job without invoking the callback.
    """

    def __init__(self, service, *, lease_seconds=60, max_attempts=3):
        if not _number(lease_seconds, positive=True) or lease_seconds > 3600:
            raise ValueError('lease_seconds must be positive and at most 3600')
        if type(max_attempts) is not int or not 1 <= max_attempts <= 3:
            raise ValueError('max_attempts must be between 1 and 3')
        self.service = service
        self.store = service.store
        self.lease_seconds = float(lease_seconds)
        self.max_attempts = max_attempts
        self.initialize_schema()

    @property
    def _postgres(self):
        return getattr(self.store, 'dialect', 'sqlite') in {'postgresql', 'postgres'}

    def initialize_schema(self):
        """Create additive tables; existing stores and repeated openers are safe."""
        if not self._postgres:
            with self.store.lock:
                # SQLite ignores this pragma inside an existing transaction.
                if not self.store.db.in_transaction:
                    self.store.db.execute('PRAGMA foreign_keys=ON')
        statements = (
            '''CREATE TABLE IF NOT EXISTS cluster_workers(
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                token_hash TEXT NOT NULL UNIQUE,
                project_ids TEXT NOT NULL,
                resources TEXT NOT NULL,
                backends TEXT NOT NULL,
                created_at DOUBLE PRECISION NOT NULL,
                revoked_at DOUBLE PRECISION,
                last_seen_at DOUBLE PRECISION,
                capabilities TEXT
            )''',
            '''CREATE TABLE IF NOT EXISTS cluster_job_attempts(
                job_id TEXT NOT NULL,
                attempt INTEGER NOT NULL CHECK(attempt > 0),
                worker_id TEXT NOT NULL REFERENCES cluster_workers(id),
                job_kind TEXT NOT NULL DEFAULT 'jobs' CHECK(job_kind='jobs'),
                lease_hash TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('ACTIVE','EXPIRED','REVOKED','COMPLETED','CLOSED')),
                claimed_at DOUBLE PRECISION NOT NULL,
                expires_at DOUBLE PRECISION NOT NULL,
                last_heartbeat_at DOUBLE PRECISION NOT NULL,
                finished_at DOUBLE PRECISION,
                completion_digest TEXT,
                metrics TEXT NOT NULL,
                PRIMARY KEY(job_id,attempt),
                FOREIGN KEY(job_kind,job_id) REFERENCES entities(kind,id)
            )''',
            "CREATE UNIQUE INDEX IF NOT EXISTS cluster_one_active_worker ON cluster_job_attempts(worker_id) WHERE state='ACTIVE'",
            "CREATE UNIQUE INDEX IF NOT EXISTS cluster_one_active_job ON cluster_job_attempts(job_id) WHERE state='ACTIVE'",
            'CREATE INDEX IF NOT EXISTS cluster_attempt_expiry ON cluster_job_attempts(state,expires_at)',
        )
        with self.store.transaction():
            for statement in statements:
                self.store.db.execute(statement)

    def _now(self):
        query = ('SELECT EXTRACT(EPOCH FROM clock_timestamp())' if self._postgres else
                 "SELECT (julianday('now')-2440587.5)*86400.0")
        return float(self.store.db.execute(query).fetchone()[0])

    def _worker(self, worker_id, *, active=True):
        row = self.store.db.execute(
            'SELECT ' + ','.join(_WORKER_COLUMNS) + ' FROM cluster_workers WHERE id=?',
            (worker_id,)).fetchone()
        if not row:
            raise ServiceError('WORKER_UNAUTHORIZED', 'Valid worker credential required', 401)
        worker = dict(zip(_WORKER_COLUMNS, row))
        if active and worker['revoked_at'] is not None:
            raise ServiceError('WORKER_UNAUTHORIZED', 'Worker credential was revoked', 401)
        for name in ('project_ids', 'resources', 'backends'):
            worker[name] = json.loads(worker[name])
        worker['capabilities'] = json.loads(worker['capabilities']) if worker['capabilities'] else None
        return worker

    @staticmethod
    def _public_worker(worker):
        return copy.deepcopy({key: value for key, value in worker.items() if key != 'token_hash'})

    def _attempt(self, job_id, attempt):
        row = self.store.db.execute(
            'SELECT ' + ','.join(_ATTEMPT_COLUMNS) +
            ' FROM cluster_job_attempts WHERE job_id=? AND attempt=?', (job_id, attempt)).fetchone()
        if not row:
            raise ServiceError('LEASE_INVALID', 'Lease does not identify a known attempt', 409)
        result = dict(zip(_ATTEMPT_COLUMNS, row))
        result['metrics'] = json.loads(result['metrics'])
        return result

    @staticmethod
    def _public_attempt(attempt):
        return copy.deepcopy({key: value for key, value in attempt.items()
                              if key not in {'lease_hash', 'completion_digest'}})

    @staticmethod
    def _resources(value):
        if not isinstance(value, dict):
            raise ServiceError('INVALID_WORKER_RESOURCES')
        aliases = {'memory_limit_gib': 'memory_gib', 'disk_limit_gib': 'disk_gib'}
        result = {aliases.get(key, key): item for key, item in value.items()}
        if (set(result) - {'threads', 'memory_gib', 'disk_gib', 'mpi_ranks', 'devices'}
                or not _number(result.get('threads'), positive=True, integer=True)
                or not _number(result.get('memory_gib'), positive=True)
                or not _number(result.get('disk_gib'), positive=True)):
            raise ServiceError('INVALID_WORKER_RESOURCES')
        result.setdefault('mpi_ranks', 1)
        result.setdefault('devices', ['cpu'])
        if (not _number(result['mpi_ranks'], positive=True, integer=True)
                or not _strings(result['devices'])
                or set(result['devices']) - {'cpu', 'gpu'}):
            raise ServiceError('INVALID_WORKER_RESOURCES')
        return result

    def enroll(self, name, project_ids, resources=None, backends=None):
        require_admin()
        if (not isinstance(name, str) or not 1 <= len(name.strip()) <= 128
                or any(not character.isprintable() for character in name)):
            raise ServiceError('INVALID_WORKER_NAME')
        if (not _strings(project_ids) or len(project_ids) > 1000
                or ('*' in project_ids and project_ids != ['*'])):
            raise ServiceError('INVALID_WORKER_PROJECT_SCOPE')
        resources = self._resources(resources)
        if not _strings(backends) or set(backends) - {'scipy', 'slepc'}:
            raise ServiceError('INVALID_WORKER_BACKENDS')
        token = 'ew_' + secrets.token_urlsafe(48)
        worker_id = 'w_' + uuid.uuid4().hex
        with self.store.transaction():
            for project_id in project_ids:
                if project_id != '*' and not self.store.get('projects', project_id):
                    raise ServiceError('INVALID_WORKER_PROJECT_SCOPE', 'Unknown project in worker allowlist')
            now = self._now()
            self.store.db.execute(
                '''INSERT INTO cluster_workers(id,name,token_hash,project_ids,resources,backends,created_at)
                   VALUES(?,?,?,?,?,?,?)''',
                (worker_id, name.strip(), _hash(token), encode(project_ids), encode(resources), encode(backends), now))
            self.store.audit('worker.enroll', worker_id)
            worker = self._public_worker(self._worker(worker_id))
        return {'worker': worker, 'token': token}

    def authenticate(self, token):
        if not isinstance(token, str) or not 40 <= len(token) <= 512:
            raise ServiceError('WORKER_UNAUTHORIZED', 'Valid worker credential required', 401)
        hashed = _hash(token)
        with self.store.read_transaction():
            row = self.store.db.execute('SELECT id FROM cluster_workers WHERE token_hash=?', (hashed,)).fetchone()
            if not row:
                raise ServiceError('WORKER_UNAUTHORIZED', 'Valid worker credential required', 401)
            worker = self._worker(row[0])
            if not hmac.compare_digest(hashed, worker['token_hash']):
                raise ServiceError('WORKER_UNAUTHORIZED', 'Valid worker credential required', 401)
            return self._public_worker(worker)

    def workers(self):
        require_admin()
        with self.store.read_transaction():
            rows = self.store.db.execute('SELECT id FROM cluster_workers ORDER BY created_at,id').fetchall()
            result = []
            for row in rows:
                worker = self._public_worker(self._worker(row[0], active=False))
                active = self.store.db.execute(
                    "SELECT job_id,attempt,expires_at FROM cluster_job_attempts WHERE worker_id=? AND state='ACTIVE'",
                    (row[0],)).fetchone()
                worker['active_lease'] = ({'job_id': active[0], 'attempt': active[1], 'expires_at': active[2]}
                                          if active else None)
                result.append(worker)
            return result

    list_workers = workers

    def revoke(self, worker_id):
        require_admin()
        with self.store.transaction():
            worker = self._worker(worker_id, active=False)
            now = self._now()
            if worker['revoked_at'] is None:
                self.store.db.execute('UPDATE cluster_workers SET revoked_at=? WHERE id=?', (now, worker_id))
                rows = self.store.db.execute(
                    "SELECT job_id,attempt FROM cluster_job_attempts WHERE worker_id=? AND state='ACTIVE'",
                    (worker_id,)).fetchall()
                for job_id, attempt in rows:
                    self._retire(self._attempt(job_id, attempt), now, 'REVOKED', 'WORKER_REVOKED')
                self.store.audit('worker.revoke', worker_id)
            return self._public_worker(self._worker(worker_id, active=False))

    def _event(self, job_id, event):
        return self.service._event(job_id, event)

    def _terminal(self, job, status, code):
        updated = transition(job, status)
        updated['error'] = {'code': code, 'message': code.replace('_', ' ').capitalize()}
        updated['result'] = None
        self.store.put('jobs', job['id'], updated)
        self._event(job['id'], {'stage': updated['stage'], 'event': status, 'error_code': code})
        return updated

    def _retire(self, attempt, now, state, reason):
        self.store.db.execute(
            "UPDATE cluster_job_attempts SET state=?,finished_at=? WHERE job_id=? AND attempt=? AND state='ACTIVE'",
            (state, now, attempt['job_id'], attempt['attempt']))
        job = self.store.get('jobs', attempt['job_id'])
        self._event(attempt['job_id'], {'stage': 'scheduling', 'event': reason,
                                      'attempt': attempt['attempt'], 'worker_id': attempt['worker_id']})
        if not job or job['status'] in TERMINAL:
            return job
        if job['status'] == 'CANCELLING' or job.get('cancel_requested'):
            return self._terminal(job, 'CANCELLED', 'USER_CANCELLED')
        if attempt['attempt'] >= self.max_attempts:
            return self._terminal(job, 'FAILED', 'LEASE_RETRY_EXHAUSTED')
        # Retry is an internal recovery operation, never an ordinary public
        # transition from RUNNING to QUEUED; retain immutable numerical inputs.
        updated = copy.deepcopy(job)
        updated.update(status='QUEUED', stage='queued', result=None, error=None)
        for key in ('started_at', 'finished_at', 'worker_pid', 'worker_create_time'):
            updated.pop(key, None)
        updated['cluster'] = {'last_attempt': attempt['attempt'], 'retry_reason': reason}
        self.store.put('jobs', job['id'], updated)
        self._event(job['id'], {'stage': 'queued', 'event': 'QUEUED',
                               'attempt': attempt['attempt'], 'reason': reason})
        return updated

    def _reap(self, now):
        rows = self.store.db.execute(
            "SELECT job_id,attempt FROM cluster_job_attempts WHERE state='ACTIVE' AND expires_at<=? ORDER BY expires_at,job_id",
            (now,)).fetchall()
        result = []
        for job_id, attempt in rows:
            job = self._retire(self._attempt(job_id, attempt), now, 'EXPIRED', 'LEASE_EXPIRED')
            result.append({'job_id': job_id, 'attempt': attempt, 'status': job['status'] if job else None})
        return result

    def reap_expired(self):
        with self.store.transaction():
            return self._reap(self._now())

    @staticmethod
    def _capabilities(value):
        from .cluster_protocol import execution_fingerprint
        if not isinstance(value, dict) or value.get('execution') != execution_fingerprint():
            raise ServiceError('WORKER_EXECUTION_MISMATCH', 'Worker source or execution protocol differs', 409)
        if (not _number(value.get('threads'), integer=True)
                or not _number(value.get('memory_gib')) or not _number(value.get('disk_gib'))
                or not _strings(value.get('backends'), empty=True)
                or not _strings(value.get('devices'), empty=True)
                or not _number(value.get('mpi_ranks', 1), positive=True, integer=True)):
            raise ServiceError('INVALID_WORKER_CAPABILITIES')
        # Only these reviewed fields are persisted. Arbitrary runtime reports
        # must not copy credentials, host paths or business documents into metadata.
        return {key: copy.deepcopy(value[key]) for key in
                ('threads', 'memory_gib', 'disk_gib', 'backends', 'devices', 'execution')}

    def _eligible(self, job, worker, capabilities):
        if worker['project_ids'] != ['*'] and job.get('project_id') not in worker['project_ids']:
            return False
        project = self.store.get('projects', job.get('project_id'))
        if not project or project.get('archived'):
            return False
        config = job.get('config', {})
        resources, solver = config.get('resources', {}), config.get('solver', {})
        backend, device = solver.get('backend'), resources.get('device', 'cpu')
        if (backend not in worker['backends'] or backend not in capabilities['backends']
                or device not in worker['resources']['devices'] or device not in capabilities['devices']):
            return False
        for config_key, capacity_key in (('threads', 'threads'), ('memory_limit_gib', 'memory_gib'),
                                          ('disk_limit_gib', 'disk_gib')):
            required = resources.get(config_key)
            if not _number(required, positive=True) or required > min(
                    worker['resources'][capacity_key], capabilities[capacity_key]):
                return False
        ranks = resources.get('mpi_ranks', 1)
        if not _number(ranks, positive=True, integer=True) or ranks > min(
                worker['resources']['mpi_ranks'], capabilities.get('mpi_ranks', 1)):
            return False
        frozen_execution = job.get('execution', job.get('runtime', {}).get('execution'))
        return frozen_execution is None or frozen_execution == capabilities['execution']

    def claim(self, worker_id, capabilities=None):
        reported = self._capabilities(capabilities)
        # Preserve the optional MPI admission field without persisting arbitrary
        # keys from the worker's reported capabilities.
        reported['mpi_ranks'] = capabilities.get('mpi_ranks', 1)
        with self.store.transaction():
            worker = self._worker(worker_id)
            now = self._now()
            self._reap(now)
            self.store.db.execute('UPDATE cluster_workers SET last_seen_at=?,capabilities=? WHERE id=?',
                                  (now, encode(reported), worker_id))
            active = self.store.db.execute(
                "SELECT job_id FROM cluster_job_attempts WHERE worker_id=? AND state='ACTIVE'", (worker_id,)).fetchone()
            if active:
                return None
            rows = self.store.db.execute(
                "SELECT j.body FROM entities j LEFT JOIN entities p ON p.kind='projects' AND p.id=j.project_id "
                "WHERE j.kind='jobs' AND j.status='QUEUED' "
                "ORDER BY COALESCE(CAST(json_extract(p.body,'$.policy.priority') AS BIGINT),0) DESC,j.seq LIMIT 1000"
            ).fetchall()
            for row in rows:
                job = json.loads(row[0])
                if not self._eligible(job, worker, reported):
                    continue
                attempt = self.store.db.execute(
                    'SELECT COALESCE(MAX(attempt),0)+1 FROM cluster_job_attempts WHERE job_id=?', (job['id'],)).fetchone()[0]
                if attempt > self.max_attempts:
                    self._terminal(job, 'FAILED', 'LEASE_RETRY_EXHAUSTED')
                    continue
                token = secrets.token_urlsafe(48)
                expires = now + self.lease_seconds
                self.store.db.execute(
                    '''INSERT INTO cluster_job_attempts(job_id,attempt,worker_id,lease_hash,state,
                       claimed_at,expires_at,last_heartbeat_at,metrics) VALUES(?,?,?,?,?,?,?,?,?)''',
                    (job['id'], attempt, worker_id, _hash(token), 'ACTIVE', now, expires, now, '{}'))
                updated = transition(job, 'RUNNING')
                updated['started_at'] = utcnow()
                updated['cluster'] = {'worker_id': worker_id, 'attempt': attempt, 'expires_at': expires}
                self.store.put('jobs', job['id'], updated)
                self._event(job['id'], {'stage': 'running', 'event': 'RUNNING',
                                        'worker_id': worker_id, 'attempt': attempt})
                self.store.audit('job.claim', job['id'])
                return {'job': updated, 'job_id': job['id'], 'worker_id': worker_id,
                        'attempt': attempt, 'lease_token': token, 'expires_at': expires}
            return None

    def _validate(self, worker_id, job_id, lease_token, attempt, now, *, allow_completed=False):
        self._worker(worker_id)
        if (not isinstance(lease_token, str) or not 40 <= len(lease_token) <= 512
                or type(attempt) is not int or attempt < 1):
            raise ServiceError('LEASE_INVALID', 'Valid attempt and lease credential required', 409)
        lease = self._attempt(job_id, attempt)
        if lease['worker_id'] != worker_id or not hmac.compare_digest(lease['lease_hash'], _hash(lease_token)):
            raise ServiceError('LEASE_INVALID', 'Lease credential does not match this worker and attempt', 409)
        job = self.store.get('jobs', job_id)
        if not job:
            raise ServiceError('LEASE_INVALID', 'Lease job no longer exists', 409)
        if allow_completed and lease['state'] == 'COMPLETED':
            return lease, job
        if lease['state'] != 'ACTIVE' or lease['expires_at'] <= now or job['status'] in TERMINAL:
            raise ServiceError('LEASE_EXPIRED', 'Lease is no longer active', 409)
        return lease, job

    def require_lease(self, worker_id, job_id, lease_token, attempt, *, allow_completed=False):
        with self.store.transaction():
            _, job = self._validate(worker_id, job_id, lease_token, attempt, self._now(),
                                    allow_completed=allow_completed)
            return job

    def heartbeat(self, worker_id, job_id, lease_token, attempt, metrics=None):
        if metrics is not None and not isinstance(metrics, dict):
            raise ServiceError('INVALID_WORKER_METRICS')
        sanitized = {key: value for key, value in (metrics or {}).items()
                     if key in _METRIC_FIELDS and _number(value)}
        with self.store.transaction():
            now = self._now()
            lease, job = self._validate(worker_id, job_id, lease_token, attempt, now)
            expires = now + self.lease_seconds
            self.store.db.execute(
                '''UPDATE cluster_job_attempts SET expires_at=?,last_heartbeat_at=?,metrics=?
                   WHERE job_id=? AND attempt=?''',
                (expires, now, encode({**lease['metrics'], **sanitized}), job_id, attempt))
            self.store.db.execute('UPDATE cluster_workers SET last_seen_at=? WHERE id=?', (now, worker_id))
            job['cluster'] = {'worker_id': worker_id, 'attempt': attempt, 'expires_at': expires}
            self.store.put('jobs', job_id, job)
            return {'job_id': job_id, 'attempt': attempt, 'expires_at': expires,
                    'cancel_requested': job['status'] == 'CANCELLING' or bool(job.get('cancel_requested'))}

    def finish(self, worker_id, job_id, lease_token, attempt, completion_digest, callback):
        if (not isinstance(completion_digest, str) or len(completion_digest) != 64
                or any(character not in '0123456789abcdef' for character in completion_digest)):
            raise ServiceError('INVALID_COMPLETION_DIGEST')
        if not callable(callback):
            raise ValueError('Completion callback must be callable')
        # Service cancellation takes the service mutex before the store lock.
        # Acquire that same order before calling back into Service._finish.
        with getattr(self.service, '_mutex', nullcontext()), self.store.transaction():
            now = self._now()
            lease, job = self._validate(worker_id, job_id, lease_token, attempt, now, allow_completed=True)
            if lease['state'] == 'COMPLETED':
                if not hmac.compare_digest(lease['completion_digest'] or '', completion_digest):
                    raise ServiceError('COMPLETION_CONFLICT', 'Completed attempt has a different result digest', 409)
                return job
            if job['status'] == 'CANCELLING' or job.get('cancel_requested'):
                finished = self._terminal(job, 'CANCELLED', 'USER_CANCELLED')
            else:
                callback()
                finished = self.store.get('jobs', job_id)
                if not finished or finished['status'] not in TERMINAL:
                    raise ServiceError('INVALID_COMPLETION', 'Completion callback must publish a terminal job', 409)
                # The transaction blocks replacement claims, but wall time can
                # still pass during publication. A slow callback cannot revive
                # an already expired lease when it finally returns.
                if self._now() >= lease['expires_at']:
                    raise ServiceError('LEASE_EXPIRED', 'Lease expired during completion publication', 409)
            self.store.db.execute(
                "UPDATE cluster_job_attempts SET state='COMPLETED',completion_digest=?,finished_at=? WHERE job_id=? AND attempt=?",
                (completion_digest, self._now(), job_id, attempt))
            self.store.audit('job.remote.complete', job_id)
            return finished

    def lease_status(self, job_id):
        with self.store.read_transaction():
            rows = self.store.db.execute(
                'SELECT attempt FROM cluster_job_attempts WHERE job_id=? ORDER BY attempt', (job_id,)).fetchall()
            attempts = [self._public_attempt(self._attempt(job_id, row[0])) for row in rows]
            active = next((value for value in reversed(attempts) if value['state'] == 'ACTIVE'), None)
            return {'job_id': job_id, 'active': active, 'attempts': attempts}
