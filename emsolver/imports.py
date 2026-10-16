"""Persistent staged imports with isolated validation and atomic cancellation."""
from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from typing import Protocol
import uuid

import psutil

from .access import Identity, current_identity, identity_context
from .errors import ServiceError
from .integrity import file_hash
from .store import utcnow


TERMINAL = frozenset({'SUCCEEDED', 'FAILED', 'CANCELLED'})


class ImportGateway(Protocol):
    root: Path
    store: object
    _mutex: object

    def _project(self, project_id, writable=False): ...
    def _get(self, kind, item_id): ...
    def _public(self, item): ...
    def _validated_metadata(self, metadata): ...
    def _kill_tree(self, process): ...
    def _register_import_result(self, project_id, path, name, storage, metadata, diagnostic): ...


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Duplicate result field')
        value[key] = item
    return value


def _reject_constant(_):
    raise ValueError('Nonfinite result')


class ImportManager:
    MAX_INPUT_BYTES = 1024**3
    MAX_RESULT_BYTES = 1024**2
    TIMEOUT_SECONDS = 120
    RSS_LIMIT_BYTES = 2 * 1024**3
    MONITOR_INTERVAL_SECONDS = .1

    def __init__(self, gateway: ImportGateway, start_worker=False):
        self.gateway = gateway
        self.inbox = Path(gateway.root) / 'inbox'
        if self.inbox.is_symlink():
            raise ServiceError('UNSAFE_IMPORT_DIRECTORY')
        self.inbox.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.inbox.chmod(0o700)
        self._stop = threading.Event()
        self._run_lock = threading.Lock()
        self._process = None
        self._active_id = None
        self._thread = None
        if start_worker:
            self.recover()
            self._thread = threading.Thread(target=self._loop, name='emsolver-imports', daemon=True)
            self._thread.start()

    def _public(self, item):
        return copy.deepcopy(self.gateway._public(item))

    def _controlled_input(self, path):
        candidate = Path(path)
        if (candidate.is_symlink() or not candidate.is_file() or
                candidate.parent.resolve() != self.inbox.resolve()):
            return None
        return candidate

    def _controls(self, item):
        if not re.fullmatch(r'i_[0-9a-f]{32}', item.get('id', '')):
            return []
        return [self.inbox / (item['id'] + suffix) for suffix in ('.spec.json', '.result.json', '.result.json.tmp')]

    def _cleanup(self, item):
        paths = self._controls(item)
        candidate = self._controlled_input(item.get('_path', ''))
        if candidate and candidate.name.startswith(item['id'] + '.'):
            paths.append(candidate)
        for path in paths:
            try:
                # Unlinking the directory entry never follows a symlink.
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def submit(self, project_id, path, filename, name=None, storage='full', metadata=None):
        candidate = self._controlled_input(path)
        if candidate is None:
            raise ServiceError('UNSAFE_IMPORT_INPUT')
        item = None
        try:
            with self.gateway._mutex, self.gateway.store.transaction():
                if self._stop.is_set():
                    raise ServiceError('IMPORT_MANAGER_CLOSED', status=503)
                self.gateway._project(project_id, True)
                if (not isinstance(filename, str) or not filename or len(filename) > 255 or
                        Path(filename).name != filename or '/' in filename or '\\' in filename or
                        any(ord(char) < 32 for char in filename)):
                    raise ServiceError('UNSAFE_FILENAME')
                suffix = Path(filename).suffix.lower()
                if suffix not in {'.npz', '.npy'} or candidate.suffix.lower() != suffix:
                    raise ServiceError('UNSUPPORTED_FORMAT')
                if storage not in {'full', 'upper', 'lower'}:
                    raise ServiceError('INVALID_STORAGE')
                if name is not None and (not isinstance(name, str) or not 1 <= len(name.strip()) <= 200):
                    raise ServiceError('INVALID_NAME')
                metadata = self.gateway._validated_metadata(metadata) if metadata is not None else {}
                size = candidate.stat().st_size
                if size > self.MAX_INPUT_BYTES:
                    raise ServiceError('INPUT_TOO_LARGE', status=413)
                identifier = 'i_' + uuid.uuid4().hex
                destination = self.inbox / (identifier + suffix)
                candidate.replace(destination)
                candidate = destination
                candidate.chmod(0o600)
                identity = current_identity()
                item = {'id': identifier, 'project_id': project_id, 'status': 'QUEUED', 'stage': 'queued',
                        'filename': filename, 'name': name.strip() if name else filename, 'storage': storage,
                        'metadata': metadata, 'bytes_received': size, 'total_bytes': size,
                        'matrix_id': None, 'error': None, 'created_at': utcnow(),
                        '_path': str(candidate), '_input_sha256': file_hash(candidate),
                        '_identity': {'subject': identity.subject, 'global_admin': identity.global_admin, 'mode': identity.mode}}
                self.gateway.store.put('imports', identifier, item)
                self.gateway.store.audit('import.submit', identifier)
            return self._public(item)
        except BaseException:
            candidate.unlink(missing_ok=True)
            if item is not None:
                self._cleanup(item)
            raise

    def get(self, import_id):
        with self.gateway._mutex, self.gateway.store.transaction():
            return self._public(self.gateway._get('imports', import_id))

    def list(self, project_id=None):
        with self.gateway._mutex, self.gateway.store.transaction():
            if project_id is not None:
                self.gateway._project(project_id)
            result = []
            for item in self.gateway.store.list('imports', project_id=project_id):
                try:
                    self.gateway._project(item['project_id'])
                except ServiceError as exc:
                    if exc.code in {'FORBIDDEN', 'NOT_FOUND'}:
                        continue
                    raise
                result.append(self._public(item))
            return result

    def cancel(self, import_id):
        process = None
        with self.gateway._mutex, self.gateway.store.transaction():
            item = self.gateway._get('imports', import_id)
            self.gateway._project(item['project_id'], True)
            if item['status'] in TERMINAL:
                return self._public(item)
            if item['status'] == 'QUEUED':
                item.update(status='CANCELLED', stage='cancelled', finished_at=utcnow(),
                            error={'code': 'USER_CANCELLED'})
                self._cleanup(item)
            else:
                item.update(status='CANCELLING', stage='cancelling')
                if self._active_id == import_id:
                    process = self._process
            self.gateway.store.put('imports', import_id, item)
            self.gateway.store.audit('import.cancel', import_id)
        if process is not None:
            self.gateway._kill_tree(process)
        return self._public(item)

    def _finish(self, import_id, code):
        with self.gateway._mutex, self.gateway.store.transaction():
            item = self.gateway.store.get('imports', import_id)
            if item is None or item['status'] in TERMINAL:
                return
            cancelled = item['status'] == 'CANCELLING'
            item.update(status='CANCELLED' if cancelled else 'FAILED', stage='cancelled' if cancelled else 'failed',
                        error={'code': 'USER_CANCELLED' if cancelled else code}, finished_at=utcnow())
            self.gateway.store.put('imports', import_id, item)
            self.gateway.store.audit('import.finish', import_id, result=item['status'])

    def recover(self):
        """Only the runner owner calls recovery; secondary readers do not."""
        with self.gateway._mutex, self.gateway.store.transaction():
            for item in self.gateway.store.list('imports'):
                active = item.get('status') in {'RUNNING', 'CANCELLING'}
                missing = item.get('status') == 'QUEUED' and self._controlled_input(item.get('_path', '')) is None
                if not active and not missing:
                    continue
                if active and item.get('worker_pid'):
                    try:
                        process = psutil.Process(item['worker_pid'])
                        controls = self._controls(item)
                        if (controls and 'emsolver.import_worker' in process.cmdline() and str(controls[0]) in process.cmdline() and
                                item.get('_worker_create_time') is not None and
                                abs(process.create_time() - item['_worker_create_time']) < .01):
                            self.gateway._kill_tree(process)
                    except psutil.Error:
                        pass
                item.update(status='FAILED', stage='failed', error={'code': 'WORKER_LOST'}, finished_at=utcnow())
                self.gateway.store.put('imports', item['id'], item)
                self._cleanup(item)

    def _read_result(self, path):
        if path.is_symlink() or not path.is_file() or path.stat().st_size > self.MAX_RESULT_BYTES:
            raise ValueError('Invalid result file')
        with path.open('rb') as source:
            raw = source.read(self.MAX_RESULT_BYTES + 1)
        if len(raw) > self.MAX_RESULT_BYTES:
            raise ValueError('Oversized result')
        value = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
        if not isinstance(value, dict) or value.get('schema_version') != '1.0':
            raise ValueError('Invalid result schema')
        if value.get('status') == 'FAILED':
            if set(value) != {'schema_version', 'status', 'error_code'} or not re.fullmatch(r'[A-Z][A-Z_]{2,63}', value['error_code']):
                raise ValueError('Invalid error result')
            return value
        if set(value) != {'schema_version', 'status', 'input_sha256', 'diagnostic'} or value['status'] != 'SUCCEEDED':
            raise ValueError('Invalid success result')
        diagnostic = value['diagnostic']
        required = {'n', 'nnz', 'dtype', 'hermitian_error', 'norm_inf', 'storage_bytes', 'density', 'preview',
                    'diagonal_min', 'diagonal_max', 'row_nnz_min', 'row_nnz_max'}
        optional = {'is_diagonal', 'normalization', 'row_nnz_histogram'}
        if not isinstance(diagnostic, dict) or not required <= set(diagnostic) or set(diagnostic) - required - optional:
            raise ValueError('Invalid diagnostic schema')
        n = diagnostic['n']
        if type(n) is not int or n < 1 or diagnostic['dtype'] not in {'float64', 'complex128'}:
            raise ValueError('Invalid matrix shape or scalar')
        for key in ('nnz', 'storage_bytes', 'row_nnz_min', 'row_nnz_max'):
            if type(diagnostic[key]) is not int or diagnostic[key] < 0:
                raise ValueError('Invalid diagnostic count')
        for key in ('hermitian_error', 'norm_inf', 'density', 'diagonal_min', 'diagonal_max'):
            if type(diagnostic[key]) not in (int, float) or not math.isfinite(diagnostic[key]):
                raise ValueError('Invalid diagnostic scalar')
        preview = diagnostic['preview']
        if not isinstance(preview, list) or len(preview) > 1500:
            raise ValueError('Invalid preview budget')
        for point in preview:
            if (not isinstance(point, list) or len(point) != 3 or
                    any(type(index) is not int or not 0 <= index < n for index in point[:2]) or
                    type(point[2]) not in (int, float) or not math.isfinite(point[2]) or point[2] < 0):
                raise ValueError('Invalid preview element')
        def bounded_metadata(value, depth=0):
            if depth > 4:
                raise ValueError('Diagnostic metadata nesting exceeds budget')
            if isinstance(value, dict):
                if len(value) > 40 or any(not isinstance(key, str) or len(key) > 100 for key in value):
                    raise ValueError('Invalid diagnostic metadata keys')
                for child in value.values(): bounded_metadata(child, depth + 1)
            elif isinstance(value, list):
                if len(value) > 100: raise ValueError('Invalid diagnostic metadata list')
                for child in value: bounded_metadata(child, depth + 1)
            elif isinstance(value, str):
                if len(value) > 200 or value.startswith(('/', '\\')):
                    raise ValueError('Invalid diagnostic metadata string')
            elif type(value) in (int, float):
                if not math.isfinite(value): raise ValueError('Nonfinite diagnostic metadata')
            elif value is not None and type(value) is not bool:
                raise ValueError('Invalid diagnostic metadata value')
        for key in optional & set(diagnostic):
            bounded_metadata(diagnostic[key])
        return value

    def run_once(self):
        if self._stop.is_set() or not self._run_lock.acquire(blocking=False):
            return False
        item = None
        try:
            with self.gateway._mutex, self.gateway.store.transaction():
                item = self.gateway.store.first('imports', status='QUEUED')
                if item is None:
                    return False
                item.update(status='RUNNING', stage='validating', started_at=utcnow())
                self.gateway.store.put('imports', item['id'], item)
                self._active_id = item['id']
            try:
                self._run(item)
            except ServiceError as exc:
                self._finish(item['id'], exc.code)
            except (ValueError, TypeError, KeyError, OSError, UnicodeError, RecursionError, OverflowError):
                self._finish(item['id'], 'INVALID_IMPORT_RESULT')
            except Exception:
                # A registrar or optional dependency failure must not leave a
                # RUNNING row or stop the remaining queue. Never expose text.
                self._finish(item['id'], 'IMPORT_WORKER_ERROR')
            finally:
                if self._process is not None:
                    if self._process.poll() is None:
                        self.gateway._kill_tree(self._process)
                    try:
                        self._process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self._process.kill()
                        self._process.wait(timeout=5)
                with self.gateway._mutex:
                    self._process, self._active_id = None, None
                self._cleanup(item)
            return True
        finally:
            self._run_lock.release()

    def _run(self, item):
        path = self._controlled_input(item['_path'])
        if path is None or path.stat().st_size > self.MAX_INPUT_BYTES:
            raise ServiceError('WORKER_LOST')
        spec_path, result_path, _ = self._controls(item)
        spec = {'schema_version': '1.0', 'input_path': str(path), 'storage': item['storage'],
                'input_sha256': item['_input_sha256']}
        descriptor = os.open(spec_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
            json.dump(spec, output, allow_nan=False)
        from .process_environment import child_environment
        environment=child_environment(1)
        start = time.monotonic()
        with self.gateway._mutex, self.gateway.store.transaction():
            current = self.gateway.store.get('imports', item['id'])
            if current['status'] == 'CANCELLING':
                self._finish(item['id'], 'USER_CANCELLED')
                return
            self._process = subprocess.Popen([sys.executable, '-m', 'emsolver.import_worker', '--spec', str(spec_path),
                                              '--result', str(result_path)], env=environment,
                                             cwd=Path(__file__).resolve().parent.parent,
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            current['worker_pid'] = self._process.pid
            try:
                current['_worker_create_time'] = psutil.Process(self._process.pid).create_time()
            except psutil.Error:
                current['_worker_create_time'] = None
            self.gateway.store.put('imports', current['id'], current)
        while self._process.poll() is None:
            with self.gateway._mutex, self.gateway.store.transaction():
                current = self.gateway.store.get('imports', item['id'])
            code = None
            if current['status'] == 'CANCELLING': code = 'USER_CANCELLED'
            elif self._stop.is_set(): code = 'WORKER_LOST'
            elif time.monotonic() - start > self.TIMEOUT_SECONDS: code = 'IMPORT_TIMEOUT'
            else:
                try:
                    parent = psutil.Process(self._process.pid)
                    rss = 0
                    for process in [parent, *parent.children(recursive=True)]:
                        try: rss += process.memory_info().rss
                        except psutil.NoSuchProcess: pass
                    if rss > self.RSS_LIMIT_BYTES: code = 'IMPORT_MEMORY_LIMIT'
                except psutil.NoSuchProcess:
                    pass
                except psutil.Error:
                    code = 'IMPORT_MONITOR_FAILED'
            if code:
                self.gateway._kill_tree(self._process)
                self._finish(item['id'], code)
                return
            self._stop.wait(self.MONITOR_INTERVAL_SECONDS)
        if self._stop.is_set():
            raise ServiceError('WORKER_LOST')
        if time.monotonic() - start > self.TIMEOUT_SECONDS:
            raise ServiceError('IMPORT_TIMEOUT')
        result = self._read_result(result_path)
        if result['status'] == 'FAILED':
            raise ServiceError(result['error_code'])
        if (result['input_sha256'] != item['_input_sha256'] or self._controlled_input(path) is None or
                file_hash(path) != item['_input_sha256']):
            raise ServiceError('IMPORT_INPUT_CHANGED')
        with self.gateway._mutex, self.gateway.store.transaction():
            current = self.gateway.store.get('imports', item['id'])
            if current['status'] == 'CANCELLING':
                self._finish(item['id'], 'USER_CANCELLED')
                return
            if self._stop.is_set():
                raise ServiceError('WORKER_LOST')
            original = Identity(**item['_identity'])
            with identity_context(original):
                self.gateway._project(item['project_id'], True)
                matrix = self.gateway._register_import_result(item['project_id'], path, item['name'], item['storage'],
                                                               item['metadata'], result['diagnostic'])
            current.update(status='SUCCEEDED', stage='succeeded', matrix_id=matrix['id'], error=None,
                           finished_at=utcnow(), sha256=item['_input_sha256'])
            self.gateway.store.put('imports', item['id'], current)
            self.gateway.store.audit('import.finish', item['id'], result='SUCCEEDED')

    def _loop(self):
        while not self._stop.is_set():
            if not self.run_once():
                self._stop.wait(self.MONITOR_INTERVAL_SECONDS)

    def close(self):
        self._stop.set()
        with self.gateway._mutex:
            process = self._process
        if process is not None:
            self.gateway._kill_tree(process)
        if self._thread:
            self._thread.join(timeout=10)
