#!/usr/bin/env python3
"""Verify an installed EMSolver instance using only standard-library clients.

Run inside the final image with --network=none for an OS-enforced offline proof.
This program itself requests only 127.0.0.1, ignores proxy settings and refuses
redirects. It never installs dependencies or assumes a repository/venv path.
"""
from __future__ import annotations

import argparse
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import secrets
import socket
import stat
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.parse import urlsplit, parse_qsl
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import uuid
import zipfile


EXPECTED_VERSION = '0.9.0'
MAX_RESPONSE_BYTES = 64 * 1024**2
TERMINAL = {'SUCCEEDED', 'PARTIAL', 'FAILED', 'CANCELLED', 'TIMED_OUT'}
PRIVATE_KEYS = {'actor', 'subject', 'members', 'user', 'username', 'email', 'identity',
                'configured_subjects', 'authorization', 'password', 'secret', 'token',
                'api_key', 'access_token', 'input_path', 'output_dir', 'matrix_path', 'filepath'}
PRIVATE_PATH = re.compile(r'(?:/(?:Users|home|private|tmp|var|srv|mnt)/[^\s"\'<>;,}]+|(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s"\'<>;,}]+)')


class VerificationError(RuntimeError):
    pass


def require(condition, code):
    if not condition:
        raise VerificationError(code)


def sha256(value):
    return hashlib.sha256(value).hexdigest()


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        raise VerificationError('UNEXPECTED_HTTP_REDIRECT')


class LoopbackClient:
    def __init__(self, port, token):
        self.origin = f'http://127.0.0.1:{port}'
        self.token = token
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(self, path, body=None, *, method=None, binary=False, content_type=None):
        require(path.startswith('/') and not path.startswith('//') and '\\' not in path,
                'NONLOCAL_REQUEST_REJECTED')
        if body is not None and not isinstance(body, bytes):
            body = json.dumps(body, allow_nan=False).encode('utf-8')
            content_type = 'application/json'
        headers = {'Authorization': 'Bearer ' + self.token}
        if content_type:
            headers['Content-Type'] = content_type
        request = Request(self.origin + path, data=body, method=method or ('POST' if body is not None else 'GET'), headers=headers)
        with self.opener.open(request, timeout=5) as response:
            data = response.read(MAX_RESPONSE_BYTES + 1)
            require(len(data) <= MAX_RESPONSE_BYTES, 'HTTP_RESPONSE_TOO_LARGE')
            return data if binary else json.loads(data)


def free_port(preferred=0):
    with socket.socket() as listener:
        try:
            listener.bind(('127.0.0.1', preferred))
        except OSError:
            raise VerificationError('PORT_UNAVAILABLE') from None
        return listener.getsockname()[1]


