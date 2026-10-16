"""Authenticated, lease-bound HTTP execution of frozen numerical jobs.

The controller owns credentials and network traffic. The existing numerical
worker receives only local files and the exact submitted solver configuration.
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from urllib.parse import quote, urljoin, urlsplit
import zipfile

from .contracts import resolve_config
from .process_environment import child_environment
from .resource_budgets import memory_budgets


class WorkerError(RuntimeError):
    """A stable code safe to report without paths, credentials or response text."""


class _TransientTransfer(Exception):
    """Retry the same operation without changing its input or completion bytes."""


def load_token(path):
    """Read an owned 0600 regular file without following a final symlink."""
    descriptor = None
    try:
        before = Path(path).lstat()
        if not stat.S_ISREG(before.st_mode) or stat.S_IMODE(before.st_mode) != 0o600:
            raise WorkerError('TOKEN_FILE_UNSAFE')
        if hasattr(os, 'getuid') and before.st_uid != os.getuid():
            raise WorkerError('TOKEN_FILE_UNSAFE')
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_mode, before.st_uid) != (
                after.st_dev, after.st_ino, after.st_mode, after.st_uid):
            raise WorkerError('TOKEN_FILE_UNSAFE')
        raw = os.read(descriptor, 4097)
        token = raw.decode('ascii').strip()
        if len(raw) > 4096 or not token or not re.fullmatch(r'[!-~]+', token):
            raise WorkerError('INVALID_TOKEN')
        return token
    except (OSError, UnicodeError) as exc:
        raise WorkerError('TOKEN_FILE_UNSAFE') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _origin(url):
    try:
        value = urlsplit(url)
        return value.scheme.lower(), value.hostname.lower(), value.port or (443 if value.scheme == 'https' else 80)
    except (ValueError, AttributeError):
        raise WorkerError('UNSAFE_SERVER_URL') from None


def validate_server_url(url, *, allow_insecure_http=False):
    try:
        value = urlsplit(url)
        if (value.scheme not in {'http', 'https'} or not value.hostname or value.username is not None or
                value.password is not None or value.query or value.fragment or value.path not in {'', '/'}):
            raise WorkerError('UNSAFE_SERVER_URL')
        _origin(url)  # Validate the port before any network request.
        local = value.hostname.lower() == 'localhost'
        try:
            local = local or ipaddress.ip_address(value.hostname).is_loopback
        except ValueError:
            pass
        if value.scheme == 'http' and not (local or allow_insecure_http):
            raise WorkerError('HTTPS_REQUIRED')
        return url.rstrip('/')
    except ValueError:
        raise WorkerError('UNSAFE_SERVER_URL') from None


def _expiry(value, *, lease_seconds=None, request_started=None):
    try:
        if lease_seconds is not None:
            if type(lease_seconds) not in (int, float) or not math.isfinite(lease_seconds) or not 0 <= lease_seconds <= 3600:
                raise ValueError
            # Starting at request dispatch subtracts the full round trip. It
            # also makes the lease independent of either host's wall clock.
            return (time.monotonic() if request_started is None else request_started) + lease_seconds
        epoch = float(value) if isinstance(value, (int, float)) else datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
        if not math.isfinite(epoch):
            raise ValueError
        return time.monotonic() + (epoch - time.time())
    except (ValueError, TypeError, AttributeError, OverflowError):
        raise WorkerError('INVALID_LEASE') from None


def _kill_tree(process):
    import psutil
    try:
        parent = psutil.Process(process.pid)
        processes = list(reversed(parent.children(recursive=True))) + [parent]
        for item in processes:
            try:
                item.terminate()
            except psutil.NoSuchProcess:
                pass
        # Popen must reap its own child; psutil.wait_procs(parent) can consume
        # the exit status first and make Popen subsequently report success.
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
        _, alive = psutil.wait_procs([item for item in processes if item.pid != process.pid], timeout=0.5)
        for item in alive:
            try:
                item.kill()
            except psutil.NoSuchProcess:
                pass
    except psutil.NoSuchProcess:
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


class _Lease:
    def __init__(self, worker, job_id, value, *, request_started=None):
        self.worker, self.job_id = worker, job_id
        self.token, self.attempt = value.get('token'), value.get('attempt')
        if (not isinstance(self.token, str) or not re.fullmatch(r'[!-~]+', self.token) or
                type(self.attempt) is not int or self.attempt < 1):
            raise WorkerError('INVALID_LEASE')
        self.deadline = _expiry(value.get('expires_at'), lease_seconds=value.get('lease_seconds'), request_started=request_started)
        self.done, self.lock = threading.Event(), threading.Lock()
        self.reason, self.process = None, None
        self.completion_started = False
        self.metrics = {}
        self.thread = threading.Thread(target=self._loop, daemon=True)

    def body(self):
        return {'lease_token': self.token, 'attempt': self.attempt}

    def headers(self):
        return {'X-EMSolver-Lease': self.token, 'X-EMSolver-Attempt': str(self.attempt)}

    def check(self):
        if self.reason:
            raise WorkerError(self.reason)
        if self.worker.stop_event.is_set():
            self.abort('WORKER_STOPPED')
            raise WorkerError('WORKER_STOPPED')
        if time.monotonic() >= self.deadline:
            self.abort('LEASE_EXPIRED')
            raise WorkerError('LEASE_EXPIRED')

    def abort(self, reason):
        with self.lock:
            if self.reason is not None:
                return
            self.reason = reason
            process = self.process
        if process is not None and process.poll() is None:
            _kill_tree(process)

    def _loop(self):
        next_beat = time.monotonic()
        while not self.done.is_set():
            try:
                self.check()
                now = time.monotonic()
                if now >= next_beat:
                    request_started = time.monotonic()
                    result = self.worker._request_json(
                        'POST', self.worker._job_route(self.job_id, 'heartbeat'),
                        {**self.body(), 'metrics': dict(self.metrics)}, lease=self)
                    # A response arriving after the previous deadline cannot revive work.
                    self.check()
                    if result.get('cancel_requested') is True:
                        self.abort('USER_CANCELLED')
                        return
                    deadline = _expiry(result.get('expires_at'), lease_seconds=result.get('lease_seconds'),
                                       request_started=request_started)
                    if deadline <= time.monotonic():
                        self.abort('LEASE_EXPIRED')
                        return
                    self.deadline = deadline
                    next_beat = time.monotonic() + min(self.worker.heartbeat_interval,
                                                       max(0.01, (deadline - time.monotonic()) / 3))
            except WorkerError as exc:
                if (str(exc) == 'LEASE_REVOKED' and getattr(exc, 'http_status', None) == 409 and
                        self.completion_started):
                    # A committed completion rejects further heartbeats. The
                    # upload may have lost its response, so permit only replay
                    # of its fixed archive within the last granted deadline.
                    return
                if str(exc) in {'LEASE_REVOKED', 'INVALID_LEASE', 'INVALID_SERVER_RESPONSE'}:
                    self.abort('LEASE_REVOKED')
                    return
                if self.reason:
                    return
                # Connection loss is tolerated only while the last granted lease lives.
                next_beat = time.monotonic() + min(0.5, self.worker.heartbeat_interval)
            self.done.wait(0.02)

    def close(self):
        self.done.set()
        if self.thread.is_alive():
            self.thread.join(timeout=6)


class _UploadFile:
    """Keep multipart file reads bounded and stop upload when a lease is lost."""
    def __init__(self, handle, lease):
        self.handle, self.lease = handle, lease

    def read(self, size=-1):
        self.lease.check()
        return self.handle.read(min(size, 1024 * 1024) if size >= 0 else 1024 * 1024)

    def seek(self, *args):
        return self.handle.seek(*args)

    def tell(self):
        return self.handle.tell()

    def fileno(self):
        return self.handle.fileno()


class RemoteWorker:
    def __init__(self, server, token, work_dir, capabilities=None, *, allow_insecure_http=False,
                 client=None, stop_event=None, heartbeat_interval=2.0, poll_interval=1.0):
        import httpx
        from .cluster_protocol import execution_fingerprint
        self.server = validate_server_url(server, allow_insecure_http=allow_insecure_http)
        if not isinstance(token, str) or not re.fullmatch(r'[!-~]+', token):
            raise WorkerError('INVALID_TOKEN')
        self._token = token
        self.work_dir = Path(work_dir)
        if self.work_dir.is_symlink():
            raise WorkerError('UNSAFE_WORK_DIRECTORY')
        self.work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.work_dir.chmod(0o700)
        self.stop_event = stop_event or threading.Event()
        if any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0
               for value in (heartbeat_interval, poll_interval)):
            raise WorkerError('INVALID_INTERVAL')
        self.heartbeat_interval, self.poll_interval = heartbeat_interval, poll_interval
        self._owns_client = client is None
        self.client = client or httpx.Client(follow_redirects=False, trust_env=False, timeout=5.0)
        import psutil
        defaults = {'threads': min(os.cpu_count() or 1, 256),
                    'memory_gib': psutil.virtual_memory().available / 1024**3,
                    'disk_gib': shutil.disk_usage(self.work_dir).free / 1024**3}
        selected = capabilities or {}
        for name in defaults:
            value = selected.get(name, defaults[name])
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise WorkerError('INVALID_CAPABILITIES')
            defaults[name] = min(value, defaults[name])
        defaults['threads'] = int(defaults['threads'])
        if defaults['threads'] < 1 or selected.get('backends', ['scipy']) != ['scipy'] or selected.get('devices', ['cpu']) != ['cpu']:
            raise WorkerError('UNSUPPORTED_BACKEND')
        self.capabilities = {**defaults, 'backends': ['scipy'], 'devices': ['cpu'],
                             'execution': execution_fingerprint()}

    @staticmethod
    def _job_route(job_id, action):
        return '/internal/v1/cluster/jobs/' + quote(job_id, safe='') + '/' + action

    def _url(self, value):
        if not isinstance(value, str) or '\\' in value or any(ord(c) < 33 for c in value):
            raise WorkerError('UNSAFE_DOWNLOAD_URL')
        url = urljoin(self.server + '/', value)
        parts = urlsplit(url)
        if (parts.username is not None or parts.password is not None or parts.fragment or
                _origin(url) != _origin(self.server)):
            raise WorkerError('UNSAFE_DOWNLOAD_URL')
        return url

    def _request_json(self, method, route, body=None, *, lease=None):
        import httpx
        if lease:
            lease.check()
        try:
            response = self.client.request(method, self._url(route), json=body,
                headers={'Authorization': 'Bearer ' + self._token}, follow_redirects=False,
                timeout=max(0.01, min(5., lease.deadline-time.monotonic())) if lease else 5.)
            if response.status_code in {401, 403, 404, 409, 410} and lease:
                error = WorkerError('LEASE_REVOKED')
                error.http_status = response.status_code
                raise error
            if response.status_code != 200:
                raise WorkerError('SERVER_UNAVAILABLE')
            if len(response.content) > 8 * 1024**2:
                raise WorkerError('INVALID_SERVER_RESPONSE')
            value = response.json()
            if not isinstance(value, dict):
                raise WorkerError('INVALID_SERVER_RESPONSE')
            return value
        except (httpx.HTTPError, ValueError):
            raise WorkerError('SERVER_UNAVAILABLE') from None

    def _download(self, description, target, lease, maximum):
        import httpx
        digest = description.get('sha256')
        size = description.get('bytes')
        if (not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest) or
                (size is not None and (type(size) is not int or not 0 < size <= maximum))):
            raise WorkerError('INVALID_INPUT_DESCRIPTOR')
        url = self._url(description.get('url'))
        delay = .1
        while True:
            lease.check()
            total, checksum, created = 0, hashlib.sha256(), False
            try:
                with self.client.stream('GET', url, headers={'Authorization': 'Bearer ' + self._token, **lease.headers()},
                        follow_redirects=False, timeout=max(.01, min(5., lease.deadline-time.monotonic()))) as response:
                    if response.status_code in {401, 403, 404, 409, 410}:
                        raise WorkerError('LEASE_REVOKED')
                    if 500 <= response.status_code <= 599:
                        raise _TransientTransfer()
                    if response.status_code != 200:
                        raise WorkerError('INPUT_DOWNLOAD_FAILED')
                    # Encoded bodies can amplify beyond the advertised byte count.
                    if response.headers.get('content-encoding', 'identity') != 'identity':
                        raise WorkerError('INVALID_INPUT_DESCRIPTOR')
                    with target.open('xb') as handle:
                        created = True
                        target.chmod(0o600)
                        for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                            lease.check()
                            total += len(chunk)
                            if total > maximum or (size is not None and total > size):
                                raise WorkerError('DOWNLOAD_TOO_LARGE')
                            checksum.update(chunk)
                            handle.write(chunk)
                lease.check()
                if (size is not None and total != size) or checksum.hexdigest() != digest:
                    raise WorkerError('INPUT_HASH_MISMATCH')
                return
            except (httpx.HTTPError, _TransientTransfer):
                if created:
                    target.unlink(missing_ok=True)
                self._retry_wait(lease, delay)
                delay = min(1., delay * 2)

    def _retry_wait(self, lease, delay):
        until = time.monotonic() + delay
        while True:
            lease.check()
            remaining = min(until - time.monotonic(), lease.deadline - time.monotonic())
            if remaining <= 0:
                return
            self.stop_event.wait(min(.02, remaining))

    def _budget(self, directory, maximum):
        from .optional_backends import private_directory_bytes
        size = private_directory_bytes(directory, maximum)
        if size > maximum:
            raise WorkerError('RESOURCE_EXHAUSTED')
        return size

    def _execute(self, assignment, directory, lease):
        import psutil
        job = assignment['job']
        config = job.get('config')
        try:
            if not isinstance(config, dict) or resolve_config(config) != config:
                raise ValueError
        except (ValueError, TypeError):
            raise WorkerError('INVALID_FROZEN_CONFIG') from None
        resources = config['resources']
        if config['solver']['backend'] != 'scipy' or resources['device'] != 'cpu':
            raise WorkerError('UNSUPPORTED_BACKEND')
        if any(resources[key] > self.capabilities[cap] for key, cap in
               [('threads', 'threads'), ('memory_limit_gib', 'memory_gib'), ('disk_limit_gib', 'disk_gib')]):
            raise WorkerError('INSUFFICIENT_CAPABILITIES')
        maximum = int(resources['disk_limit_gib'] * 1024**3)
        partitions = memory_budgets(config)
        description = assignment.get('input', {})
        filename = description.get('filename', '')
        if not isinstance(filename, str) or Path(filename).name != filename or Path(filename).suffix.lower() not in {'.npz', '.npy'}:
            raise WorkerError('INVALID_INPUT_DESCRIPTOR')
        source = directory / ('input' + Path(filename).suffix.lower())
        self._download(description, source, lease, maximum)
        specification = {'config': config, 'input_path': str(source), 'output_dir': str(directory / 'artifacts')}
        checkpoint = assignment.get('checkpoint')
        if checkpoint:
            if checkpoint.get('kind') not in {'exact_block', 'subspace'}:
                raise WorkerError('CHECKPOINT_INCOMPATIBLE')
            snapshot = directory / 'resume.npz'
            self._download(checkpoint, snapshot, lease, maximum - self._budget(directory, maximum))
            specification['resume_checkpoint_path' if checkpoint['kind'] == 'exact_block' else 'initial_subspace_path'] = str(snapshot)
        self._budget(directory, maximum)
        spec = directory / 'worker.spec.json'
        spec.write_text(json.dumps(specification, allow_nan=False), encoding='utf-8')
        spec.chmod(0o600)
        lease.check()
        process = subprocess.Popen([sys.executable, '-m', 'emsolver.worker', str(spec)],
            env=child_environment(resources['threads']), cwd=directory,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        with lease.lock:
            lease.process = process
        start, peak = time.monotonic(), 0
        try:
            while True:
                lease.check()
                try:
                    parent = psutil.Process(process.pid)
                    rss = sum(item.memory_info().rss for item in [parent] + parent.children(recursive=True) if item.is_running())
                    peak = max(peak, rss)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
                disk = self._budget(directory, maximum)
                elapsed = time.monotonic() - start
                lease.metrics = {'walltime_seconds': elapsed, 'elapsed_seconds': elapsed,
                                 'peak_rss_tree_bytes': peak, 'disk_bytes': disk}
                if elapsed > resources['walltime_seconds']:
                    raise WorkerError('WALLTIME_EXCEEDED')
                if peak > partitions['host_bytes']:
                    raise WorkerError('RESOURCE_EXHAUSTED')
                if process.poll() is not None:
                    break
                self.stop_event.wait(0.05)
            lease.check()
        finally:
            if process.poll() is None:
                _kill_tree(process)
            with lease.lock:
                lease.process = None
            spec.unlink(missing_ok=True)
        result_path = directory / 'worker.result.json'
        if result_path.is_symlink() or not result_path.is_file() or result_path.stat().st_size > min(maximum, 8 * 1024**2):
            raise WorkerError('INVALID_WORKER_RESULT')
        try:
            result = json.loads(result_path.read_text(encoding='utf-8'))
            if not isinstance(result, dict) or result.get('status') not in {'SUCCEEDED', 'PARTIAL', 'FAILED'}:
                raise ValueError
        except (OSError, ValueError, UnicodeError):
            raise WorkerError('INVALID_WORKER_RESULT') from None
        if process.returncode != 0 or result['status'] == 'FAILED':
            code = result.get('error_code')
            raise WorkerError(code if isinstance(code, str) and re.fullmatch(r'[A-Z][A-Z_]{3,100}', code) else 'WORKER_ERROR')
        result['metrics'] = {**result.get('metrics', {}), **lease.metrics, 'job_memory_partitions': partitions}
        result_path.write_text(json.dumps(result, allow_nan=False), encoding='utf-8')
        self._upload(directory, lease, maximum)

    def _upload(self, directory, lease, maximum):
        import httpx
        from .cluster_protocol import ARTIFACT_NAMES
        files = [(directory / 'worker.result.json', 'worker.result.json')]
        for name in sorted(ARTIFACT_NAMES):
            path = directory / 'artifacts' / name
            if path.exists():
                if path.is_symlink() or not path.is_file():
                    raise WorkerError('INVALID_WORKER_RESULT')
                files.append((path, name))
        if not {'manifest.json', 'eigenvalues.npy', 'eigenvectors.npy', 'residuals.npy'} <= {name for _, name in files}:
            raise WorkerError('INVALID_WORKER_RESULT')
        if self._budget(directory, maximum) + sum(path.stat().st_size + 1024 for path, _ in files) > maximum:
            raise WorkerError('RESOURCE_EXHAUSTED')
        bundle = directory / 'result.zip'
        with zipfile.ZipFile(bundle, 'w', compression=zipfile.ZIP_STORED) as archive:
            for path, name in files:
                with path.open('rb') as source, archive.open(name, 'w') as target:
                    while chunk := source.read(1024 * 1024):
                        lease.check()
                        target.write(chunk)
                self._budget(directory, maximum)
        lease.check()
        delay = .1
        lease.completion_started = True
        try:
            while True:
                lease.check()
                try:
                    # Reopen the existing ZIP at offset zero: rebuilding it
                    # would change the digest used for completion idempotency.
                    with bundle.open('rb') as handle:
                        response = self.client.post(self._url(self._job_route(lease.job_id, 'complete')),
                            headers={'Authorization': 'Bearer ' + self._token, **lease.headers()},
                            data={'lease_token': lease.token, 'attempt': str(lease.attempt)},
                            files={'bundle': ('result.zip', _UploadFile(handle, lease), 'application/zip')},
                            follow_redirects=False, timeout=max(.01, min(5., lease.deadline-time.monotonic())))
                    lease.check()
                    if response.status_code in {401, 403, 404, 409, 410}:
                        raise WorkerError('LEASE_REVOKED')
                    if 500 <= response.status_code <= 599:
                        raise _TransientTransfer()
                    if response.status_code != 200:
                        raise WorkerError('RESULT_UPLOAD_FAILED')
                    return
                except (httpx.HTTPError, _TransientTransfer):
                    self._retry_wait(lease, delay)
                    delay = min(1., delay * 2)
        finally:
            lease.completion_started = False

    def run_once(self):
        if self.stop_event.is_set():
            return False
        request_started = time.monotonic()
        assignment = self._request_json('POST', '/internal/v1/cluster/claim', {'capabilities': self.capabilities})
        job = assignment.get('job')
        if job is None:
            return False
        if not isinstance(job, dict) or not isinstance(job.get('id'), str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', job['id']):
            raise WorkerError('INVALID_SERVER_RESPONSE')
        lease = _Lease(self, job['id'], assignment.get('lease', {}), request_started=request_started)
        directory = None
        try:
            lease.check()
            directory = Path(tempfile.mkdtemp(prefix='attempt-', dir=self.work_dir)).resolve()
            lease.thread.start()
            self._execute(assignment, directory, lease)
        except WorkerError as exc:
            if str(exc) not in {'LEASE_EXPIRED', 'LEASE_REVOKED', 'USER_CANCELLED', 'WORKER_STOPPED'}:
                try:
                    self._request_json('POST', self._job_route(job['id'], 'failure'),
                                       {**lease.body(), 'code': str(exc)}, lease=lease)
                except WorkerError:
                    pass
        finally:
            lease.close()
            if directory is not None:
                shutil.rmtree(directory)
        return True

    def run(self, *, once=False):
        try:
            while not self.stop_event.is_set():
                try:
                    worked = self.run_once()
                except WorkerError:
                    if once:
                        raise
                    worked = False
                if once:
                    break
                if not worked:
                    self.stop_event.wait(self.poll_interval)
        finally:
            if self._owns_client:
                self.client.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Run a lease-bound EMSolver HTTP compute worker')
    parser.add_argument('--server', '--server-url', required=True)
    parser.add_argument('--token-file', required=True)
    parser.add_argument('--work-dir', required=True)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--allow-insecure-http', action='store_true')
    parser.add_argument('--threads', type=int)
    parser.add_argument('--memory-gib', type=float)
    parser.add_argument('--disk-gib', type=float)
    parser.add_argument('--heartbeat-interval', type=float, default=2.)
    parser.add_argument('--poll-interval', type=float, default=1.)
    args = parser.parse_args(argv)
    stop = threading.Event()
    previous = {}
    if threading.current_thread() is threading.main_thread():
        for number in (signal.SIGINT, signal.SIGTERM):
            previous[number] = signal.signal(number, lambda *_: stop.set())
    try:
        worker = RemoteWorker(args.server, load_token(args.token_file), args.work_dir,
            capabilities={key: value for key, value in {'threads': args.threads, 'memory_gib': args.memory_gib,
                'disk_gib': args.disk_gib}.items() if value is not None}, stop_event=stop,
            allow_insecure_http=args.allow_insecure_http, heartbeat_interval=args.heartbeat_interval,
            poll_interval=args.poll_interval)
        worker.run(once=args.once)
        return 0
    except (WorkerError, OSError) as exc:
        print(str(exc) if isinstance(exc, WorkerError) else 'WORKER_IO_ERROR', file=sys.stderr)
        return 1
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


if __name__ == '__main__':
    raise SystemExit(main())
