"""Immutable project references; array loading and comparison run in a child."""
from __future__ import annotations

from datetime import datetime
import json
import math
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile

import numpy as np
import psutil

from .access import require_project
from .errors import ServiceError
from .integrity import file_hash
from .process_environment import child_environment
from .store import utcnow
from .validation_process import _terminate


MAX_UPLOAD_BYTES = 128 * 1024**2
MAX_UNCOMPRESSED_BYTES = 128 * 1024**2
MAX_VECTOR_ENTRIES = 8_000_000
MAX_N = 2_000_000
MAX_MODES = 32
MAX_WORKER_BYTES = 65536
MAX_MEMORY_BYTES = 2 * 1024**3
MAX_WALLTIME_SECONDS = 30
KIND = 'industrial_reference_assets'
FIELDS = {'id', 'schema_version', 'project_id', 'sha256', 'bytes', 'n', 'modes', 'dtype',
          'eigenvalue_dtype', 'created_at', 'availability', '_path'}


def _invalid(message='Invalid or unbounded independent reference'):
    return ServiceError('INDUSTRIAL_REFERENCE_INVALID', message)


def _supported_scalar(dtype, *, eigenvalues=False):
    """All accepted scalars promote exactly to the evaluator's FP64 types."""
    return ((dtype.kind == 'f' and dtype.itemsize in (4, 8)) or
            (not eigenvalues and dtype.kind == 'c' and dtype.itemsize in (8, 16)))


def _array_header(source, total_bytes):
    """Read only the bounded NPY header, never materialize its array."""
    version = np.lib.format.read_magic(source)
    if version == (1, 0):
        shape, order, dtype = np.lib.format.read_array_header_1_0(source, max_header_size=16384)
    elif version == (2, 0):
        shape, order, dtype = np.lib.format.read_array_header_2_0(source, max_header_size=16384)
    else:
        raise ValueError('Unsupported NPY version')
    if (dtype.hasobject or dtype.fields is not None or dtype.subdtype is not None or
            dtype.kind not in 'fciu' or dtype.itemsize > 16 or len(shape) not in (1, 2) or
            any(type(size) is not int or size < 1 for size in shape)):
        raise ValueError('Unsafe NPY scalar or shape')
    entries = math.prod(shape)
    if entries > MAX_VECTOR_ENTRIES or source.tell() + entries * dtype.itemsize != total_bytes:
        raise ValueError('NPY payload size or entry budget')
    return {'shape': shape, 'dtype': str(dtype), 'kind': dtype.kind, 'bytes': total_bytes}


def _metadata(headers):
    values, vectors = headers['eigenvalues.npy'], headers['eigenvectors.npy']
    shape = vectors['shape']
    if (not _supported_scalar(np.dtype(values['dtype']), eigenvalues=True) or
            not _supported_scalar(np.dtype(vectors['dtype'])) or
            len(values['shape']) != 1 or len(shape) != 2 or
            not 1 <= shape[0] <= MAX_N or not 1 <= shape[1] <= MAX_MODES or values['shape'][0] != shape[1]):
        raise ValueError('Reference arrays must be matching real values and complete mode vectors')
    return {'n': shape[0], 'modes': shape[1], 'dtype': vectors['dtype'], 'eigenvalue_dtype': values['dtype']}


def header_metadata(path):
    """Preflight exact archive names, expanded sizes and NPY headers only."""
    path = Path(path)
    try:
        if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= MAX_UPLOAD_BYTES:
            raise ValueError('Reference upload budget')
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) != 2 or {member.filename for member in members} != {'eigenvalues.npy', 'eigenvectors.npy'}:
                raise ValueError('Exact reference members required')
            if sum(member.file_size for member in members) > MAX_UNCOMPRESSED_BYTES:
                raise ValueError('Reference expansion budget')
            headers = {}
            for member in members:
                mode = member.external_attr >> 16
                if (member.is_dir() or stat.S_ISLNK(mode) or member.flag_bits & 1 or
                        member.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED} or
                        not 0 < member.file_size <= MAX_UNCOMPRESSED_BYTES):
                    raise ValueError('Unsafe reference member')
                with archive.open(member) as source:
                    headers[member.filename] = _array_header(source, member.file_size)
            return _metadata(headers)
    except (OSError, ValueError, TypeError, EOFError, OverflowError, zipfile.BadZipFile, NotImplementedError) as exc:
        raise _invalid() from exc


def candidate_metadata(folder):
    """Apply the same bounded header policy to existing candidate NPY files."""
    folder = Path(folder)
    try:
        if folder.is_symlink() or not folder.is_dir(): raise ValueError('Unsafe result directory')
        headers = {}
        total = 0
        for name in ('eigenvalues.npy', 'eigenvectors.npy'):
            path = folder / name
            if path.is_symlink() or not path.is_file(): raise ValueError('Unsafe result file')
            size = path.stat().st_size
            total += size
            if total > MAX_UNCOMPRESSED_BYTES: raise ValueError('Candidate budget')
            with path.open('rb') as source: headers[name] = _array_header(source, size)
        return _metadata(headers)
    except (OSError, ValueError, TypeError, EOFError, OverflowError) as exc:
        raise _invalid() from exc