class OwnedServer:
    def __init__(self, root, data, environment, token, port, role):
        self.process = None
        self.log = None
        self.root, self.data, self.environment = root, data, environment
        self.port = free_port(port)
        self.client = LoopbackClient(self.port, token)
        self.role = role

    def start(self):
        self.log = (self.root / (self.role + '-server.log')).open('wb')
        command = [sys.executable, '-m', 'emsolver', '--data-dir', str(self.data),
                   'serve', '--host', '127.0.0.1', '--port', str(self.port)]
        self.process = subprocess.Popen(command, cwd=self.root, env=self.environment,
                                        stdin=subprocess.DEVNULL, stdout=self.log, stderr=self.log)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            require(self.process.poll() is None, 'SERVER_EXITED_BEFORE_READY')
            # Wait for our child's successful bind before touching HTTP. A
            # listener winning the port race must never become the test target.
            ready_line = f'Uvicorn running on http://127.0.0.1:{self.port}'
            if ready_line not in (self.root / (self.role + '-server.log')).read_text(encoding='utf-8', errors='replace'):
                time.sleep(.05)
                continue
            try:
                health = self.client.request('/api/v1/health')
                if health.get('service_alive'):
                    require(health.get('version') == EXPECTED_VERSION, 'VERSION_MISMATCH')
                    require(health.get('backend_runnable') and health.get('queue_available') and health.get('storage_writable'),
                            'SERVICE_NOT_READY')
                    return health
            except (URLError, TimeoutError, ConnectionError):
                pass
            time.sleep(.05)
        raise VerificationError('SERVER_START_TIMEOUT')

    def stop(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.log is not None:
            self.log.close()

    @property
    def stopped(self):
        return self.process is None or self.process.poll() is not None


class AssetParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.assets = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'script' and attrs.get('src'):
            self.assets.append(attrs['src'])
        if tag == 'link' and attrs.get('rel') == 'stylesheet' and attrs.get('href'):
            self.assets.append(attrs['href'])


def verify_frontend(client):
    index = client.request('/', binary=True)
    require(b'<html' in index.lower(), 'FRONTEND_NOT_BUILT')
    parser = AssetParser()
    parser.feed(index.decode('utf-8'))
    require(any(path.endswith('.js') for path in parser.assets) and any(path.endswith('.css') for path in parser.assets),
            'FRONTEND_ASSETS_MISSING')
    assets = []
    for path in sorted(set(parser.assets)):
        require(re.fullmatch(r'/assets/[A-Za-z0-9_.-]+-[A-Za-z0-9_-]+\.(?:js|css)', path), 'ASSET_NOT_HASHED_OR_LOCAL')
        content = client.request(path, binary=True)
        require(content and not content.lstrip().lower().startswith(b'<!doctype html'), 'ASSET_SPA_FALLBACK')
        assets.append({'name': path.removeprefix('/'), 'bytes': len(content), 'sha256': sha256(content)})
    return {'index_sha256': sha256(index), 'assets': assets, 'scope': 'Actual served index and referenced built assets; no browser interaction is claimed'}


def scan_public_bundle(path, token, work_root):
    checked = 0
    def visit(value):
        if isinstance(value, dict):
            for key, child in value.items():
                require(key.lower() not in PRIVATE_KEYS and not any(word in key.lower() for word in ('password', 'secret', 'token')),
                        'PUBLIC_PRIVATE_FIELD')
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, str):
            parsed = urlsplit(value)
            if parsed.scheme in {'http', 'https'} and parsed.netloc:
                require(parsed.username is None and parsed.password is None and not parsed.fragment, 'PUBLIC_URL_CREDENTIAL')
                for key, item in parse_qsl(parsed.query):
                    normalized = re.sub(r'[^a-z0-9]', '', key.lower())
                    sensitive = normalized in {'key', 'auth', 'sig', 'code'} or any(word in normalized for word in ('token', 'secret', 'password', 'credential', 'signature', 'authorization', 'apikey', 'accesskey', 'sessionid'))
                    require(not sensitive or item == '[redacted]', 'PUBLIC_URL_CREDENTIAL')
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        names = [entry.filename for entry in entries]
        require(len(names) == len(set(names)) and 'checksums.json' in names, 'PUBLIC_ARCHIVE_MANIFEST')
        require(sum(entry.file_size for entry in entries) <= 128 * 1024**2, 'PUBLIC_ARCHIVE_TOO_LARGE')
        hashes = json.loads(archive.read('checksums.json'))
        require(isinstance(hashes, dict) and set(hashes) == set(names) - {'checksums.json'}, 'PUBLIC_HASH_MEMBERS_MISMATCH')
        for entry in entries:
            name = entry.filename
            member = PurePosixPath(name)
            require(not member.is_absolute() and '..' not in member.parts and '\\' not in name and not entry.is_dir()
                    and not stat.S_ISLNK(entry.external_attr >> 16), 'PUBLIC_UNSAFE_MEMBER')
            require(entry.file_size <= MAX_RESPONSE_BYTES, 'PUBLIC_MEMBER_TOO_LARGE')
            content = archive.read(entry)
            require(token not in content, 'PUBLIC_TOKEN_LEAK')
            if name != 'checksums.json':
                require(hashes[name] == sha256(content), 'PUBLIC_HASH_MISMATCH')
            if (name in {'job.json', 'events.json'} or name.startswith('run/')) and name.endswith(('.json', '.jsonl', '.yaml')):
                checked += 1
                text = content.decode('utf-8')
                require(str(work_root) not in text and not PRIVATE_PATH.search(text), 'PUBLIC_HOST_PATH_LEAK')
                require(not re.search(r'Bearer\s+[A-Za-z0-9_.~+/-]{8,}', text), 'PUBLIC_CREDENTIAL_LEAK')
                if name.endswith('.json'):
                    visit(json.loads(text))
                elif name.endswith('.jsonl'):
                    for line in text.splitlines():
                        if line:
                            visit(json.loads(line))
        return {'sha256': sha256(Path(path).read_bytes()), 'members': len(names), 'checksums_verified': True,
                'runtime_scan_passed': True, 'runtime_members_scanned': checked}


