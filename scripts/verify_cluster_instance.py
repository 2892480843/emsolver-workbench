#!/usr/bin/env python3
"""Verify real PostgreSQL + loopback HTTP + two independent compute processes.

The two numerical nodes run on ONE physical host with separate work directories.
They exchange inputs/results through HTTP, not a shared business-data directory.
A deliberate SIGSTOP barrier keeps the tiny jobs leased while the API restarts;
the report distinguishes lease overlap from observed numerical-process overlap.
Only disposable schemas, directories and processes created by this run are used.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid


TERMINAL = {'SUCCEEDED', 'PARTIAL', 'FAILED', 'CANCELLED', 'TIMED_OUT'}
REPOSITORY = Path(__file__).resolve().parents[1]


class VerificationError(RuntimeError):
    pass


def require(condition, code):
    if not condition:
        raise VerificationError(code)


def private_file(path, content):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w', encoding='utf-8') as stream:
        stream.write(content)
    return path


def utcnow():
    return datetime.now(timezone.utc).isoformat()


class ClusterVerification:
    def __init__(self, args):
        import httpx
        self.args = args
        self.output = Path(args.output).resolve()
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.run_id = uuid.uuid4().hex[:16]
        self.schema = 'emsolver_network_' + self.run_id
        self.private = self.output.parent / 'private' / ('network-' + self.run_id)
        self.private.mkdir(parents=True, mode=0o700)
        self.private.chmod(0o700)
        self.logs = self.output.parent / 'logs' / ('network-' + self.run_id)
        self.logs.mkdir(parents=True)
        self.events = self.logs / 'verification.jsonl'
        self.events.touch(mode=0o600)
        self.processes = []
        self.handles = []
        self.temporary = []
        self.server = None
        self.paused_descendants = []
        self.dsn = None
        self.origin = f'http://127.0.0.1:{args.port}'
        self.client = httpx.Client(base_url=self.origin, trust_env=False, follow_redirects=False, timeout=10)
        self.tokens = {name: secrets.token_urlsafe(48) for name in ('owner', 'analyst-a', 'viewer')}
        self.report = {
            'status': 'running', 'started_at': utcnow(), 'run_id': self.run_id,
            'platform': platform.system(), 'python': platform.python_version(),
            'topology': {'physical_hosts': 1, 'remote_worker_processes': 2,
                         'transport': 'real loopback HTTP', 'database': 'real PostgreSQL',
                         'separate_business_work_directories': True,
                         'cross_physical_machine_execution_verified': False,
                         'existing_port_8000_contacted_or_managed': False},
            'checks': [], 'logs': str(self.logs.relative_to(self.output.parent)),
        }

    def record(self, name, **details):
        event = {'time': utcnow(), 'name': name, **details}
        with self.events.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + '\n')

    def check(self, name, condition, **details):
        self.report['checks'].append({'name': name, 'passed': bool(condition), **details})
        self.record(name, passed=bool(condition), **details)
        require(condition, name)

    def request(self, method, path, token, *, expected=200, body=None, params=None, headers=None, record=True):
        response = self.client.request(method, path, headers={'Authorization': 'Bearer ' + token, **(headers or {})},
                                       json=body, params=params)
        if record:
            self.check(method + ' ' + path, response.status_code == expected, status_code=response.status_code)
        else:
            require(response.status_code == expected, 'POLL_HTTP_STATUS_' + str(response.status_code))
        return response

    def owner(self, method, path, **kwargs):
        return self.request(method, path, self.tokens['owner'], **kwargs)

    def directory(self, prefix):
        path = Path(tempfile.mkdtemp(prefix=prefix)).resolve()
        path.chmod(0o700)
        self.temporary.append(path)
        return path

    def start_process(self, command, log_name, *, env):
        log_path = self.logs / log_name
        handle = os.fdopen(os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb')
        self.handles.append(handle)
        process = subprocess.Popen(command, cwd=REPOSITORY, env=env, stdout=handle,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        self.processes.append(process)
        return process

    @staticmethod
    def stop_process(process):
        import psutil
        if process is None or process.poll() is not None:
            return
        try:
            descendants = psutil.Process(process.pid).children(recursive=True)
        except psutil.NoSuchProcess:
            descendants = []
        try:
            os.killpg(process.pid, signal.SIGCONT)
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
        # Numerical/verification subprocesses may own separate process groups.
        # Clean only descendants captured from this run's known parent.
        for child in descendants:
            try:
                if child.is_running() and child.status() != psutil.STATUS_ZOMBIE:
                    child.resume()
                    child.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        _, alive = psutil.wait_procs(descendants, timeout=2)
        for child in alive:
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass

    def start_server(self, generation):
        import httpx
        self.server = self.start_process(
            [sys.executable, '-m', 'uvicorn', 'emsolver.api:app', '--host', '127.0.0.1',
             '--port', str(self.args.port), '--log-level', 'warning'],
            f'api-{generation}.log', env=self.server_env)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            require(self.server.poll() is None, 'API_PROCESS_EXITED_DURING_STARTUP')
            try:
                response = self.client.get('/api/v1/cluster/status',
                    headers={'Authorization': 'Bearer ' + self.tokens['owner']}, timeout=1)
                if response.status_code == 200:
                    value = response.json()
                    self.check('cluster status generation ' + str(generation),
                               value.get('enabled') is True and value.get('persistence_backend') == 'postgresql'
                               and value.get('coordinator_available') is True)
                    self.record('api.ready', generation=generation, pid=self.server.pid)
                    return
            except (OSError, ValueError, httpx.HTTPError):
                pass
            time.sleep(.05)
        raise VerificationError('API_STARTUP_TIMEOUT')

    def db(self):
        import psycopg
        connection = psycopg.connect(self.dsn, autocommit=True, connect_timeout=10)
        connection.execute(psycopg.sql.SQL('SET search_path TO {}, pg_catalog').format(psycopg.sql.Identifier(self.schema)))
        return connection

    def prepare(self):
        config_path = Path(self.args.postgres_config).resolve()
        require(config_path.is_file() and not config_path.is_symlink(), 'PRIVATE_POSTGRES_CONFIG_REQUIRED')
        require(stat.S_IMODE(config_path.stat().st_mode) & 0o077 == 0, 'POSTGRES_CONFIG_MUST_BE_PRIVATE')
        config = json.loads(config_path.read_text(encoding='utf-8'))
        self.dsn = config.get('dsn')
        require(isinstance(self.dsn, str) and self.dsn.startswith(('postgresql://', 'postgres://')),
                'POSTGRES_CONFIG_REQUIRES_DSN')
        with socket.socket() as probe:
            require(probe.connect_ex(('127.0.0.1', self.args.port)) != 0, 'VERIFICATION_PORT_ALREADY_IN_USE')
        self.data = self.directory('emsolver-network-api-')
        self.node_dirs = [self.directory('emsolver-network-node-a-'), self.directory('emsolver-network-node-b-')]
        identities = private_file(self.private / 'identities.json', json.dumps({'users': [
            {'subject': subject, 'token_sha256': hashlib.sha256(token.encode()).hexdigest(),
             'global_admin': subject == 'owner'} for subject, token in self.tokens.items()
        ]}))
        base = {**os.environ, 'PYTHONUNBUFFERED': '1', 'OMP_NUM_THREADS': '1',
                'OPENBLAS_NUM_THREADS': '1', 'MKL_NUM_THREADS': '1'}
        self.server_env = {**base, 'EMSOLVER_DATABASE_URL': self.dsn,
                           'EMSOLVER_DATABASE_SCHEMA': self.schema,
                           'EMSOLVER_EXECUTION_MODE': 'cluster', 'EMSOLVER_DATA_DIR': str(self.data),
                           'EMSOLVER_IDENTITIES_FILE': str(identities)}
        self.node_env = {key: value for key, value in base.items()
                         if key not in {'EMSOLVER_DATABASE_URL', 'EMSOLVER_DATABASE_SCHEMA',
                                        'EMSOLVER_IDENTITIES_FILE', 'EMSOLVER_API_TOKEN', 'EMSOLVER_DATA_DIR'}}
        self.start_server(1)

    def setup_jobs(self):
        self.projects, self.plans, self.enrollments, self.jobs = [], [], [], []
        for label in ('A', 'B'):
            project = self.owner('POST', '/api/v1/projects', expected=201,
                                 body={'name': 'Network verification project ' + label}).json()
            self.projects.append(project)
            if label == 'A':
                self.owner('PATCH', '/api/v1/projects/' + project['id'], body={
                    'expected_version': project['version'], 'members': [
                        {'subject': 'owner', 'role': 'admin'},
                        {'subject': 'analyst-a', 'role': 'analyst'},
                        {'subject': 'viewer', 'role': 'viewer'}]})
            matrix = self.owner('POST', '/api/v1/samples', expected=201,
                body={'project_id': project['id'], 'n': 12, 'kind': 'indefinite', 'seed': 20261016}).json()
            plan = self.owner('POST', '/api/v1/plans', expected=201, body={'config': {
                'problem': {'matrix_id': matrix['id']}, 'target': {'k': 2, 'sigma': .1},
                'resources': {'threads': 1, 'memory_limit_gib': 1, 'disk_limit_gib': .05,
                              'walltime_seconds': 30}}}).json()
            self.plans.append(plan)
            enrollment = self.owner('POST', '/api/v1/cluster/workers', expected=201, body={
                'name': 'network-node-' + label.lower(), 'project_ids': [project['id']],
                'resources': {'threads': 1, 'memory_gib': 1, 'disk_gib': .05}, 'backends': ['scipy']}).json()
            enrollment['token_file'] = private_file(self.private / ('node-' + label.lower() + '.token'), enrollment['token'])
            self.enrollments.append(enrollment)
        # Put B first to prove that node A's project allowlist can skip an older
        # queued job rather than accepting whatever happens to be at the head.
        submitted = {}
        for index in (1, 0):
            token = self.tokens['analyst-a' if index == 0 else 'owner']
            submitted[index] = self.request('POST', '/api/v1/jobs', token, expected=201,
                body={'plan_id': self.plans[index]['id'], 'idempotency_key': self.run_id + '-' + str(index)}).json()
        self.jobs = [submitted[index] for index in (0, 1)]
        self.check('node token files are owner-readable and owner-writable only',
                   all(stat.S_IMODE(value['token_file'].stat().st_mode) == 0o600 for value in self.enrollments))
        self.report['jobs'] = [{'id': value['id'], 'project_id': value['project_id']} for value in self.jobs]
        self.report['workers'] = [value['worker']['id'] for value in self.enrollments]

    def permissions_and_expiry(self):
        from emsolver.cluster_protocol import execution_fingerprint
        a, b = self.jobs
        viewer, analyst = self.tokens['viewer'], self.tokens['analyst-a']
        for token in (viewer, analyst):
            self.request('GET', '/api/v1/jobs/' + a['id'], token)
            self.request('GET', '/api/v1/jobs/' + b['id'], token, expected=403)
            self.request('GET', '/api/v1/cluster/workers', token, expected=403)
            self.request('POST', '/api/v1/cluster/workers', token, expected=403, body={
                'name': 'forbidden-node', 'project_ids': [a['project_id']],
                'resources': {'threads': 1, 'memory_gib': 1, 'disk_gib': .05}})
            self.request('POST', '/api/v1/cluster/workers/' + self.enrollments[0]['worker']['id'] + '/revoke',
                         token, expected=403)
        self.request('POST', '/api/v1/jobs', viewer, expected=403, body={'plan_id': self.plans[0]['id']})
        self.owner('POST', '/internal/v1/cluster/claim', expected=401, body={'capabilities': {}})
        self.request('GET', '/api/v1/projects', self.enrollments[0]['token'], expected=401)
        caps = {'threads': 1, 'memory_gib': 1, 'disk_gib': .05, 'backends': ['scipy'],
                'devices': ['cpu'], 'execution': execution_fingerprint()}
        stale = {**caps, 'execution': {'protocol': 'old', 'source_sha256': '0' * 64}}
        self.request('POST', '/internal/v1/cluster/claim', self.enrollments[0]['token'],
                     expected=409, body={'capabilities': stale})
        assignment = self.request('POST', '/internal/v1/cluster/claim', self.enrollments[0]['token'],
                                   body={'capabilities': caps}).json()
        self.check('project allowlist skips older foreign queued job', assignment['job']['id'] == a['id'])
        self.check('assignment omits server business paths', '_path' not in json.dumps(assignment))
        self.old_lease = assignment['lease']
        self.request('GET', assignment['input']['url'], self.enrollments[1]['token'], expected=409,
            headers={'X-EMSolver-Lease': self.old_lease['token'], 'X-EMSolver-Attempt': str(self.old_lease['attempt'])})
        with self.db() as connection:
            connection.execute("UPDATE cluster_job_attempts SET expires_at=EXTRACT(EPOCH FROM clock_timestamp())-1 WHERE job_id=%s AND state='ACTIVE'", (a['id'],))
        self.request('POST', '/internal/v1/cluster/jobs/' + a['id'] + '/heartbeat', self.enrollments[0]['token'],
            expected=409, body={'lease_token': self.old_lease['token'], 'attempt': self.old_lease['attempt']})
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            value = self.owner('GET', '/api/v1/jobs/' + a['id'], record=False).json()
            if value['status'] == 'QUEUED':
                break
            time.sleep(.05)
        self.check('expired attempt is durably requeued', value['status'] == 'QUEUED')

    def run_nodes_and_restart(self):
        import psutil
        self.nodes = []
        for index in range(2):
            self.nodes.append(self.start_process([sys.executable, '-m', 'emsolver', 'remote-worker',
                '--server', self.origin, '--token-file', str(self.enrollments[index]['token_file']),
                '--work-dir', str(self.node_dirs[index]), '--once', '--threads', '1', '--memory-gib', '1',
                '--disk-gib', '.05', '--heartbeat-interval', '.2', '--allow-insecure-http'],
                'node-' + str(index + 1) + '.log', env=self.node_env))
        suspended = set()
        deadline = time.monotonic() + 25
        active = {}
        while len(suspended) != 2 and time.monotonic() < deadline:
            for index, job in enumerate(self.jobs):
                if index in suspended:
                    continue
                value = self.owner('GET', '/api/v1/jobs/' + job['id'], record=False).json()
                require(value['status'] not in TERMINAL, 'JOB_FINISHED_BEFORE_RESTART_BARRIER')
                require(self.nodes[index].poll() is None, 'NODE_EXITED_BEFORE_RESTART_BARRIER')
                if value['status'] == 'RUNNING':
                    os.killpg(self.nodes[index].pid, signal.SIGSTOP)
                    # The numerical executable deliberately owns a separate
                    # session. Pause any existing descendants explicitly too.
                    for child in psutil.Process(self.nodes[index].pid).children(recursive=True):
                        try:
                            if child.status() != psutil.STATUS_ZOMBIE:
                                child.suspend()
                                self.paused_descendants.append(child)
                        except psutil.NoSuchProcess:
                            pass
                    suspended.add(index)
                    active[job['id']] = self.owner('GET', '/api/v1/cluster/jobs/' + job['id'] + '/lease').json()['active']
            time.sleep(.01)
        self.check('two independent CLI node processes hold active leases', len(suspended) == 2)
        self.check('nodes have disjoint work directories', self.node_dirs[0] != self.node_dirs[1]
                   and not self.data.is_relative_to(self.node_dirs[0]) and not self.data.is_relative_to(self.node_dirs[1]))
        old_pid = self.server.pid
        self.stop_process(self.server)
        with self.db() as connection:
            rows = connection.execute("SELECT job_id,attempt,worker_id,state FROM cluster_job_attempts WHERE state='ACTIVE' ORDER BY job_id").fetchall()
        self.check('leases remain active after API process exits', len(rows) == 2 and {row[0] for row in rows} == {job['id'] for job in self.jobs})
        self.start_server(2)
        self.check('API restarted as a new process', self.server.pid != old_pid)
        for job in self.jobs:
            after = self.owner('GET', '/api/v1/cluster/jobs/' + job['id'] + '/lease').json()['active']
            self.check('lease ownership survived restart ' + job['id'], after is not None
                       and all(after[key] == active[job['id']][key] for key in ('attempt', 'worker_id', 'expires_at')))
        self.report['api_restart'] = {'old_pid': old_pid, 'new_pid': self.server.pid,
            'active_job_ids': list(active), 'same_lease_ownership_after_restart': True,
            'barrier': 'SIGSTOP both node process groups and observed descendants after claim; SIGCONT after API ready',
            'computation_during_api_downtime_claimed': False}
        observations = []
        observation_done = threading.Event()

        def observe_children():
            while not observation_done.is_set():
                children = []
                for node in self.nodes:
                    found = []
                    try:
                        for child in psutil.Process(node.pid).children(recursive=True):
                            try:
                                if ('emsolver.worker' in child.cmdline()
                                        and child.status() not in {psutil.STATUS_ZOMBIE, psutil.STATUS_STOPPED}):
                                    found.append(child.pid)
                            except (psutil.NoSuchProcess, psutil.AccessDenied):
                                pass
                    except psutil.NoSuchProcess:
                        pass
                    children.append(found)
                if all(children):
                    observations.append({'time': utcnow(), 'numerical_child_pids': children})
                    return
                observation_done.wait(.005)

        observer = threading.Thread(target=observe_children, name='cluster-verification-observer', daemon=True)
        observer.start()
        for child in self.paused_descendants:
            try:
                child.resume()
            except psutil.NoSuchProcess:
                pass
        for node in self.nodes:
            os.killpg(node.pid, signal.SIGCONT)
        latest = {}
        deadline = time.monotonic() + self.args.timeout
        try:
            while time.monotonic() < deadline:
                latest = {job['id']: self.owner('GET', '/api/v1/jobs/' + job['id'], record=False).json() for job in self.jobs}
                if all(value['status'] in TERMINAL for value in latest.values()):
                    break
                time.sleep(.02)
        finally:
            observation_done.set()
            observer.join(timeout=2)
        self.report['simultaneous_numerical_process_observations'] = observations
        self.report['simultaneous_numerical_processes_observed'] = bool(observations)
        self.finished = [latest[job['id']] for job in self.jobs]
        self.check('both real numerical jobs succeeded', all(job['status'] == 'SUCCEEDED' for job in self.finished),
                   statuses=[job['status'] for job in self.finished])
        for node in self.nodes:
            node.wait(timeout=15)
        self.check('both independent node CLIs exited successfully', all(node.returncode == 0 for node in self.nodes))
        self.request('POST', '/internal/v1/cluster/jobs/' + self.jobs[0]['id'] + '/failure',
            self.enrollments[0]['token'], expected=409, body={
                'lease_token': self.old_lease['token'], 'attempt': self.old_lease['attempt'], 'code': 'LATE_ATTEMPT'})

    def verify_results(self):
        import numpy as np
        workers = []
        summaries = []
        for index, job in enumerate(self.finished):
            metrics, validation = job['metrics'], job['result']['validation']
            workers.append(metrics.get('worker_id'))
            self.check('control-plane independent verification ' + job['id'],
                       metrics.get('remote_worker') is True
                       and metrics.get('verification_source') == 'control_plane_independent'
                       and validation.get('numerical_status') == 'PASSED'
                       and validation.get('target_certification') == 'CERTIFIED')
            self.check('job stayed within assigned node project scope ' + job['id'],
                       metrics.get('worker_id') == self.enrollments[index]['worker']['id'])
            response = self.owner('GET', '/api/v1/jobs/' + job['id'] + '/download', params={'file': 'eigenvalues.npy'})
            values = np.load(io.BytesIO(response.content), allow_pickle=False)
            self.check('canonical eigenvalue artifact is valid ' + job['id'], values.shape == (2,) and np.isfinite(values).all())
            self.request('GET', '/api/v1/jobs/' + job['id'] + '/download', self.tokens['viewer'],
                         expected=200 if index == 0 else 403, params={'file': 'eigenvalues.npy'})
            summaries.append({'id': job['id'], 'project_id': job['project_id'], 'status': job['status'],
                'worker_id': metrics['worker_id'], 'worker_attempt': metrics['worker_attempt'],
                'verification_source': metrics['verification_source'],
                'target_certification': validation['target_certification'],
                'artifact_sha256': hashlib.sha256(response.content).hexdigest(),
                'artifact_modes': int(values.size), 'started_at': job['started_at'], 'finished_at': job['finished_at']})
        self.check('two jobs executed on different node identities', len(set(workers)) == 2)
        starts = [datetime.fromisoformat(job['started_at']) for job in self.finished]
        ends = [datetime.fromisoformat(job['finished_at']) for job in self.finished]
        overlap = max(0., (min(ends) - max(starts)).total_seconds())
        self.report['jobs'] = summaries
        self.report['assignment_interval_overlap_seconds'] = overlap
        self.report['overlap_interpretation'] = 'Assignment/lease intervals include the explicit restart pause; this is not a CPU timing benchmark.'
        self.check('active job assignment intervals overlapped', overlap > 0, seconds=overlap)
        public_workers = self.owner('GET', '/api/v1/cluster/workers').json()
        audit = self.owner('GET', '/api/v1/audit').json()
        public = json.dumps({'workers': public_workers, 'audit': audit})
        self.check('public records and audit contain no machine credentials',
                   all(enrollment['token'] not in public for enrollment in self.enrollments)
                   and 'token_hash' not in json.dumps(public_workers))
        logs = ''.join(path.read_text(encoding='utf-8', errors='replace') for path in self.logs.iterdir() if path.is_file())
        secrets_to_exclude = [*self.tokens.values(), self.dsn, *(value['token'] for value in self.enrollments)]
        self.check('verification logs contain no bearer credentials or database DSN',
                   all(value not in logs for value in secrets_to_exclude))

    def cleanup(self):
        errors = []
        for process in reversed(self.processes):
            try:
                self.stop_process(process)
            except Exception:
                errors.append('PROCESS_CLEANUP_FAILED')
        for handle in self.handles:
            handle.close()
        if self.dsn:
            try:
                import psycopg
                with psycopg.connect(self.dsn, autocommit=True, connect_timeout=10) as connection:
                    connection.execute(psycopg.sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(psycopg.sql.Identifier(self.schema)))
            except Exception:
                errors.append('SCHEMA_CLEANUP_FAILED')
        for directory in self.temporary:
            try:
                shutil.rmtree(directory)
            except OSError:
                errors.append('TEMPORARY_DATA_CLEANUP_FAILED')
        shutil.rmtree(self.private, ignore_errors=True)
        self.client.close()
        self.report['cleanup'] = {'created_processes_stopped': all(process.poll() is not None for process in self.processes),
            'disposable_schema_dropped': 'SCHEMA_CLEANUP_FAILED' not in errors,
            'temporary_data_removed': all(not directory.exists() for directory in self.temporary),
            'credential_files_removed': not self.private.exists(), 'errors': errors}
        if errors:
            self.report['status'] = 'failed'
        self.report['finished_at'] = utcnow()
        self.output.write_text(json.dumps(self.report, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')

    def run(self):
        try:
            self.prepare()
            self.setup_jobs()
            self.permissions_and_expiry()
            self.run_nodes_and_restart()
            self.verify_results()
            self.report['status'] = 'passed'
        except Exception as exc:
            self.report['status'] = 'failed'
            # Never serialize arbitrary transport/database exception text: it
            # can contain URLs, credentials or request bodies.
            self.report['failure'] = str(exc) if isinstance(exc, VerificationError) else type(exc).__name__
        finally:
            self.cleanup()
        return self.report['status'] == 'passed'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--postgres-config', required=True, help='Private JSON file containing a dsn field; never printed')
    parser.add_argument('--output', required=True, help='Public evidence JSON path (contains no credentials)')
    parser.add_argument('--port', type=int, default=18082)
    parser.add_argument('--timeout', type=float, default=90)
    args = parser.parse_args(argv)
    require(1024 <= args.port <= 65535 and args.port != 8000, 'USE_A_DISPOSABLE_HIGH_PORT_OTHER_THAN_8000')
    require(0 < args.timeout <= 600, 'TIMEOUT_MUST_BE_POSITIVE_AND_AT_MOST_600')
    require(os.name == 'posix', 'THIS_RESTART_BARRIER_REQUIRES_POSIX_PROCESS_GROUPS')
    sys.path.insert(0, str(REPOSITORY))
    verification = ClusterVerification(args)
    passed = verification.run()
    print(json.dumps({'status': verification.report['status'], 'checks': len(verification.report['checks']),
                      'evidence': str(verification.output)}, ensure_ascii=False))
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
