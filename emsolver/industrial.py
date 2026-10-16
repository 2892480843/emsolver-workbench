"""Immutable acceptance dossiers and fail-closed, post-run evidence checks.

The gateway supplies existing project authorization, storage and quota policy.
No reference data or acceptance declarations are sent to solver workers.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import psutil

from .contracts import content_hash
from .engineering import EngineeringAssets, canonical
from .engineering_contracts import frequency_values
from .errors import ServiceError
from .industrial_attestation import attestation_message, verify_attestation
from .industrial_contracts import (MAX_CHECK_BYTES, MAX_DOSSIER_BYTES, MAX_MANIFEST_BYTES,
    MAX_REFERENCE_ENTRIES, MAX_REFERENCE_MODES, Manifest, validate_manifest)
from .integrity import file_hash
from .jobs import TERMINAL
from .process_environment import child_environment
from .reporting import sanitize_public
from .store import utcnow


def _unsigned(value):
    return {key: item for key, item in value.items() if key != 'attestation'} if value else None


def _gate(name, status, reason, **details):
    return {'name': name, 'status': status, 'reason': reason, 'details': details}


def _finite(value, positive=False):
    return type(value) in (int, float) and math.isfinite(value) and (value > 0 if positive else value >= 0)


class IndustrialCases:
    def __init__(self, gateway):
        self.gateway = gateway
        self.folder = Path(gateway.root) / 'industrial'
        with gateway._mutex:
            if not hasattr(gateway, '_industrial_check_lock'): gateway._industrial_check_lock = threading.Lock()

    def _project(self, project_id, permission='read'):
        project = self.gateway._project(project_id, writable=permission != 'read')
        self.gateway._authorize(project, permission)
        return project

    def _record(self, kind, identity):
        record = self.gateway._get(kind, identity)
        self._project(record['project_id'])
        return record

    def _read(self, record, maximum):
        digest = record.get('dossier_sha256') or record.get('check_sha256')
        if record.get('availability') == 'MISSING': raise ServiceError('INDUSTRIAL_PAYLOAD_MISSING', status=409)
        if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest): raise ServiceError('INDUSTRIAL_INTEGRITY_FAILED', status=409)
        path = self.folder / (digest + '.json')
        if (self.folder.is_symlink() or path.is_symlink() or not path.is_file() or str(path) != record.get('_path') or
            not path.resolve().is_relative_to(Path(self.gateway.root).resolve()) or path.stat().st_size > maximum or file_hash(path) != digest):
            raise ServiceError('INDUSTRIAL_INTEGRITY_FAILED', status=409)
        try: return json.loads(path.read_text(encoding='utf-8'))
        except (ValueError, OSError): raise ServiceError('INDUSTRIAL_INTEGRITY_FAILED', status=409) from None

    def _append(self, kind, payload, fields, maximum):
        data = canonical(payload)
        if len(data) > maximum: raise ServiceError('INDUSTRIAL_EVIDENCE_TOO_LARGE', status=413)
        digest = hashlib.sha256(data).hexdigest()
        key = 'dossier_sha256' if kind == 'industrial_cases' else 'check_sha256'
        with self.gateway._mutex, self.gateway.store.transaction():
            self._project(fields['project_id'], 'admin' if kind == 'industrial_cases' else 'write')
            same = self.gateway.store.db.execute(
                "SELECT body FROM entities WHERE kind=? AND project_id=? AND json_extract(body, '$.' || ?)=? LIMIT 1",
                (kind, fields['project_id'], key, digest)).fetchone()
            if same:
                existing = json.loads(same[0]); self._read(existing, maximum)
                return existing
            if self.folder.is_symlink(): raise ServiceError('INDUSTRIAL_INTEGRITY_FAILED')
            self.folder.mkdir(mode=0o700, exist_ok=True)
            path = self.folder / (digest + '.json')
            if path.is_symlink() or (path.exists() and file_hash(path) != digest): raise ServiceError('INDUSTRIAL_INTEGRITY_FAILED')
            self.gateway.governance.reserve(fields['project_id'], input_bytes=len(data))
            new = not path.exists()
            try:
                if new:
                    with path.open('xb') as output: output.write(data)
                    path.chmod(0o400)
                record = {'schema_version': '1.0', **copy.deepcopy(fields), key: digest, 'bytes': len(data), '_path': str(path), 'availability': 'READY'}
                self.gateway.store.put(kind, record['id'], record)
                self.gateway.governance.add_bytes(fields['project_id'], inputs=len(data))
                self.gateway.store.audit('industrial.case.create' if kind == 'industrial_cases' else 'industrial.check', record['id'])
                return record
            except BaseException:
                if new: path.unlink(missing_ok=True)
                raise

    @staticmethod
    def _snapshot(job):
        keys = ('id', 'project_id', 'matrix_id', 'matrix_hash', 'status', 'config', 'result', 'metrics',
                'runtime', 'engineering_model', 'created_at', 'finished_at', 'artifacts_state')
        return sanitize_public({key: copy.deepcopy(job[key]) for key in keys if key in job})

    def _publication(self, job):
        folder = Path(self.gateway.root) / 'runs' / job['id'] / 'artifacts'
        path = folder / 'manifest.json'
        if (any(part.is_symlink() for part in (folder.parent.parent, folder.parent, folder, path)) or
            not path.is_file() or path.stat().st_size > 1024**2): return None
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
            hashes = value['artifacts']
            if (value.get('input_sha256') != job['matrix_hash'] or not isinstance(hashes, dict) or len(hashes) > 64 or
                any(not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', name) or not isinstance(sha, str) or
                    not re.fullmatch(r'[0-9a-f]{64}', sha) for name, sha in hashes.items())): return None
            return {'sha256': file_hash(path), 'artifacts': hashes, 'status': value.get('status')}
        except (ValueError, KeyError, TypeError, OSError): return None

    def _bindings(self, manifest, matrix, model, job, reference_asset=None):
        reference = manifest['reference']
        target_hash = content_hash(job['config']['target'])
        if reference and (reference['matrix_sha256'] != matrix['hash'] or reference['target_sha256'] != target_hash or
            (reference_asset['n'] != matrix['n'] if reference_asset else len(reference['vectors_real']) != matrix['n'])):
            raise ServiceError('INDUSTRIAL_REFERENCE_BINDING_MISMATCH', status=409)
        return {'matrix_sha256': matrix['hash'], 'engineering_sha256': model.get('sidecar_sha256') if model else None,
            'target_sha256': target_hash, 'physical_sha256': content_hash(_unsigned(manifest['physical'])),
            'reference_sha256': content_hash(_unsigned(reference)), 'candidate_job_id': job['id'],
            'baseline_job_id': manifest['baseline_job_id'], 'declared_use': manifest['declared_use'],
            'acceptance_scope': manifest['acceptance_scope'], 'reference_asset_id': reference_asset['id'] if reference_asset else None,
            'reference_asset_sha256': reference_asset['sha256'] if reference_asset else None}

    def template(self, project_id, job_id):
        self._project(project_id)
        job = self.gateway.get_job(job_id)
        if job['project_id'] != project_id: raise ServiceError('PROJECT_MISMATCH', status=409)
        manifest = validate_manifest({'schema_version': '1.0', 'title': 'Acceptance ' + job_id,
            'declared_use': 'research', 'matrix_id': job['matrix_id'],
            'engineering_asset_id': (job.get('engineering_model') or {}).get('id'),
            'candidate_job_id': job_id, 'baseline_job_id': None, 'physical': None, 'reference': None, 'criteria': {}})
        return {'manifest': manifest, 'binding': {'matrix_sha256': job['matrix_hash'], 'target_sha256': content_hash(job['config']['target'])},
            'limits': {'request_bytes': MAX_MANIFEST_BYTES, 'reference_modes': MAX_REFERENCE_MODES,
                'reference_vector_entries': MAX_REFERENCE_ENTRIES, 'check_bytes': MAX_CHECK_BYTES,
                'external_reference_bytes': 128 * 1024**2, 'external_reference_vector_entries': 8_000_000,
                'external_reference_rows': 2_000_000},
            'schema': Manifest.model_json_schema(),
            'notes': ['Missing engineering, physical review or independent reference remains UNVERIFIED.',
                'Only completed jobs can be frozen; configured issuer receipts authenticate assertions, not physical truth.']}

    def create(self, project_id, value, *, parent_case_id=None):
        self._project(project_id, 'admin')
        try:
            if len(canonical(value)) > MAX_MANIFEST_BYTES: raise ServiceError('INDUSTRIAL_EVIDENCE_TOO_LARGE', status=413)
        except (ValueError, TypeError, OverflowError, RecursionError): raise ServiceError('INVALID_INDUSTRIAL_MANIFEST') from None
        manifest = validate_manifest(value)
        with self.gateway._mutex, self.gateway.store.transaction():
            matrix = self.gateway._get('matrices', manifest['matrix_id'])
            job = self.gateway.get_job(manifest['candidate_job_id'])
            if matrix['project_id'] != project_id or job['project_id'] != project_id or job['matrix_id'] != matrix['id'] or job['matrix_hash'] != matrix['hash']:
                raise ServiceError('INDUSTRIAL_BINDING_MISMATCH', status=409)
            if job['status'] not in TERMINAL: raise ServiceError('INDUSTRIAL_JOB_NOT_TERMINAL', status=409)
            model = None
            if manifest['engineering_asset_id']:
                assets = EngineeringAssets(self.gateway)
                model = assets.get(manifest['engineering_asset_id']); assets.manifest(model['id'])
                if model['matrix_id'] != matrix['id'] or any((job.get('engineering_model') or {}).get(key) != model[key] for key in ('id', 'sidecar_sha256', 'matrix_hash')):
                    raise ServiceError('INDUSTRIAL_ENGINEERING_NOT_FROZEN', status=409)
            baseline = None
            if manifest['baseline_job_id']:
                baseline = self.gateway.get_job(manifest['baseline_job_id'])
                if baseline['id'] == job['id'] or baseline['project_id'] != project_id or baseline['matrix_hash'] != matrix['hash'] or baseline['status'] not in TERMINAL:
                    raise ServiceError('INDUSTRIAL_BASELINE_MISMATCH', status=409)
            parent = self._record('industrial_cases', parent_case_id) if parent_case_id else None
            if parent and parent['project_id'] != project_id: raise ServiceError('PROJECT_MISMATCH', status=409)
            reference_asset = None
            if manifest['reference'] and manifest['reference']['artifact_id']:
                from .industrial_reference import IndustrialReferenceAssets
                references = IndustrialReferenceAssets(self.gateway)
                reference_asset = references.get(manifest['reference']['artifact_id'])
                references.path(reference_asset['id'])
                if reference_asset['project_id'] != project_id or reference_asset['sha256'] != manifest['reference']['artifact_sha256']:
                    raise ServiceError('INDUSTRIAL_REFERENCE_BINDING_MISMATCH', status=409)
            binding = self._bindings(manifest, matrix, model, job, reference_asset)
            payload = {'schema_version': '1.0', 'manifest': manifest, 'binding': binding,
                'parent_case_id': parent_case_id, 'revision': parent['revision'] + 1 if parent else 1,
                'snapshots': {'matrix': {key: copy.deepcopy(matrix.get(key)) for key in ('id', 'hash', 'n', 'source')},
                    'engineering': EngineeringAssets._public(model) if model else None, 'candidate': self._snapshot(job),
                    'baseline': self._snapshot(baseline) if baseline else None, 'publication': self._publication(job),
                    'reference_asset': reference_asset, 'baseline_publication': self._publication(baseline) if baseline else None}}
            record = self._append('industrial_cases', payload, {'id': 'ic_' + uuid.uuid4().hex, 'schema_version': '1.0',
                'project_id': project_id, 'matrix_id': matrix['id'], 'title': manifest['title'], 'declared_use': manifest['declared_use'], 'acceptance_scope': manifest['acceptance_scope'],
                'candidate_job_id': job['id'], 'revision': payload['revision'], 'parent_case_id': parent_case_id,
                'binding': binding, 'created_at': utcnow()}, MAX_DOSSIER_BYTES)
            return self.get(record['id'])

    def revise(self, case_id, manifest):
        parent = self._record('industrial_cases', case_id)
        return self.create(parent['project_id'], manifest, parent_case_id=case_id)

    def get(self, case_id):
        record = self._record('industrial_cases', case_id)
        self._read(record, MAX_DOSSIER_BYTES)
        summary = self.gateway._public(record)
        with self.gateway.store.lock:
            row = self.gateway.store.db.execute("SELECT body FROM entities WHERE kind='industrial_checks' AND project_id=? AND json_extract(body,'$.case_id')=? ORDER BY seq DESC LIMIT 1", (record['project_id'], case_id)).fetchone()
        summary['latest_check'] = self._read(json.loads(row[0]), MAX_CHECK_BYTES) if row else None
        return summary

    def list(self, project_id, *, limit=20, cursor=None):
        self._project(project_id)
        try: page = self.gateway.store.page('industrial_cases', project_id=project_id, limit=limit, cursor=cursor)
        except ValueError: raise ServiceError('INVALID_PAGE') from None
        return {**page, 'items': [self.get(item['id']) for item in page['items']]}

    def dossier(self, case_id): return self._read(self._record('industrial_cases', case_id), MAX_DOSSIER_BYTES)

    def check_record(self, check_id): return self._read(self._record('industrial_checks', check_id), MAX_CHECK_BYTES)

    def checks(self, case_id, *, limit=20):
        case = self._record('industrial_cases', case_id)
        if type(limit) is not int or not 1 <= limit <= 100: raise ServiceError('INVALID_PAGE')
        with self.gateway.store.lock:
            rows = self.gateway.store.db.execute("SELECT body FROM entities WHERE kind='industrial_checks' AND project_id=? AND json_extract(body,'$.case_id')=? ORDER BY seq DESC LIMIT ?", (case['project_id'], case_id, limit)).fetchall()
        return {'items': [self._read(json.loads(row[0]), MAX_CHECK_BYTES) for row in rows], 'limit': limit}

    def export(self, case_id):
        case = self.get(case_id)
        self.gateway.store.audit('industrial.export', case_id)
        return {'schema_version': '1.0', 'case': case, 'dossier': self.dossier(case_id), 'checks': self.checks(case_id)['items'],
            'scope': 'Private project-authorized acceptance evidence; trust keys are excluded; no official certification.'}

    def _artifacts(self, dossier):
        job = dossier['snapshots']['candidate']
        if job.get('artifacts_state') in {'MISSING', 'EXPIRED'}: return _gate('artifacts', 'UNVERIFIED', 'RESULT_FILES_UNAVAILABLE'), None
        current = self._publication(job)
        frozen = dossier['snapshots']['publication']
        if current is None or frozen is None: return _gate('artifacts', 'UNVERIFIED', 'NO_PUBLISHED_RESULT'), None
        if current != frozen: return _gate('artifacts', 'FAIL', 'PUBLICATION_CHANGED'), None
        folder = Path(self.gateway.root) / 'runs' / job['id'] / 'artifacts'
        required = {'eigenvalues.npy', 'eigenvectors.npy', 'residuals.npy', 'validation.json', 'config.resolved.yaml'}
        try:
            if not required <= current['artifacts'].keys(): raise ValueError('Missing artifact hashes')
            for name in required:
                path = folder / name
                if path.is_symlink() or not path.is_file() or file_hash(path) != current['artifacts'][name]: raise ValueError('Artifact changed')
            matrix = self.gateway._get('matrices', job['matrix_id'])
            path = Path(matrix['_path'])
            if path.is_symlink() or not path.is_file() or file_hash(path) != job['matrix_hash']: raise ValueError('Matrix changed')
        except (OSError, ValueError): return _gate('artifacts', 'FAIL', 'INPUT_OR_RESULT_INTEGRITY_FAILED'), None
        return _gate('artifacts', 'PASS', 'PUBLISHED_HASHES_MATCH'), folder

    def _binding_gate(self, dossier):
        frozen = dossier['snapshots']['candidate']
        if self._snapshot(self.gateway.get_job(frozen['id'])) != frozen:
            return _gate('frozen_binding', 'FAIL', 'FROZEN_JOB_CHANGED')
        model = dossier['snapshots']['engineering']
        if model is not None:
            try:
                assets = EngineeringAssets(self.gateway)
                current = assets.get(model['id']); assets.manifest(model['id'])
                if any(current.get(key) != model.get(key) for key in ('id', 'project_id', 'matrix_id', 'matrix_hash', 'sidecar_sha256')):
                    raise ServiceError('ENGINEERING_BINDING_CHANGED')
            except ServiceError as exc:
                return _gate('frozen_binding', 'UNVERIFIED' if 'MISSING' in exc.code else 'FAIL', exc.code)
        return _gate('frozen_binding', 'PASS', 'FROZEN_JOB_AND_ENGINEERING_MATCH')

    def _reference(self, dossier, folder):
        import numpy as np
        manifest, binding = dossier['manifest'], dossier['binding']
        reference = manifest['reference']; criteria = manifest['criteria']
        if reference is None: return [_gate('reference_provenance', 'UNVERIFIED', 'NO_INDEPENDENT_REFERENCE'),
            _gate('reference_comparison', 'UNVERIFIED', 'NO_INDEPENDENT_REFERENCE')], None
        publication = dossier['snapshots']['publication'] or {}
        candidate_hashes = {binding['matrix_sha256'], publication.get('sha256'), *publication.get('artifacts', {}).values()}
        if reference['artifact_sha256'] in candidate_hashes or reference['producer'] == manifest['candidate_job_id']:
            provenance = _gate('reference_provenance', 'FAIL', 'CANDIDATE_PRODUCED_REFERENCE')
        elif reference['method'] == 'analytic' and manifest['declared_use'] == 'industrial':
            provenance = _gate('reference_provenance', 'UNVERIFIED', 'ANALYTIC_REFERENCE_NOT_INDUSTRIAL_EVIDENCE')
        else:
            status, reason = verify_attestation(reference['attestation'], binding, 'reference', data_root=self.gateway.root)
            provenance = _gate('reference_provenance', status, reason, method=reference['method'], issuer=(reference['attestation'] or {}).get('issuer'))
        if folder is None: return [provenance, _gate('reference_comparison', 'UNVERIFIED', 'CANDIDATE_ARTIFACTS_UNAVAILABLE')], None
        try:
            if reference['artifact_id'] is not None:
                from .industrial_reference import IndustrialReferenceAssets
                assets = IndustrialReferenceAssets(self.gateway)
                frozen = dossier['snapshots']['reference_asset']; current = assets.get(reference['artifact_id'])
                if any(current.get(key) != frozen.get(key) for key in ('id', 'project_id', 'sha256', 'n', 'modes', 'bytes')):
                    return [provenance, _gate('reference_comparison', 'FAIL', 'FROZEN_REFERENCE_ASSET_CHANGED')], None
                result = assets.compare(reference['artifact_id'], folder, criteria)
                return [provenance, _gate('reference_comparison', 'PASS' if result['passed'] else 'FAIL', 'INDEPENDENT_REFERENCE_COMPARISON',
                    **{key: value for key, value in result.items() if key not in {'reference_eigenvalues', 'candidate_eigenvalues'}})], result
            for name in ('eigenvalues.npy', 'eigenvectors.npy'):
                if (folder / name).stat().st_size > MAX_REFERENCE_ENTRIES * 16 + 4096: raise ValueError('Reference budget')
            candidate = np.load(folder / 'eigenvectors.npy', allow_pickle=False, mmap_mode='r')
            if candidate.ndim != 2 or candidate.size > MAX_REFERENCE_ENTRIES or candidate.shape[1] > MAX_REFERENCE_MODES:
                raise ValueError('Reference budget')
            real = np.asarray(reference['vectors_real'], dtype=np.float64)
            basis = real + 1j * np.asarray(reference['vectors_imag'], dtype=np.float64) if reference['vectors_imag'] is not None else real
            with tempfile.TemporaryDirectory(prefix='industrial-reference-', dir=self.gateway.root / 'inbox') as directory:
                target = Path(directory)
                np.save(target / 'eigenvalues.npy', np.asarray(reference['eigenvalues'], dtype=np.float64))
                np.save(target / 'eigenvectors.npy', basis)
                spec, output = target / 'spec.json', target / 'comparison.json'
                spec.write_bytes(canonical({'candidate': str(folder), 'reference': str(target), 'criteria': criteria}))
                process = subprocess.Popen([sys.executable, '-m', 'emsolver.industrial_inline_worker', str(spec), str(output)],
                    env=child_environment(1), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                deadline = time.monotonic() + 10
                try:
                    while process.poll() is None:
                        if time.monotonic() > deadline: raise ValueError('Reference time budget')
                        try:
                            owned = psutil.Process(process.pid)
                            if sum(child.memory_info().rss for child in [owned, *owned.children(recursive=True)] if child.is_running()) > 512 * 1024**2:
                                raise ValueError('Reference memory budget')
                        except psutil.NoSuchProcess: pass
                        if output.exists() and output.stat().st_size > MAX_CHECK_BYTES: raise ValueError('Reference output budget')
                        time.sleep(.02)
                    if process.returncode or not output.is_file() or output.is_symlink() or output.stat().st_size > MAX_CHECK_BYTES:
                        raise ValueError('Reference child rejected input')
                    result = json.loads(output.read_text(encoding='utf-8'))
                    if type(result.get('passed')) is not bool: raise ValueError('Reference child schema')
                    canonical(result)
                finally:
                    if process.poll() is None: self.gateway._kill_tree(process)
                    process.wait(timeout=5)
            return [provenance, _gate('reference_comparison', 'PASS' if result['passed'] else 'FAIL', 'INDEPENDENT_REFERENCE_COMPARISON', **{key: value for key, value in result.items() if key not in {'reference_eigenvalues', 'candidate_eigenvalues'}})], result
        except ServiceError as exc:
            status = 'UNVERIFIED' if 'RESOURCE_LIMIT' in exc.code or 'MISSING' in exc.code else 'FAIL'
            return [provenance, _gate('reference_comparison', status, exc.code)], None
        except (OSError, ValueError, TypeError, MemoryError, subprocess.SubprocessError, psutil.Error):
            return [provenance, _gate('reference_comparison', 'UNVERIFIED', 'REFERENCE_RESOURCE_OR_DATA_LIMIT')], None

    def _performance(self, dossier):
        snapshots = dossier['snapshots']; candidate = snapshots['candidate']; baseline = snapshots['baseline']
        criteria = dossier['manifest']['criteria']
        metrics = candidate.get('metrics') or {}
        checks = []
        for criterion, metric in [('max_walltime_seconds', 'walltime_seconds'), ('max_peak_memory_bytes', 'peak_rss_tree_bytes')]:
            if criteria[criterion] is None: continue
            value = metrics.get(metric)
            if not _finite(value): checks.append(('UNVERIFIED', metric + '_UNAVAILABLE'))
            elif criterion == 'max_peak_memory_bytes' and metrics.get('peak_is_lower_bound'): checks.append(('UNVERIFIED', 'PEAK_IS_ONLY_LOWER_BOUND'))
            else: checks.append(('PASS' if value <= criteria[criterion] else 'FAIL', metric + '_LIMIT'))
        speedup = None
        if criteria['min_speedup'] is not None:
            protected = ('problem', 'target', 'validation', 'resources', 'output')
            comparable = (baseline is not None and baseline['id'] != candidate['id'] and
                baseline.get('matrix_hash') == candidate.get('matrix_hash') and
                all((baseline.get('config') or {}).get(key) == (candidate.get('config') or {}).get(key) for key in protected) and
                bool((candidate.get('runtime') or {}).get('hardware_id')) and baseline.get('runtime') == candidate.get('runtime') and
                all(job.get('status') == 'SUCCEEDED' and ((job.get('result') or {}).get('validation') or {}).get('target_certification') == 'CERTIFIED' and
                    ((job.get('result') or {}).get('validation') or {}).get('numerical_status') == 'PASSED' for job in (candidate, baseline)))
            if comparable:
                live = self.gateway.get_job(baseline['id'])
                alternate = {**dossier, 'snapshots': {**snapshots, 'candidate': baseline, 'publication': snapshots['baseline_publication']}}
                integrity, baseline_folder = self._artifacts(alternate)
                comparable = self._snapshot(live) == baseline and integrity['status'] == 'PASS'
                if comparable:
                    from .validation_process import verify_bounded
                    matrix = self.gateway._get('matrices', baseline['matrix_id'])
                    verified = verify_bounded(matrix['_path'], baseline_folder, baseline['config'])
                    comparable = bool(verified and verified.get('numerical_status') == 'PASSED' and verified.get('target_certification') == 'CERTIFIED')
            before = (baseline.get('metrics') or {}).get('walltime_seconds') if baseline else None
            after = metrics.get('walltime_seconds')
            if comparable and _finite(before, True) and _finite(after, True):
                speedup = before / after
                checks.append(('PASS' if speedup >= criteria['min_speedup'] else 'FAIL', 'COMPARABLE_RECORDED_TIMINGS'))
            else: checks.append(('UNVERIFIED', 'FAIR_BASELINE_UNAVAILABLE'))
        if not checks: return _gate('performance', 'PASS', 'NO_PERFORMANCE_CLAIM_REQUESTED', speedup=None)
        status = 'FAIL' if any(item[0] == 'FAIL' for item in checks) else 'UNVERIFIED' if any(item[0] == 'UNVERIFIED' for item in checks) else 'PASS'
        return _gate('performance', status, 'DECLARED_RESOURCE_AND_TIMING_LIMITS', checks=[{'status': status, 'reason': reason} for status, reason in checks], speedup=speedup)

    def check(self, case_id):
        record = self._record('industrial_cases', case_id)
        self._project(record['project_id'], 'write')
        if not self.gateway._industrial_check_lock.acquire(blocking=False):
            raise ServiceError('INDUSTRIAL_CHECK_BUSY', 'One bounded acceptance check is already active', 409)
        try: return self._check(case_id)
        finally: self.gateway._industrial_check_lock.release()

    def _check(self, case_id):
        record = self._record('industrial_cases', case_id)
        self._project(record['project_id'], 'write')
        dossier = self._read(record, MAX_DOSSIER_BYTES)
        manifest, snapshots, binding = dossier['manifest'], dossier['snapshots'], dossier['binding']
        job = snapshots['candidate']; criteria = manifest['criteria']; model = snapshots['engineering']; physical = manifest['physical']
        gates = [_gate('declared_use', 'PASS' if manifest['declared_use'] == 'industrial' else 'UNVERIFIED',
            'INDUSTRIAL_DECLARATION' if manifest['declared_use'] == 'industrial' else 'TEACHING_OR_RESEARCH_ONLY')]
        binding_gate = self._binding_gate(dossier); gates.append(binding_gate)
        artifact_gate, folder = self._artifacts(dossier); gates.append(artifact_gate)
        validation = None
        if folder:
            from .validation_process import verify_bounded
            matrix = self.gateway._get('matrices', job['matrix_id'])
            validation = verify_bounded(matrix['_path'], folder, job['config'])
        if validation is None:
            gates.extend([_gate('residual', 'UNVERIFIED', 'FRESH_INDEPENDENT_VALIDATION_UNAVAILABLE'), _gate('target_coverage', 'UNVERIFIED', 'FRESH_TARGET_VALIDATION_UNAVAILABLE')])
        else:
            residuals = validation.get('residuals') or []
            maximum = max(residuals) if residuals and all(_finite(value) for value in residuals) else None
            passed = validation.get('numerical_status') == 'PASSED' and maximum is not None and maximum <= criteria['max_scaled_residual']
            gates.append(_gate('residual', 'PASS' if passed else 'FAIL', 'FRESH_ORIGINAL_PROBLEM_VALIDATION', residual_max=maximum, threshold=criteria['max_scaled_residual']))
            coverage = validation.get('target_certification') == 'CERTIFIED' and job['status'] == 'SUCCEEDED'
            gates.append(_gate('target_coverage', 'PASS' if coverage else 'UNVERIFIED', 'ORIGINAL_TARGET_CERTIFICATION', certification=validation.get('target_certification')))
        if job['status'] not in {'SUCCEEDED', 'PARTIAL'}: gates.append(_gate('job_outcome', 'FAIL', 'CANDIDATE_DID_NOT_SUCCEED', status_recorded=job['status']))
        physical_ready = bool(physical and physical['review'] and snapshots['matrix'].get('source') != 'synthetic' and
            (model is None or (model.get('model_kind') != 'analytic_demo' and not str((model.get('source') or {}).get('url', '')).startswith('urn:emsolver:'))))
        if manifest['acceptance_scope'] == 'resonant_frequencies':
            physical_ready = bool(physical_ready and model and physical['mesh'] and physical['mesh']['units'] == model.get('coordinate_units') and
                physical['boundary']['kind'] != 'unspecified' and all(material['conductivity_s_per_m'] == 0 for material in physical['materials']))
        physical_ready = physical_ready and binding_gate['status'] == 'PASS'
        if physical_ready:
            status, reason = verify_attestation(physical['attestation'], binding, 'physical_review', data_root=self.gateway.root)
            gates.append(_gate('physical_review', status, reason, evidence_basis='configured issuer assertion; mesh digest is declared, not independently reproduced'))
        else: gates.append(_gate('physical_review', 'UNVERIFIED', 'PHYSICAL_MODEL_OR_REVIEW_INCOMPLETE_OR_TEACHING_ONLY'))
        reference_gates, comparison = self._reference(dossier, folder); gates.extend(reference_gates)
        mapping = model.get('frequency_mapping') if model else None
        if manifest['acceptance_scope'] == 'matrix_eigenproblem':
            gates.append(_gate('frequency', 'PASS', 'NO_FREQUENCY_CLAIM_MATRIX_SCOPE'))
        elif mapping and manifest['reference'] and comparison and comparison.get('passed'):
            expected = frequency_values(comparison['reference_eigenvalues'], mapping)['frequencies_hz']
            values = comparison['candidate_eigenvalues']
            measured = frequency_values(values, mapping)['frequencies_hz']
            matches = comparison.get('matching', [])
            valid = len(expected) == len(measured) == len(matches) and all(value is not None for value in expected + measured)
            errors = [abs(expected[i] - measured[index]) / max(abs(expected[i]), abs(measured[index]), 1e-300) for i, index in enumerate(matches)] if valid else []
            passed = bool(errors) and max(errors) <= criteria['max_frequency_relative_error']
            gates.append(_gate('frequency', 'PASS' if passed else 'UNVERIFIED' if not valid else 'FAIL', 'FROZEN_REVIEWED_MAPPING_COMPARISON', max_relative_error=max(errors) if errors else None))
        else: gates.append(_gate('frequency', 'UNVERIFIED', 'REVIEWED_MAPPING_OR_REFERENCE_UNAVAILABLE'))
        gates.append(self._performance(dossier))
        # Recheck publication and input after children finish; changed evidence
        # must never inherit the successful checks from an earlier file version.
        if folder:
            final_integrity, _ = self._artifacts(dossier)
            if final_integrity['status'] != 'PASS':
                gates[gates.index(artifact_gate)] = final_integrity
        final_binding = self._binding_gate(dossier)
        if final_binding['status'] != 'PASS': gates[gates.index(binding_gate)] = final_binding
        status = 'FAIL' if any(gate['status'] == 'FAIL' for gate in gates) else 'UNVERIFIED' if any(gate['status'] == 'UNVERIFIED' for gate in gates) else 'PASS'
        result = {'schema_version': '1.0', 'id': 'ick_' + uuid.uuid4().hex, 'case_id': case_id, 'project_id': record['project_id'],
            'created_at': utcnow(), 'status': status, 'industrial_accepted': status == 'PASS', 'gates': gates,
            'dossier_sha256': record['dossier_sha256'], 'binding': binding,
            'acceptance_scope': manifest['acceptance_scope'],
            'claim_scope': 'Internal bounded ' + manifest['acceptance_scope'] + ' acceptance for this frozen input and configured issuer assertions; no official certification or independently proven physical truth.',
            'limitations': ['Reference evaluation is limited to supplied target modes: inline 50000 vector entries, external artifact limits are recorded with that asset.',
                'Physical mesh and material truth depend on the configured independent authority; hashes alone do not prove origin.',
                'Sampled process memory is not an exact operating system peak; unrequested speedup is never inferred.']}
        self._append('industrial_checks', result, {key: result[key] for key in ('id', 'project_id', 'case_id', 'created_at', 'status')}, MAX_CHECK_BYTES)
        return result