def wait_job(client, job_id):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        job = client.request('/api/v1/jobs/' + job_id)
        if job.get('status') in TERMINAL:
            require(job['status'] == 'SUCCEEDED', 'SAMPLE_JOB_NOT_CERTIFIED_SUCCESS')
            return job
        time.sleep(.05)
    raise VerificationError('SAMPLE_JOB_TIMEOUT')


def independent_validation(data, matrix, job, environment, work):
    require(re.fullmatch(r'[0-9a-f]{64}', matrix['hash']), 'INVALID_MATRIX_HASH')
    paths = list((data / 'matrices').glob(matrix['hash'] + '.*'))
    require(len(paths) == 1 and sha256(paths[0].read_bytes()) == matrix['hash'], 'ORIGINAL_INPUT_HASH_MISMATCH')
    run = data / 'runs' / job['id'] / 'artifacts'
    completed = subprocess.run([sys.executable, '-m', 'emsolver', 'validate', '--matrix', str(paths[0]), '--run', str(run), '--json'],
                               cwd=work, env=environment, capture_output=True, text=True, timeout=20)
    require(completed.returncode == 0, 'INDEPENDENT_VALIDATION_FAILED')
    result = json.loads(completed.stdout)
    require(result.get('numerical_status') == 'PASSED' and result.get('target_certification') == 'CERTIFIED'
            and result.get('artifact_integrity') == 'PASSED', 'INDEPENDENT_VALIDATION_NOT_CERTIFIED')
    return {key: result[key] for key in ('numerical_status', 'target_certification', 'artifact_integrity', 'n_validated')}


def verify_backup(path):
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read('backup-manifest.json'))
        hashes = manifest['files']
        require(set(hashes) == set(archive.namelist()) - {'backup-manifest.json'}, 'BACKUP_MEMBER_MISMATCH')
        for name, expected in hashes.items():
            require(sha256(archive.read(name)) == expected, 'BACKUP_HASH_MISMATCH')
    return {'sha256': sha256(path.read_bytes()), 'members_verified': len(hashes)}


