"""Immutable, project-authorized engineering sidecars and measured field samples."""
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
import time
import uuid

import psutil

from .access import require_project
from .engineering_contracts import MAX_MANIFEST_BYTES, validate_manifest, frequency_values
from .errors import ServiceError
from .integrity import file_hash
from .process_environment import child_environment
from .store import utcnow


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


class EngineeringAssets:
    def __init__(self, gateway):
        self.gateway = gateway
        self.folder = Path(gateway.root) / 'engineering'

    def _matrix(self, matrix_id, write=False):
        matrix = self.gateway._get('matrices', matrix_id)
        project = self.gateway._project(matrix['project_id'], writable=write)
        if write: require_project(project, 'admin')
        return matrix, project

    @staticmethod
    def _public(record):
        return copy.deepcopy({key: value for key, value in record.items() if not key.startswith('_')})

    def get(self, asset_id):
        record = self.gateway.store.get('engineering_assets', asset_id)
        if not record: raise ServiceError('NOT_FOUND', status=404)
        matrix, _ = self._matrix(record['matrix_id'])
        if matrix['project_id'] != record['project_id'] or matrix['hash'] != record['matrix_hash']:
            raise ServiceError('ENGINEERING_BINDING_CHANGED')
        return record

    def _manifest(self, record):
        if record.get('availability') == 'MISSING': raise ServiceError('ENGINEERING_PAYLOAD_MISSING', status=409)
        digest = record.get('sidecar_sha256', '')
        if not re.fullmatch(r'[0-9a-f]{64}', digest): raise ServiceError('ENGINEERING_INTEGRITY_FAILED')
        path = self.folder / (digest + '.json')
        if (self.folder.is_symlink() or path.is_symlink() or not path.is_file() or
            str(path) != record.get('_path') or not path.resolve().is_relative_to(Path(self.gateway.root).resolve()) or
            path.stat().st_size > MAX_MANIFEST_BYTES or file_hash(path) != digest):
            raise ServiceError('ENGINEERING_INTEGRITY_FAILED')
        try: return json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError): raise ServiceError('ENGINEERING_INTEGRITY_FAILED') from None

    def manifest(self, asset_id):
        return self._manifest(self.get(asset_id))

    def list(self, matrix_id):
        matrix, _ = self._matrix(matrix_id)
        with self.gateway.store.lock:
            where = "kind='engineering_assets' AND project_id=? AND json_extract(body,'$.matrix_id')=?"
            parameters = (matrix['project_id'], matrix_id)
            rows = self.gateway.store.db.execute('SELECT body FROM entities WHERE ' + where + ' ORDER BY seq DESC LIMIT 100', parameters).fetchall()
            total = self.gateway.store.db.execute('SELECT count(*) FROM entities WHERE ' + where, parameters).fetchone()[0]
        return {'items': [self._public(json.loads(row[0])) for row in rows], 'selected_id': matrix.get('engineering_asset_id'), 'total': total, 'limit': 100}

    def _nullspace(self, matrix, manifest):
        if manifest['nullspace'] is None: return None
        source = Path(matrix['_path'])
        if source.is_symlink() or not source.is_file() or source.parent.resolve() != (Path(self.gateway.root) / 'matrices').resolve():
            raise ServiceError('ENGINEERING_INPUT_UNSAFE')
        with tempfile.TemporaryDirectory(prefix='engineering-', dir=Path(self.gateway.root) / 'inbox') as directory:
            spec, output = Path(directory) / 'spec.json', Path(directory) / 'result.json'
            spec.write_bytes(canonical({'matrix_path': str(source), 'matrix_storage': matrix['storage'], 'manifest': manifest}))
            spec.chmod(0o600)
            process = subprocess.Popen([sys.executable, '-m', 'emsolver.engineering_worker', str(spec), str(output)],
                env=child_environment(1), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline = time.monotonic() + 20
            try:
                while process.poll() is None:
                    if time.monotonic() > deadline: raise ServiceError('ENGINEERING_VERIFICATION_TIMEOUT')
                    try:
                        parent = psutil.Process(process.pid)
                        memory = sum(item.memory_info().rss for item in [parent, *parent.children(recursive=True)] if item.is_running())
                        if memory > 1024**3: raise ServiceError('ENGINEERING_VERIFICATION_RESOURCE_LIMIT')
                    except psutil.NoSuchProcess: pass
                    if output.exists() and output.stat().st_size > 1024**2: raise ServiceError('ENGINEERING_VERIFICATION_RESOURCE_LIMIT')
                    time.sleep(.025)
                if process.returncode or not output.is_file() or output.is_symlink() or output.stat().st_size > 1024**2:
                    raise ServiceError('ENGINEERING_NULLSPACE_REJECTED', 'Sparse basis, independence or original-input validation failed')
                result = json.loads(output.read_text())
                if result.get('status') not in {'VERIFIED', 'UNVERIFIED'} or result.get('basis_count') != len(manifest['nullspace']['basis']):
                    raise ServiceError('ENGINEERING_NULLSPACE_REJECTED')
                canonical(result)
                return result
            finally:
                if process.poll() is None:
                    self.gateway._kill_tree(process)
                process.wait(timeout=5)

    def create(self, matrix_id, value):
        matrix, project = self._matrix(matrix_id, True)
        try:
            if len(canonical(value)) > MAX_MANIFEST_BYTES: raise ServiceError('ENGINEERING_MANIFEST_TOO_LARGE', status=413)
        except (ValueError, TypeError, OverflowError): raise ServiceError('INVALID_ENGINEERING_MANIFEST') from None
        manifest = validate_manifest(value, matrix)
        if manifest['public'] and project.get('classification') == 'confidential': raise ServiceError('PROJECT_CONFIDENTIAL')
        data = canonical(manifest)
        if len(data) > MAX_MANIFEST_BYTES: raise ServiceError('ENGINEERING_MANIFEST_TOO_LARGE', status=413)
        digest = hashlib.sha256(data).hexdigest()
        report = self._nullspace(matrix, manifest)
        with self.gateway._mutex, self.gateway.store.transaction():
            matrix, _ = self._matrix(matrix_id, True)
            if matrix['hash'] != manifest['matrix_sha256']: raise ServiceError('ENGINEERING_BINDING_CHANGED')
            same = self.gateway.store.db.execute(
                "SELECT body FROM entities WHERE kind='engineering_assets' AND project_id=? AND json_extract(body,'$.matrix_id')=? AND json_extract(body,'$.sidecar_sha256')=? LIMIT 1",
                (matrix['project_id'], matrix_id, digest)).fetchone()
            if same:
                item = json.loads(same[0])
                self._manifest(item)
                return self._public(item)
            if self.folder.is_symlink(): raise ServiceError('ENGINEERING_INPUT_UNSAFE')
            self.folder.mkdir(exist_ok=True, mode=0o700)
            path = self.folder / (digest + '.json')
            if path.is_symlink(): raise ServiceError('ENGINEERING_INPUT_UNSAFE')
            if path.exists() and file_hash(path) != digest: raise ServiceError('ENGINEERING_INTEGRITY_FAILED')
            already_charged = self.gateway.store.db.execute(
                "SELECT 1 FROM entities WHERE kind='engineering_assets' AND project_id=? AND json_extract(body,'$.sidecar_sha256')=? AND coalesce(json_extract(body,'$.availability'),'READY') != 'MISSING' LIMIT 1",
                (matrix['project_id'], digest)).fetchone()
            charged_bytes = 0 if already_charged else len(data)
            self.gateway.governance.reserve(matrix['project_id'], input_bytes=charged_bytes)
            new_file = not path.exists()
            try:
                if new_file:
                    with path.open('xb') as handle: handle.write(data)
                    path.chmod(0o400)
                record = {'id': 'eng_' + uuid.uuid4().hex, 'schema_version': '1.0', 'project_id': matrix['project_id'], 'matrix_id': matrix_id,
                    'matrix_hash': matrix['hash'], 'sidecar_sha256': digest, 'bytes': len(data), 'created_at': utcnow(), '_path': str(path),
                    **{key: copy.deepcopy(manifest[key]) for key in ('model_version', 'model_kind', 'coordinate_units', 'field_units', 'source', 'license', 'authorized', 'public', 'frequency_mapping')},
                    'n_nodes': len(manifest['coordinates']), 'n_dofs': len(manifest['dof_nodes']), 'components': sorted(set(manifest['dof_components'])),
                    'availability': 'READY', 'nullspace_report': report,
                    'authorization_basis': 'explicit project administrator declaration; no independent legal or physical certification'}
                self.gateway.store.put('engineering_assets', record['id'], record)
                self.gateway.governance.add_bytes(matrix['project_id'], inputs=charged_bytes)
                self.gateway.store.audit('engineering.create', record['id'])
            except BaseException:
                if new_file: path.unlink(missing_ok=True)
                raise
        return self._public(record)

    def select(self, matrix_id, asset_id):
        with self.gateway._mutex, self.gateway.store.transaction():
            matrix, _ = self._matrix(matrix_id, True)
            record = self.get(asset_id)
            if record['matrix_id'] != matrix_id: raise ServiceError('ENGINEERING_BINDING_CHANGED')
            self._manifest(record)
            matrix.update(engineering_asset_id=asset_id, metadata_version=matrix.get('metadata_version', 1) + 1)
            matrix['version'] = matrix.get('version', 1) + 1
            self.gateway.store.put('matrices', matrix_id, matrix)
            self.gateway.store.audit('engineering.select', asset_id)
            return self._public(matrix)

    def freeze(self, matrix):
        asset_id = matrix.get('engineering_asset_id')
        if not asset_id: return None
        record = self.get(asset_id)
        if record['matrix_id'] != matrix['id']: raise ServiceError('ENGINEERING_BINDING_CHANGED')
        self._manifest(record)
        return {**self._public(record), 'kind': 'engineering_model_snapshot'}

    def create_demo(self, matrix_id):
        matrix, _ = self._matrix(matrix_id, True)
        if matrix.get('source') != 'synthetic' or matrix['n'] > 512: raise ServiceError('ENGINEERING_DEMO_REQUIRES_SMALL_SYNTHETIC_INPUT')
        n = matrix['n']
        return self.create(matrix_id, {'schema_version': '1.0', 'model_version': 'analytic-chain-v1', 'model_kind': 'analytic_demo',
            'matrix_sha256': matrix['hash'], 'authorized': True, 'public': False,
            'source': {'title': 'EMSolver generated analytic node chain; mapping demonstration, not a device mesh', 'url': 'urn:emsolver:analytic-chain:v1'},
            'license': 'CC0-1.0', 'coordinate_units': 'm', 'field_units': 'normalized',
            'coordinates': [[float(index), 0., 0.] for index in range(n)], 'dof_nodes': list(range(n)), 'dof_components': ['scalar'] * n,
            'cells': [{'kind': 'line', 'nodes': [index, index + 1]} for index in range(n - 1)]})

    def field_preview(self, job_id, mode=0, component='magnitude', limit=512):
        import numpy as np
        if type(mode) is not int or mode < 0 or type(limit) is not int or not 1 <= limit <= 512 or component not in {'scalar', 'x', 'y', 'z', 'magnitude'}:
            raise ServiceError('INVALID_FIELD_SELECTION')
        job = self.gateway._get('jobs', job_id)
        if job['status'] not in {'SUCCEEDED', 'PARTIAL'} or job.get('artifacts_state') in {'EXPIRED', 'MISSING'}: raise ServiceError('FIELD_RESULT_UNAVAILABLE', status=409)
        frozen = job.get('engineering_model')
        if not isinstance(frozen, dict) or not frozen.get('id'): raise ServiceError('ENGINEERING_MODEL_NOT_FROZEN', status=409)
        record = self.get(frozen['id'])
        if any(record.get(key) != frozen.get(key) for key in ('project_id', 'matrix_id', 'matrix_hash', 'sidecar_sha256')) or record['matrix_hash'] != job['matrix_hash'] or record['matrix_id'] != job['matrix_id'] or record['project_id'] != job['project_id']:
            raise ServiceError('ENGINEERING_BINDING_CHANGED')
        manifest = self._manifest(record)
        path = Path(self.gateway.download(job_id, 'eigenvectors.npy'))
        folder = path.parent
        publication = folder / 'manifest.json'
        if publication.is_symlink() or not publication.is_file() or publication.stat().st_size > 1024**2 or path.is_symlink() or path.stat().st_size > 128 * 1024**2:
            raise ServiceError('FIELD_PREVIEW_RESOURCE_OR_INTEGRITY_LIMIT')
        try:
            expected = json.loads(publication.read_text())['artifacts']['eigenvectors.npy']
            if file_hash(path) != expected: raise ValueError('Changed result')
            vectors = np.load(path, mmap_mode='r', allow_pickle=False)
            if vectors.ndim != 2 or vectors.shape[0] != record['n_dofs'] or mode >= vectors.shape[1] or vectors.dtype not in (np.dtype('float64'), np.dtype('complex128')): raise ValueError('Result shape')
        except (ValueError, OSError, KeyError): raise ServiceError('FIELD_RESULT_INTEGRITY_FAILED') from None
        node_dofs = {}
        for dof, (node, axis) in enumerate(zip(manifest['dof_nodes'], manifest['dof_components'])):
            node_dofs.setdefault(node, {})[axis] = dof
        eligible = sorted(node for node, axes in node_dofs.items() if component == 'magnitude' or component in axes)
        if not eligible: raise ServiceError('FIELD_COMPONENT_UNAVAILABLE')
        selected = [eligible[int(index)] for index in np.linspace(0, len(eligible) - 1, min(limit, len(eligible)), dtype=int)]
        points = []
        for node in selected:
            values = {axis: complex(vectors[dof, mode]) for axis, dof in node_dofs[node].items()}
            if any(not math.isfinite(value.real) or not math.isfinite(value.imag) for value in values.values()): raise ServiceError('FIELD_RESULT_INTEGRITY_FAILED')
            measured = {axis: {'real': value.real, 'imag': value.imag, 'amplitude': abs(value)} for axis, value in values.items()}
            magnitude = math.hypot(*(abs(value) for value in values.values()))
            if not math.isfinite(magnitude): raise ServiceError('FIELD_RESULT_INTEGRITY_FAILED')
            points.append({'node_id': node, 'coordinate': manifest['coordinates'][node], 'components': measured,
                'value': {'real': None, 'imag': None, 'amplitude': magnitude} if component == 'magnitude' else measured[component],
                'magnitude': magnitude, 'complete_vector': set(values) >= {'x', 'y', 'z'}})
        chosen = set(selected)
        cells = [cell for cell in manifest['cells'] if set(cell['nodes']) <= chosen][:1024]
        validation = (job.get('result') or {}).get('validation') or {}
        return {'job_id': job_id, 'mode': mode, 'component': component, 'points': points, 'cells': cells,
            'total_mapped_nodes': len(eligible), 'sample_limit': limit, 'cells_total': len(manifest['cells']),
            'engineering_model': self._public(record), 'coordinate_units': record['coordinate_units'], 'field_units': record['field_units'],
            'numerical_status': validation.get('numerical_status'), 'target_certification': validation.get('target_certification'),
            'interpretation': 'Coordinates and components from the frozen authorized model; available-component magnitude only, no invented missing components. Not additional physical or numerical certification.'}