def validate_record(item, path=None):
    """Pure backup boundary: validate metadata/hash/headers, without array loads.

    The caller binds the project/entity references and supplies the staged path;
    its original absolute _path is accepted only by its content-addressed name.
    With path=None only strict metadata is checked, for allow-missing restores.
    """
    try:
        if (not isinstance(item, dict) or set(item) != FIELDS or item['schema_version'] != '1.0' or
                not isinstance(item['id'], str) or not re.fullmatch(r'ir_[0-9a-f]{32}', item['id']) or
                not isinstance(item['project_id'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', item['project_id']) or
                not isinstance(item['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', item['sha256']) or
                type(item['bytes']) is not int or not 0 < item['bytes'] <= MAX_UPLOAD_BYTES or
                type(item['n']) is not int or not 1 <= item['n'] <= MAX_N or
                type(item['modes']) is not int or not 1 <= item['modes'] <= MAX_MODES or
                item['n'] * item['modes'] > MAX_VECTOR_ENTRIES or
                item['availability'] not in {'READY', 'MISSING'} or
                not isinstance(item['created_at'], str) or len(item['created_at']) > 64 or
                datetime.fromisoformat(item['created_at']).tzinfo is None or
                not isinstance(item['_path'], str) or Path(item['_path']).name != item['sha256'] + '.npz'):
            raise ValueError('Invalid reference record')
        for key in ('dtype', 'eigenvalue_dtype'):
            if not isinstance(item[key], str) or len(item[key]) > 32: raise ValueError('Invalid scalar')
            dtype = np.dtype(item[key])
            if (dtype.hasobject or dtype.fields is not None or dtype.subdtype is not None or
                    not _supported_scalar(dtype, eigenvalues=key == 'eigenvalue_dtype') or str(dtype) != item[key]):
                raise ValueError('Invalid reference scalar')
        expected = {key: item[key] for key in ('n', 'modes', 'dtype', 'eigenvalue_dtype')}
        if path is None: return expected
        path = Path(path)
        if (path.is_symlink() or path.parent.is_symlink() or not path.is_file() or
                path.name != item['sha256'] + '.npz' or path.stat().st_size != item['bytes'] or
                file_hash(path) != item['sha256']):
            raise ValueError('Reference integrity failed')
        metadata = header_metadata(path)
        if any(item[key] != value for key, value in metadata.items()): raise ValueError('Changed reference metadata')
        return metadata
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
        raise _invalid('Independent reference record or payload failed integrity validation') from exc


class IndustrialReferenceAssets:
    def __init__(self, gateway):
        self.gateway = gateway
        self.folder = Path(gateway.root) / 'industrial'
        self.inbox = Path(gateway.root) / 'inbox'
        # All instances for one service share one worker admission slot.
        with gateway._mutex:
            if not hasattr(gateway, '_industrial_reference_lock'):
                gateway._industrial_reference_lock = threading.Lock()
            self._worker_lock = gateway._industrial_reference_lock

    def _project(self, project_id, write=False):
        project = self.gateway._project(project_id, writable=write)
        require_project(project, 'admin' if write else 'read')
        return project

    def record(self, asset_id):
        record = self.gateway._get(KIND, asset_id)
        self._project(record['project_id'])
        return record

    def path(self, asset_id):
        record = self.record(asset_id)
        if record.get('availability') == 'MISSING':
            raise ServiceError('INDUSTRIAL_REFERENCE_MISSING', status=409)
        path = self.folder / (record.get('sha256', '') + '.npz')
        if str(path) != record.get('_path') or self.folder.is_symlink(): raise _invalid()
        validate_record(record, path)
        return path

    def get(self, asset_id):
        record = self.record(asset_id)
        return self.gateway._public(record)

    def _worker(self, operation, source, *, candidate=None, criteria=None):
        if not self._worker_lock.acquire(blocking=False):
            raise ServiceError('INDUSTRIAL_REFERENCE_BUSY', status=409)
        try:
            return self._run_worker(operation, source, candidate=candidate, criteria=criteria)
        finally:
            self._worker_lock.release()

    def _run_worker(self, operation, source, *, candidate=None, criteria=None):
        process = None
        if self.inbox.is_symlink(): raise _invalid()
        with tempfile.TemporaryDirectory(prefix='industrial-ref-', dir=self.inbox) as temporary:
            folder = Path(temporary)
            spec, result = folder / 'spec.json', folder / 'result.json'
            value = {'operation': operation, 'source': str(source), 'sha256': file_hash(source),
                     'candidate': str(candidate) if candidate is not None else None, 'criteria': criteria}
            spec.write_text(json.dumps(value, allow_nan=False), encoding='utf-8')
            spec.chmod(0o600)
            deadline = time.monotonic() + MAX_WALLTIME_SECONDS
            try:
                process = subprocess.Popen([sys.executable, '-m', 'emsolver.industrial_reference_worker', str(spec), str(result)],
                    cwd=Path(__file__).resolve().parent.parent, env=child_environment(1),
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
                while process.poll() is None:
                    if time.monotonic() >= deadline:
                        raise ServiceError('INDUSTRIAL_REFERENCE_RESOURCE_LIMIT', status=409)
                    try:
                        parent = psutil.Process(process.pid)
                        rss = 0
                        for item in [parent, *parent.children(recursive=True)]:
                            try: rss += item.memory_info().rss
                            except psutil.NoSuchProcess: pass
                        if rss > MAX_MEMORY_BYTES: raise ServiceError('INDUSTRIAL_REFERENCE_RESOURCE_LIMIT', status=409)
                    except psutil.NoSuchProcess:
                        pass
                    if result.exists() and result.stat().st_size > MAX_WORKER_BYTES: raise _invalid()
                    time.sleep(.02)
                if (process.returncode != 0 or time.monotonic() >= deadline or result.is_symlink() or
                        not result.is_file() or result.stat().st_size > MAX_WORKER_BYTES): raise _invalid()
                report = json.loads(result.read_text(encoding='utf-8'), parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite')))
                if (not isinstance(report, dict) or set(report) != {'schema_version', 'sha256', 'operation', 'result'} or
                        report['schema_version'] != '1.0' or report['sha256'] != value['sha256'] or
                        report['operation'] != operation or file_hash(source) != value['sha256']): raise _invalid()
                return report['result']
            except (OSError, ValueError, TypeError, psutil.Error, subprocess.SubprocessError) as exc:
                raise _invalid() from exc
            finally:
                if process is not None and process.poll() is None: _terminate(process)

    def create(self, project_id, source_path):
        self._project(project_id, True)
        source = Path(source_path)
        if (self.inbox.is_symlink() or source.is_symlink() or not source.is_file() or
                source.parent.resolve() != self.inbox.resolve()): raise _invalid('Reference must use a controlled upload')
        metadata = header_metadata(source)
        digest = file_hash(source)
        validated = self._worker('validate', source)
        if validated != metadata or file_hash(source) != digest: raise _invalid()
        with self.gateway._mutex, self.gateway.store.transaction():
            self._project(project_id, True)
            same = self.gateway.store.db.execute(
                "SELECT id FROM entities WHERE kind=? AND project_id=? AND json_extract(body,'$.sha256')=? LIMIT 1",
                (KIND, project_id, digest)).fetchone()
            if same:
                self.path(same[0])
                return self.get(same[0])
            if self.folder.is_symlink(): raise _invalid()
            self.folder.mkdir(mode=0o700, exist_ok=True)
            path = self.folder / (digest + '.npz')
            size = source.stat().st_size
            self.gateway.governance.reserve(project_id, input_bytes=size)
            new = not path.exists()
            if path.is_symlink() or (not new and file_hash(path) != digest): raise _invalid()
            try:
                if new:
                    with source.open('rb') as original, path.open('xb') as output:
                        shutil.copyfileobj(original, output, length=1024**2)
                    path.chmod(0o400)
                record = {'id': 'ir_' + uuid.uuid4().hex, 'schema_version': '1.0', 'project_id': project_id,
                    'sha256': digest, 'bytes': size, **metadata, 'created_at': utcnow(), 'availability': 'READY', '_path': str(path)}
                validate_record(record, path)
                self.gateway.store.put(KIND, record['id'], record)
                self.gateway.governance.add_bytes(project_id, inputs=size)
                self.gateway.store.audit('industrial.reference.create', record['id'])
                return self.gateway._public(record)
            except BaseException:
                if new: path.unlink(missing_ok=True)
                raise

    def compare(self, asset_id, candidate_dir, criteria):
        source = self.path(asset_id)
        record = self.record(asset_id)
        candidate = Path(candidate_dir)
        metadata = candidate_metadata(candidate)
        if metadata['n'] != record['n']: raise _invalid('Reference and candidate dimensions differ')
        limits = {}
        for key in ('max_eigenvalue_absolute_error', 'max_eigenvalue_relative_error', 'max_subspace_sine'):
            value = criteria.get(key)
            if type(value) not in (float, int) or not math.isfinite(value) or value < 0: raise _invalid()
            limits[key] = value
        result = self._worker('compare', source, candidate=candidate.resolve(), criteria=limits)
        self.path(asset_id)
        if not isinstance(result, dict) or type(result.get('passed')) is not bool: raise _invalid()
        for key in ('candidate_eigenvalues', 'reference_eigenvalues'):
            if (not isinstance(result.get(key), list) or len(result[key]) > MAX_MODES or
                    any(type(value) not in (int, float) or not math.isfinite(value) for value in result[key])): raise _invalid()
        return result