def verify_instance(work, port, evidence):
    token = secrets.token_hex(32)
    environment = os.environ.copy()
    environment.pop('EMSOLVER_IDENTITIES_FILE', None)
    environment.pop('PYTHONPATH', None)
    environment['EMSOLVER_API_TOKEN'] = token
    environment.update(OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1', VECLIB_MAXIMUM_THREADS='1')
    servers = []
    checks = evidence['checks']
    data = work / 'original-data'
    try:
        evidence['stage'] = 'start_original'
        server = OwnedServer(work, data, environment, token, port, 'original')
        servers.append(server)
        health = server.start()
        evidence['version'] = health['version']
        client = server.client
        evidence['stage'] = 'frontend'
        checks['frontend'] = verify_frontend(client)
        evidence['stage'] = 'solve'
        project = client.request('/api/v1/projects', {'name': 'Offline acceptance'})
        matrix = client.request('/api/v1/samples', {'project_id': project['id'], 'kind': 'indefinite', 'n': 40, 'seed': 20261016})
        frozen = client.request('/api/v1/configs', {'config': {'problem': {'matrix_id': matrix['id']},
            'target': {'k': 4, 'sigma': .1}, 'solver': {'method': 'arpack', 'subspace_max': 20,
            'adaptive_shift': {'enabled': True, 'max_attempts': 4, 'policy': 'observed_ritz_gap_v2'}},
            'resources': {'threads': 1, 'memory_limit_gib': 1, 'walltime_seconds': 20, 'disk_limit_gib': .1}}})
        plan = client.request('/api/v1/plans', {'config_id': frozen['id']})
        submitted = client.request('/api/v1/jobs', {'plan_id': plan['id'], 'idempotency_key': 'offline-original'})
        job = wait_job(client, submitted['id'])
        checks['solver'] = {'status': job['status'], 'n': 40, 'k': 4, 'matrix_sha256': matrix['hash'], 'shift_policy': 'observed_ritz_gap_v2'}
        evidence['stage'] = 'independent_validation'
        checks['independent_validation'] = independent_validation(data, matrix, job, environment, work)
        evidence['stage'] = 'report_public_bundle'
        report = client.request('/api/v1/reports', {'job_id': job['id']})
        html = client.request('/api/v1/reports/' + report['id'], binary=True)
        require(b'<html' in html.lower(), 'REPORT_MISSING')
        checks['report'] = {'sha256': sha256(html), 'bytes': len(html)}
        bundle = work / 'public.zip'
        bundle.write_bytes(client.request('/api/v1/jobs/' + job['id'] + '/bundle', binary=True))
        checks['public_bundle'] = scan_public_bundle(bundle, token.encode(), work)
        evidence['stage'] = 'backup_restore'
        backup = work / 'private-backup.zip'
        backup.write_bytes(client.request('/api/v1/backup', {}, binary=True))
        backup_check = verify_backup(backup)
        boundary = 'emsolver-' + uuid.uuid4().hex
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="backup.zip"\r\nContent-Type: application/zip\r\n\r\n'.encode()
                + backup.read_bytes() + f'\r\n--{boundary}--\r\n'.encode())
        restored_base = data / 'restored'
        previous = {path.name for path in restored_base.iterdir()} if restored_base.exists() else set()
        restored = client.request('/api/v1/backup/restore', body, content_type='multipart/form-data; boundary=' + boundary)
        instance_id = restored.get('instance_id', '')
        require(re.fullmatch(r'[A-Za-z0-9_-]+', instance_id) and instance_id not in previous and restored.get('restored') is True
                and restored.get('degraded') is False, 'BACKUP_RESTORE_FAILED')
        restored_data = restored_base / instance_id
        checks['backup_restore'] = {**backup_check, 'restored': True, 'empty_destination': True, 'degraded': False}
        server.stop()
        evidence['stage'] = 'restored_rerun'
        restored_server = OwnedServer(work, restored_data, environment, token, 0, 'restored')
        servers.append(restored_server)
        restored_server.start()
        client = restored_server.client
        old = client.request('/api/v1/jobs/' + job['id'])
        require(old['status'] == job['status'] and old['matrix_hash'] == matrix['hash'], 'RESTORED_HISTORY_MISMATCH')
        new_plan = client.request('/api/v1/plans', {'config': frozen['config']})
        rerun = client.request('/api/v1/jobs', {'plan_id': new_plan['id'], 'idempotency_key': 'offline-restored'})
        rerun = wait_job(client, rerun['id'])
        require(rerun['id'] != job['id'], 'RESTORED_RERUN_NOT_NEW')
        checks['restored_rerun'] = {'status': rerun['status'], 'new_job': True,
                                  **independent_validation(restored_data, matrix, rerun, environment, work)}
    finally:
        for server in reversed(servers):
            server.stop()
        evidence['server_runs'] = len(servers)
        evidence['server_processes_stopped'] = all(server.stopped for server in servers)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path, help='New structured JSON evidence file')
    parser.add_argument('--work-dir', type=Path, help='New directory only; omit to use and remove a temporary directory')
    parser.add_argument('--port', type=int, default=0, help='Loopback port; 0 chooses a currently free port')
    args = parser.parse_args(argv)
    evidence = {'schema_version': '1.0', 'passed': False, 'stage': 'prepare', 'checks': {},
                'external_http_requests': 0, 'server_runs': 0, 'server_processes_stopped': True,
                'runtime': {'system': platform.system(), 'machine': platform.machine(), 'python': platform.python_version()},
                'scope': 'Fresh installed local service and independent original-input validation. Caller must enforce --network=none for OS-level offline evidence; no browser or performance claim.'}
    temporary = None
    try:
        require(not args.output.exists(), 'OUTPUT_EXISTS')
        require(0 <= args.port <= 65535, 'INVALID_PORT')
        if args.work_dir is None:
            temporary = tempfile.TemporaryDirectory(prefix='emsolver-offline-')
            work = Path(temporary.name)
        else:
            require(not args.work_dir.exists() and not args.work_dir.is_symlink(), 'WORK_DIRECTORY_EXISTS')
            args.work_dir.mkdir(parents=True)
            work = args.work_dir.resolve()
        verify_instance(work, args.port, evidence)
        require(evidence['server_processes_stopped'], 'OWNED_SERVER_NOT_STOPPED')
        evidence.update(passed=True, stage='complete')
    except Exception as exc:
        code = str(exc) if isinstance(exc, VerificationError) else type(exc).__name__
        evidence['error'] = {'code': code, 'type': type(exc).__name__}
    finally:
        if temporary is not None:
            temporary.cleanup()
    text = json.dumps(evidence, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open('x', encoding='utf-8') as handle:
            handle.write(text)
    except OSError:
        evidence['passed'] = False
        text = json.dumps(evidence, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    print(text, end='')
    return 0 if evidence['passed'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
