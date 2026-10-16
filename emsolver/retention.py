"""Conservative output expiry with reversible quarantine before database commit."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import shutil
import uuid

from .errors import ServiceError
from .jobs import TERMINAL
from .store import utcnow


RESULT_NAMES = frozenset({'eigenvalues.npy', 'eigenvectors.npy', 'residuals.npy', 'eigenvalues.csv',
                          'eigenvectors.npz', 'eigenvalues.scipy.npz', 'eigenvectors.scipy.npz',
                          'subspace.checkpoint.npz','exact-block.checkpoint.npz'})


def _time(value):
    if isinstance(value, str):
        try: value = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError: return None
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    return value.astimezone(timezone.utc)


class LifecycleManager:
    def __init__(self, gateway):
        self.gateway = gateway
        self.root = Path(gateway.root).resolve()
        self.quarantine = self.root / '.retention-trash'

    def _files(self, job_id):
        if not isinstance(job_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', job_id):
            return []
        run = self.root / 'runs' / job_id
        artifacts = run / 'artifacts'
        if (not artifacts.is_dir() or any(path.is_symlink() for path in (self.root / 'runs', run, artifacts)) or
                not artifacts.resolve().is_relative_to(self.root)):
            return []
        paths = sorted(artifacts.rglob('*'))
        if any(path.is_symlink() for path in paths):
            return []
        return [path for path in paths if path.is_file() and path.name in RESULT_NAMES]

    def _plan(self, now):
        jobs = self.gateway.store.list('jobs')
        by_id = {job['id']: job for job in jobs}
        protected = set()
        for job in jobs:
            if job.get('retained') is not False or job.get('status') not in TERMINAL:
                parent = job.get('parent_job_id')
                seen = set()
                while parent and parent not in seen:
                    seen.add(parent)
                    protected.add(parent)
                    parent = by_id.get(parent, {}).get('parent_job_id')
        items = []
        for job in jobs:
            if (job.get('retained') is not False or job.get('status') not in TERMINAL or
                    job['id'] in protected or job.get('artifacts_state') == 'EXPIRED'):
                continue
            finished = _time(job.get('finished_at'))
            if finished is None:
                continue
            try:
                project = self.gateway._project(job['project_id'])
            except ServiceError as exc:
                if exc.code in {'FORBIDDEN', 'NOT_FOUND'}: continue
                raise
            days = project.get('policy', {}).get('retention_days', 30)
            if type(days) is not int or not 1 <= days <= 36500 or finished + timedelta(days=days) >= now:
                continue
            files = self._files(job['id'])
            if not files:
                continue
            items.append({'job_id': job['id'], 'project_id': job['project_id'],
                          'bytes': sum(path.stat().st_size for path in files),
                          'files': [str(path.relative_to(self.root)) for path in files],
                          'reason': f'explicitly unretained terminal output older than {days} days'})
        return {'items': items, 'total_bytes': sum(item['bytes'] for item in items),
                'policy': 'Only expired, explicitly unretained result files; metadata, manifests, configuration and canonical events remain.'}

    def plan(self, now=None):
        now = datetime.now(timezone.utc) if now is None else _time(now)
        if now is None: raise ValueError('Retention time must include a timezone')
        with self.gateway._mutex, self.gateway.store.transaction():
            return self._plan(now)

    def _safe_relative(self, relative):
        if not isinstance(relative, str): return None
        path = Path(relative)
        if path.is_absolute() or '..' in path.parts or len(path.parts) < 4 or path.parts[0] != 'runs' or path.parts[2] != 'artifacts':
            return None
        source = self.root / path
        if (source.name not in RESULT_NAMES or not source.parent.resolve().is_relative_to(self.root) or
                any(parent.is_symlink() for parent in source.parents if parent.is_relative_to(self.root))):
            return None
        return source

    def _recover(self):
        """Recover an interrupted rename using a bounded private local journal."""
        pending = []
        if self.quarantine.is_symlink():
            raise ServiceError('UNSAFE_RETENTION_DIRECTORY')
        if not self.quarantine.exists(): return pending
        for folder in self.quarantine.iterdir():
            if folder.is_symlink() or not folder.is_dir() or not re.fullmatch(r'[0-9a-f]{32}', folder.name):
                continue
            manifest = folder / 'journal.json'
            try:
                if manifest.is_symlink() or manifest.stat().st_size > 1024**2: raise ValueError()
                journal = json.loads(manifest.read_text(encoding='utf-8'))
                if not isinstance(journal, dict) or set(journal) != {'files', 'job_ids'}: raise ValueError()
                if not isinstance(journal['files'], list) or len(journal['files']) > 10000: raise ValueError()
                if not isinstance(journal['job_ids'], list) or not 1 <= len(journal['job_ids']) <= 10000: raise ValueError()
                jobs = [self.gateway.store.get('jobs', identity) for identity in journal['job_ids']]
                for job in jobs:
                    if not job: raise ValueError()
                    self.gateway._authorize(self.gateway._project(job['project_id']), 'admin')
                committed = bool(jobs) and all(job and job.get('_retention_batch_id') == folder.name and job.get('artifacts_state') == 'EXPIRED' for job in jobs)
                if committed:
                    shutil.rmtree(folder)
                    continue
                for relative in reversed(journal['files']):
                    source = self._safe_relative(relative)
                    staged = folder / relative
                    if (source is None or staged.is_symlink() or not staged.resolve().is_relative_to(folder.resolve()) or
                            any(parent.is_symlink() for parent in staged.parents if parent.is_relative_to(folder))): raise ValueError()
                    if staged.exists():
                        if source.exists() or source.is_symlink(): raise ValueError()
                        source.parent.mkdir(parents=True, exist_ok=True)
                        staged.replace(source)
                shutil.rmtree(folder)
            except (OSError, ValueError, TypeError, KeyError, ServiceError):
                pending.append(folder.name)
        return pending

    def run(self, dry_run=True, now=None):
        if type(dry_run) is not bool: raise ValueError('dry_run must be boolean')
        now = datetime.now(timezone.utc) if now is None else _time(now)
        if now is None: raise ValueError('Retention time must include a timezone')
        if dry_run:
            return {**self.plan(now), 'dry_run': True, 'expired': [], 'removed_bytes': 0, 'disk_reclaimed_bytes': 0, 'cleanup_pending': []}
        moved, folder, plan = [], None, None
        with self.gateway._mutex:
            pending = self._recover()
            try:
                with self.gateway.store.transaction():
                    plan = self._plan(now)
                    project_ids = set(item['project_id'] for item in plan['items'])
                    for project_id in project_ids:
                        self.gateway._authorize(self.gateway._project(project_id), 'admin')
                    if not plan['items']:
                        return {**plan, 'dry_run': False, 'expired': [], 'removed_bytes': 0, 'disk_reclaimed_bytes': 0, 'cleanup_pending': pending}
                    # Materialize the existing output projection before files move;
                    # otherwise a lazy legacy scan could count the reduced tree.
                    for project_id in project_ids:
                        self.gateway.governance.add_bytes(project_id, outputs=0)
                    self.quarantine.mkdir(mode=0o700, exist_ok=True)
                    self.quarantine.chmod(0o700)
                    folder = self.quarantine / uuid.uuid4().hex
                    folder.mkdir(mode=0o700)
                    relative_files = [relative for item in plan['items'] for relative in item['files']]
                    journal = {'files': relative_files, 'job_ids': [item['job_id'] for item in plan['items']]}
                    descriptor = os.open(folder / 'journal.json', os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
                        json.dump(journal, output, allow_nan=False)
                        output.flush()
                        os.fsync(output.fileno())
                    for relative in relative_files:
                        source = self._safe_relative(relative)
                        if source is None or source.is_symlink() or not source.is_file():
                            raise ServiceError('RETENTION_INPUT_CHANGED', status=409)
                        target = folder / relative
                        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                        source.replace(target)
                        moved.append((source, target))
                    for item in plan['items']:
                        job = self.gateway.store.get('jobs', item['job_id'])
                        job.update(artifacts_state='EXPIRED', artifacts_expired_at=utcnow(), _retention_batch_id=folder.name)
                        self.gateway.store.put('jobs', job['id'], job)
                        self.gateway.store.audit('job.artifacts.expire', job['id'])
                    for project_id in project_ids:
                        amount = sum(item['bytes'] for item in plan['items'] if item['project_id'] == project_id)
                        projection = self.gateway.store.get('project_storage', project_id) or {}
                        current = max(0, int(projection.get('output_bytes', 0)))
                        self.gateway.governance.add_bytes(project_id, outputs=-min(current, amount))
            except BaseException as exc:
                rollback_failed = False
                for source, target in reversed(moved):
                    try: target.replace(source)
                    except OSError: rollback_failed = True
                if folder is not None and not rollback_failed:
                    shutil.rmtree(folder, ignore_errors=True)
                if isinstance(exc, ServiceError): raise
                raise ServiceError('RETENTION_ROLLBACK_PENDING' if rollback_failed else 'RETENTION_FAILED',
                                   'Retention could not commit; output restoration is pending' if rollback_failed else 'Retention failed and staged files were restored', 500) from None
            reclaimed_bytes = plan['total_bytes']
            try:
                shutil.rmtree(folder)
            except OSError:
                pending.append(folder.name)
                # Logical output has expired; physical disk space has not yet
                # been verified reclaimed from quarantine.
                reclaimed_bytes = 0
            return {**plan, 'dry_run': False, 'expired': [item['job_id'] for item in plan['items']],
                    'removed_bytes': plan['total_bytes'], 'disk_reclaimed_bytes': reclaimed_bytes, 'cleanup_pending': pending}
