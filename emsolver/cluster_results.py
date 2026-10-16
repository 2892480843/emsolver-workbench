"""Bounded remote result intake and independently verified, fenced publication."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import tempfile
import uuid
import zipfile

from .cluster_protocol import ARTIFACT_NAMES, BUNDLE_NAMES
from .errors import ServiceError
from .integrity import file_hash
from .jobs import TERMINAL
from .validation_process import verify_bounded, _pairs, _invalid_constant, _strict_value


MAX_ARCHIVE_BYTES = 1024**3
MAX_RESULT_BYTES = 1024**2
_IDENTITY = re.compile(r'[A-Za-z0-9_-]{1,200}')
_DIGEST = re.compile(r'[a-f0-9]{64}')
_OBSERVATIONS = frozenset({'walltime_seconds', 'elapsed_seconds', 'peak_rss_tree_bytes',
                           'disk_bytes', 'peak_memory_bytes', 'solve_seconds',
                           'prepare_seconds', 'import_seconds', 'validation_seconds',
                           'export_seconds', 'threads_requested'})
_PUBLISHED_NAMES = frozenset({'manifest.json', 'config.resolved.yaml', 'validation.json',
                             'metrics.json', 'eigenvalues.npy', 'eigenvectors.npy', 'residuals.npy'})


def _error(code='INVALID_RESULT_ARCHIVE', status=400):
    return ServiceError(code, code, status)


def _token(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _safe_path(root, path, *, directory=False, missing=False):
    root, path = Path(root), Path(path)
    if not path.is_absolute() or not path.is_relative_to(root) or path != path.resolve():
        raise _error('UNSAFE_RESULT_PATH')
    relative = path.relative_to(root)
    for index in range(len(relative.parts) + 1):
        current = root.joinpath(*relative.parts[:index])
        try:
            info = current.lstat()
        except FileNotFoundError:
            if missing and index == len(relative.parts):
                return None
            raise _error('UNSAFE_RESULT_PATH') from None
        required_directory = index < len(relative.parts) or directory
        if (stat.S_ISLNK(info.st_mode) or (hasattr(os, 'getuid') and info.st_uid != os.getuid()) or
                info.st_mode & 0o022 or
                (not stat.S_ISDIR(info.st_mode) if required_directory else not stat.S_ISREG(info.st_mode))):
            raise _error('UNSAFE_RESULT_PATH')
    return _token(info)


def _json(path, *, code='INVALID_WORKER_RESULT'):
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_RESULT_BYTES:
            raise ValueError
        with path.open('rb') as stream:
            raw = stream.read(MAX_RESULT_BYTES + 1)
        if len(raw) > MAX_RESULT_BYTES:
            raise ValueError
        value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_invalid_constant)
        if not isinstance(value, dict):
            raise ValueError
        _strict_value(value)
        return value
    except (ValueError, UnicodeError, TypeError, RecursionError, OverflowError, OSError):
        raise _error(code) from None


def _write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True), encoding='utf-8')
    path.chmod(0o600)


def _archive_digest(stream):
    digest = hashlib.sha256()
    stream.seek(0)
    total = 0
    while block := stream.read(1024**2):
        total += len(block)
        if total > MAX_ARCHIVE_BYTES:
            raise _error('RESULT_ARCHIVE_TOO_LARGE', 413)
        digest.update(block)
    stream.seek(0)
    return digest.hexdigest()


def _check_directory_records(stream, size):
    """Bound central-directory allocation before ZipFile parses its entries.

    This protocol never needs ZIP64: both the archive and all decompressed
    content are limited to one GiB and at most the canonical artifact count.
    """
    stream.seek(max(0, size - (65535 + 22)))
    tail = stream.read(65535 + 22)
    offset = tail.rfind(b'PK\x05\x06')
    if offset < 0 or len(tail) - offset < 22:
        raise _error()
    _, disk, directory_disk, here, count, directory_size, directory_offset, comment = struct.unpack(
        '<4s4H2LH', tail[offset:offset + 22])
    position = size - len(tail) + offset
    if (disk or directory_disk or here != count or not 1 <= count <= len(BUNDLE_NAMES) or
            directory_size > MAX_RESULT_BYTES or directory_offset + directory_size != position or
            offset + 22 + comment != len(tail)):
        raise _error()
    stream.seek(0)


class ClusterResultReceiver:
    def __init__(self, service):
        self.service = service

    def _source(self, job):
        matrix = self.service.store.get('matrices', job.get('matrix_id'))
        if (not isinstance(matrix, dict) or matrix.get('id') != job.get('matrix_id') or
                not isinstance(job.get('matrix_hash'), str) or not _DIGEST.fullmatch(job['matrix_hash']) or
                matrix.get('hash') != job['matrix_hash']):
            raise _error('INPUT_HASH_MISMATCH', 409)
        try:
            source = Path(matrix['_path'])
            _safe_path(self.service.root, self.service.root / 'matrices', directory=True)
            if not source.is_relative_to(self.service.root / 'matrices'):
                raise _error('UNSAFE_RESULT_PATH')
            identity = _safe_path(self.service.root, source)
            if identity[4] > MAX_ARCHIVE_BYTES or identity[2] & 0o222:
                raise _error('UNSAFE_RESULT_PATH')
            if file_hash(source) != job['matrix_hash'] or _safe_path(self.service.root, source) != identity:
                raise _error('INPUT_HASH_MISMATCH', 409)
            return source, identity
        except (KeyError, TypeError, OSError):
            raise _error('INPUT_HASH_MISMATCH', 409) from None

    def _extract(self, stream, size, stage, maximum):
        _check_directory_records(stream, size)
        artifacts = stage / 'artifacts'
        artifacts.mkdir(mode=0o700)
        try:
            with zipfile.ZipFile(stream, 'r', allowZip64=False) as archive:
                entries = archive.infolist()
                names, total = set(), 0
                for item in entries:
                    name = item.filename
                    kind = stat.S_IFMT(item.external_attr >> 16)
                    if (name not in BUNDLE_NAMES or name in names or item.is_dir() or
                            kind not in {0, stat.S_IFREG} or item.flag_bits & 1 or
                            item.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED} or
                            item.file_size < 0 or item.compress_size < 0):
                        raise _error()
                    if name == 'worker.result.json' and item.file_size > MAX_RESULT_BYTES:
                        raise _error('INVALID_WORKER_RESULT')
                    names.add(name)
                    total += item.file_size
                    if total > maximum:
                        raise _error('RESULT_ARCHIVE_TOO_LARGE', 413)
                if 'worker.result.json' not in names:
                    raise _error('INVALID_WORKER_RESULT')
                written = 0
                for item in entries:
                    target = (stage if item.filename == 'worker.result.json' else artifacts) / item.filename
                    count = 0
                    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(descriptor, 'wb') as output, archive.open(item, 'r') as source:
                        while block := source.read(1024**2):
                            count += len(block)
                            written += len(block)
                            if count > item.file_size or written > maximum:
                                raise _error('RESULT_ARCHIVE_TOO_LARGE', 413)
                            output.write(block)
                    if count != item.file_size:
                        raise _error()
            return artifacts
        except (zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError, NotImplementedError, EOFError, ValueError):
            raise _error() from None

    def _canonical_result(self, job, worker_id, attempt, artifacts, payload):
        observations = {key: value for key, value in payload.get('metrics', {}).items()
                        if key in _OBSERVATIONS and type(value) in (int, float) and math.isfinite(value) and value >= 0}
        metrics = {'remote_worker': True, 'worker_id': worker_id, 'worker_attempt': attempt,
                   'remote_worker_observations': {'trusted': False, 'metrics': observations},
                   'verification_source': 'control_plane_independent'}
        status = payload.get('status')
        if not isinstance(status, str) or status not in {'SUCCEEDED', 'PARTIAL', 'FAILED'}:
            raise _error('INVALID_WORKER_RESULT')
        if status == 'FAILED':
            code = payload.get('error_code')
            code = code if isinstance(code, str) and re.fullmatch(r'[A-Z][A-Z_]{2,63}', code) else 'REMOTE_WORKER_FAILED'
            return {'status': 'FAILED', 'error_code': code, 'metrics': metrics}, False, None
        required = {'manifest.json', 'config.resolved.yaml', 'validation.json', 'metrics.json',
                    'eigenvalues.npy', 'eigenvectors.npy', 'residuals.npy'}
        actual = {path.name for path in artifacts.iterdir()}
        if not required <= actual:
            raise _error('INVALID_WORKER_RESULT')
        manifest = _json(artifacts / 'manifest.json')
        hashes = manifest.get('artifacts')
        if (not isinstance(hashes, dict) or set(hashes) != actual - {'manifest.json'} or
                any(name not in ARTIFACT_NAMES or name == 'manifest.json' or not isinstance(digest, str) or
                    not _DIGEST.fullmatch(digest) for name, digest in hashes.items())):
            raise _error('INVALID_WORKER_RESULT')
        source, source_identity = self._source(job)
        validation = verify_bounded(source, artifacts, job['config'])
        if file_hash(source) != job['matrix_hash'] or _safe_path(self.service.root, source) != source_identity:
            raise _error('INPUT_HASH_MISMATCH', 409)
        if validation is None:
            validation = {'numerical_status': 'FAILED', 'target_certification': 'UNVERIFIED',
                          'error_code': 'REMOTE_VERIFICATION_UNAVAILABLE'}
            code = 'REMOTE_VERIFICATION_UNAVAILABLE'
        else:
            code = 'REMOTE_VALIDATION_FAILED'
        if validation.get('numerical_status') != 'PASSED':
            return {'status': 'FAILED', 'error_code': code, 'validation': validation, 'metrics': metrics}, False, None
        import numpy as np
        try:
            values = np.load(artifacts / 'eigenvalues.npy', mmap_mode='r', allow_pickle=False)
            vectors = np.load(artifacts / 'eigenvectors.npy', mmap_mode='r', allow_pickle=False)
            if (not isinstance(values, np.ndarray) or values.ndim != 1 or len(values) > 512 or
                    not np.isfinite(values).all() or not isinstance(vectors, np.ndarray) or vectors.ndim != 2 or
                    vectors.shape[1] != len(values)):
                raise ValueError
            eigenvalues, n = values.tolist(), int(vectors.shape[0])
            del values, vectors
        except (OSError, ValueError, TypeError):
            raise _error('INVALID_WORKER_RESULT') from None
        target = job['config']['target']
        certified = validation.get('target_certification') == 'CERTIFIED'
        enough = len(eigenvalues) >= target['k'] if target['mode'] == 'nearest_k' else certified
        status = 'SUCCEEDED' if certified and enough else ('PARTIAL' if eigenvalues or certified else 'FAILED')
        code = None if status == 'SUCCEEDED' else ('TARGET_UNVERIFIED' if eigenvalues else 'NO_VALID_MODES')
        result = {'status': status, 'error_code': code, 'eigenvalues': eigenvalues,
                  'residuals': validation.get('residuals', []), 'validation': validation,
                  'metrics': metrics, 'n_accepted': len(eigenvalues), 'attempts': []}
        if status == 'FAILED':
            return result, False, None
        # Rebuild authoritative metadata; a remote metric or claimed validation
        # never becomes a control-plane measurement or certification.
        # Auxiliary exports, permutations and checkpoints were not checked by
        # verify_bounded. Hashing them cannot establish numerical correctness.
        # Publish only the verified arrays and metadata rebuilt below.
        suppressed = sorted(actual - _PUBLISHED_NAMES)
        for name in suppressed:
            (artifacts / name).unlink()
        import yaml
        (artifacts / 'config.resolved.yaml').write_text(yaml.safe_dump(job['config'], allow_unicode=True, sort_keys=True), encoding='utf-8')
        _write_json(artifacts / 'validation.json', validation)
        _write_json(artifacts / 'metrics.json', metrics)
        np.save(artifacts / 'residuals.npy', np.asarray(result['residuals'], dtype=np.float64), allow_pickle=False)
        hashes = {path.name: file_hash(path) for path in artifacts.iterdir() if path.name != 'manifest.json'}
        rebuilt = {'schema_version': '1.0', 'status': status, 'error_code': code,
                   'input_sha256': job['matrix_hash'], 'config_sha256': hashes['config.resolved.yaml'],
                   'artifacts': hashes, 'n': n, 'n_accepted': len(eigenvalues), 'requested_k': target.get('k'),
                   'numerical_status': validation['numerical_status'], 'target_certification': validation['target_certification'],
                   'attempts': [], 'worker_id': worker_id, 'worker_attempt': attempt,
                   'verification_source': 'control_plane_independent',
                   'suppressed_remote_artifacts': suppressed}
        _write_json(artifacts / 'manifest.json', rebuilt)
        return result, True, (source, source_identity)

    def complete(self, worker_id, job_id, lease_token, attempt, archive_path):
        from .cluster_admission import admission
        with admission(self.service, worker_id, job_id, attempt):
            return self._complete_admitted(worker_id, job_id, lease_token, attempt, archive_path)

    def _complete_admitted(self, worker_id, job_id, lease_token, attempt, archive_path):
        service = self.service
        if not isinstance(job_id, str) or not _IDENTITY.fullmatch(job_id):
            raise _error('INVALID_JOB_ID')
        job = service.cluster.require_lease(worker_id, job_id, lease_token, attempt, allow_completed=True)
        path = Path(archive_path)
        descriptor, stage = None, None
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_ARCHIVE_BYTES:
                raise _error('RESULT_ARCHIVE_TOO_LARGE' if info.st_size > MAX_ARCHIVE_BYTES else 'INVALID_RESULT_ARCHIVE', 413 if info.st_size > MAX_ARCHIVE_BYTES else 400)
            descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            with os.fdopen(descriptor, 'rb') as stream:
                descriptor = None
                before = _token(os.fstat(stream.fileno()))
                if before != _token(info):
                    raise _error()
                digest = _archive_digest(stream)
                if job['status'] in TERMINAL or job['status'] == 'CANCELLING' or job.get('cancel_requested'):
                    return service.cluster.finish(worker_id, job_id, lease_token, attempt, digest, lambda: None)
                maximum = min(MAX_ARCHIVE_BYTES, int(job['config']['resources']['disk_limit_gib'] * 1024**3))
                runs = service.root / 'runs'
                _safe_path(service.root, runs, directory=True)
                directory = runs / job_id
                _safe_path(service.root, directory, directory=True, missing=True)
                directory.mkdir(mode=0o700, exist_ok=True)
                stage = Path(tempfile.mkdtemp(prefix='.cluster-', dir=directory))
                artifacts = self._extract(stream, info.st_size, stage, maximum)
                if before != _token(os.fstat(stream.fileno())) or digest != _archive_digest(stream):
                    raise _error()
            payload = _json(stage / 'worker.result.json')
            if not isinstance(payload.get('metrics', {}), dict):
                raise _error('INVALID_WORKER_RESULT')
            result, publish, source_pin = self._canonical_result(job, worker_id, attempt, artifacts, payload)
            (stage / 'worker.result.json').unlink()
            if sum(item.stat().st_size for item in artifacts.iterdir()) > maximum:
                raise _error('RESULT_ARCHIVE_TOO_LARGE', 413)
            if not publish:
                # Service._finish accounts files in the run directory. Remove
                # rejected staging data before that authoritative accounting.
                shutil.rmtree(artifacts)
            destination = directory / 'artifacts'
            backup, installed = None, None

            def rollback():
                nonlocal installed, backup
                if installed is not None and destination.exists() and _token(destination.lstat())[:2] == installed:
                    shutil.rmtree(destination)
                    installed = None
                if backup is not None and backup.exists() and not destination.exists():
                    backup.rename(destination)
                    backup = None

            def publication():
                nonlocal installed, backup
                current = service.store.get('jobs', job_id)
                if not current or current['status'] in TERMINAL:
                    raise _error('COMPLETION_CONFLICT', 409)
                if any(current.get(key) != job.get(key) for key in ('config', 'matrix_id', 'matrix_hash')):
                    raise _error('COMPLETION_CONFLICT', 409)
                _safe_path(service.root, directory, directory=True)
                if publish:
                    source, identity = source_pin
                    matrix = service.store.get('matrices', job['matrix_id'])
                    if (not matrix or matrix.get('_path') != str(source) or matrix.get('hash') != job['matrix_hash'] or
                            _safe_path(service.root, source) != identity):
                        raise _error('INPUT_HASH_MISMATCH', 409)
                    _safe_path(service.root, destination, directory=True, missing=True)
                    if destination.exists():
                        backup = runs / ('.cluster-orphan-' + job_id + '-' + uuid.uuid4().hex)
                        destination.rename(backup)
                    artifacts.rename(destination)
                    installed = _token(destination.lstat())[:2]
                service._finish(job_id, result['status'], result.get('error_code'), result=result, metrics=result['metrics'])

            with service._mutex:
                try:
                    with service.store.transaction():
                        try:
                            finished = service.cluster.finish(worker_id, job_id, lease_token, attempt, digest, publication)
                        except BaseException:
                            rollback()
                            raise
                except BaseException:
                    rollback()
                    raise
                if backup is not None:
                    shutil.rmtree(backup)
            return finished
        except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
            raise _error('RESULT_IO_ERROR', 500) from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if stage is not None:
                shutil.rmtree(stage, ignore_errors=True)
