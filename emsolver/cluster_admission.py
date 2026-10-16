"""Nonblocking, cross-process admission before remote result body parsing.

One lock fences each job attempt, and a second lock reserves one of the bounded
control-plane upload/validation slots. PostgreSQL locks belong to a dedicated
session that holds no write transaction during validation. SQLite uses private
lockfiles and independent file descriptors, never its metadata write lock.

The API binds the acquired handle before reading multipart bodies. ContextVar
propagation into its threadpool lets the receiver reuse that trusted admission.
Direct receiver calls acquire the same guard themselves. Failed operations are
not queued: a busy worker receives 503 and retries its unchanged archive.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading

from .errors import ServiceError


_BOUND = ContextVar('emsolver_cluster_admission', default=None)
_NAMESPACE = 'emsolver.cluster.intake.v1'
_IDENTITY = re.compile(r'[A-Za-z0-9_-]{1,200}')


def _busy():
    return ServiceError('CLUSTER_INTAKE_BUSY', 'Result intake is busy; retry the same archive', 503)


def _unavailable():
    return ServiceError('CLUSTER_ADMISSION_UNAVAILABLE', 'Result intake admission is unavailable', 503)


def _slots():
    try:
        count = int(os.environ.get('EMSOLVER_CLUSTER_VALIDATION_SLOTS', '1'))
    except (TypeError, ValueError):
        raise ServiceError('CLUSTER_CONFIGURATION_INVALID', 'Validation slots must be an integer between 1 and 4', 503) from None
    if not 1 <= count <= 4:
        raise ServiceError('CLUSTER_CONFIGURATION_INVALID', 'Validation slots must be an integer between 1 and 4', 503)
    return count


def _identity(service, worker_id, job_id, attempt):
    if (not isinstance(worker_id, str) or not _IDENTITY.fullmatch(worker_id) or
            not isinstance(job_id, str) or not _IDENTITY.fullmatch(job_id) or
            type(attempt) is not int or not 1 <= attempt <= 3):
        raise ServiceError('INVALID_ADMISSION_ID', 'Valid worker, job and attempt identifiers required')
    return (id(service.store), worker_id, job_id, attempt)


def _lock_key(schema, *parts):
    # PostgreSQL advisory locks are already scoped to the current database.
    # Credentials, bearer tokens and connection URLs never enter the namespace.
    raw = json.dumps([_NAMESPACE, schema, *parts], ensure_ascii=True, separators=(',', ':')).encode('ascii')
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], 'big', signed=True)


class Admission:
    """An acquired attempt + global slot, released after the last bound user.

    close() is idempotent. If a cancelled HTTP request closes its handle while a
    propagated receiver binding is still running, release is deferred until that
    work exits; cancellation cannot silently free its validation resource slot.
    """
    def __init__(self, store, key, release):
        self._store = store
        self._key = key
        self._release = release
        self._mutex = threading.Lock()
        self._users = 0
        self._close_requested = False
        self._released = False

    def _matches(self, key):
        with self._mutex:
            return self._key == key and not self._close_requested and not self._released

    @contextmanager
    def bind(self):
        with self._mutex:
            if self._close_requested or self._released:
                raise _unavailable()
            self._users += 1
        token = _BOUND.set(self)
        try:
            yield self
        finally:
            _BOUND.reset(token)
            release = None
            with self._mutex:
                self._users -= 1
                if not self._users and self._close_requested and not self._released:
                    self._released = True
                    release = self._release
            if release is not None:
                release()

    def close(self):
        release = None
        with self._mutex:
            if self._close_requested:
                return
            self._close_requested = True
            if not self._users and not self._released:
                self._released = True
                release = self._release
        if release is not None:
            release()


def _postgres_acquire(service, job_id, attempt, count):
    connection = None
    try:
        # This session is intentionally independent of Store._connection():
        # session advisory locks on a reused session are reentrant and would
        # admit concurrent requests handled on the same event-loop thread.
        from .postgres_store import _driver
        psycopg = _driver()
        store = service.store
        connection = psycopg.connect(store._dsn, autocommit=True, connect_timeout=5)
        attempt_key = _lock_key(store.schema, 'attempt', job_id, attempt)
        if not connection.execute('SELECT pg_try_advisory_lock(%s)', (attempt_key,)).fetchone()[0]:
            raise _busy()
        for index in range(count):
            slot_key = _lock_key(store.schema, 'slot', index)
            if connection.execute('SELECT pg_try_advisory_lock(%s)', (slot_key,)).fetchone()[0]:
                # Closing this dedicated session releases both advisory locks.
                return connection.close
        raise _busy()
    except ServiceError:
        if connection is not None: connection.close()
        raise
    except Exception:
        if connection is not None: connection.close()
        # libpq errors can contain connection strings. Never forward them.
        raise _unavailable() from None


def _lock_directory(service):
    root = Path(service.root)
    info = root.lstat()
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or
            info.st_mode & 0o022 or (hasattr(os, 'getuid') and info.st_uid != os.getuid())):
        raise _unavailable()
    directory = root / '.cluster-admission'
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or
            info.st_mode & 0o077 or (hasattr(os, 'getuid') and info.st_uid != os.getuid())):
        raise _unavailable()
    return directory


def _try_file(directory, name):
    descriptor = os.open(directory / name, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or
                (hasattr(os, 'getuid') and info.st_uid != os.getuid())):
            raise _unavailable()
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN):
                os.close(descriptor)
                return None
            raise
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _release_files(descriptors):
    for descriptor in reversed(descriptors):
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(descriptor)
        except OSError:
            pass
    # Lockfiles must remain: unlinking an active lock inode permits bypass via
    # a newly created file of the same name in another process.


def _sqlite_acquire(service, job_id, attempt, count):
    descriptors = []
    try:
        directory = _lock_directory(service)
        name = hashlib.sha256(json.dumps([_NAMESPACE, job_id, attempt], separators=(',', ':')).encode()).hexdigest()
        descriptor = _try_file(directory, 'attempt-' + name + '.lock')
        if descriptor is None: raise _busy()
        descriptors.append(descriptor)
        for index in range(count):
            descriptor = _try_file(directory, 'slot-' + str(index) + '.lock')
            if descriptor is not None:
                descriptors.append(descriptor)
                return lambda: _release_files(descriptors)
        raise _busy()
    except ServiceError:
        _release_files(descriptors)
        raise
    except OSError:
        _release_files(descriptors)
        raise _unavailable() from None
    except BaseException:
        _release_files(descriptors)
        raise


def acquire_admission(service, worker_id, job_id, attempt):
    """Acquire before multipart parsing; return immediately with a guard or 503."""
    key = _identity(service, worker_id, job_id, attempt)
    count = _slots()
    dialect = getattr(service.store, 'dialect', 'sqlite')
    if dialect in {'postgresql', 'postgres'}:
        release = _postgres_acquire(service, job_id, attempt, count)
    elif dialect == 'sqlite':
        release = _sqlite_acquire(service, job_id, attempt, count)
    else:
        raise _unavailable()
    return Admission(service.store, key, release)


@contextmanager
def admission(service, worker_id, job_id, attempt):
    """Reuse a matching trusted API binding or guard a direct receiver call."""
    key = _identity(service, worker_id, job_id, attempt)
    current = _BOUND.get()
    if isinstance(current, Admission) and current._matches(key):
        with current.bind():
            yield current
        return
    handle = acquire_admission(service, worker_id, job_id, attempt)
    try:
        with handle.bind():
            yield handle
    finally:
        handle.close()
