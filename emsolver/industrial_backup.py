"""Pure validation of restored acceptance records; no paths are opened here."""
from __future__ import annotations

from datetime import datetime
import re

from .contracts import content_hash
from .engineering import canonical
from .errors import ServiceError
from .industrial_contracts import MAX_CHECK_BYTES, MAX_DOSSIER_BYTES, validate_manifest
from .jobs import TERMINAL
from .reporting import sanitize_public


CASE_FIELDS = {'id', 'schema_version', 'project_id', 'matrix_id', 'title', 'declared_use', 'candidate_job_id',
    'acceptance_scope', 'revision', 'parent_case_id', 'binding', 'created_at', 'dossier_sha256', 'bytes', '_path', 'availability'}
CHECK_FIELDS = {'id', 'schema_version', 'project_id', 'case_id', 'created_at', 'status', 'check_sha256', 'bytes', '_path', 'availability'}
BINDING_FIELDS = {'matrix_sha256', 'engineering_sha256', 'target_sha256', 'physical_sha256', 'reference_sha256',
    'candidate_job_id', 'baseline_job_id', 'declared_use', 'acceptance_scope', 'reference_asset_id', 'reference_asset_sha256'}
REQUIRED_GATES = {'declared_use', 'frozen_binding', 'artifacts', 'residual', 'target_coverage', 'physical_review',
    'reference_provenance', 'reference_comparison', 'frequency', 'performance'}


def _assert(condition):
    if not condition: raise ValueError('Invalid industrial backup record')


def _sha(value): return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def _binding(value):
    _assert(isinstance(value, dict) and set(value) == BINDING_FIELDS)
    _assert(all(_sha(value[key]) for key in ('matrix_sha256', 'target_sha256', 'physical_sha256', 'reference_sha256')))
    _assert(value['engineering_sha256'] is None or _sha(value['engineering_sha256']))
    _assert(value['declared_use'] in {'teaching', 'research', 'industrial'})
    _assert(value['acceptance_scope'] in {'matrix_eigenproblem', 'resonant_frequencies'})
    _assert((value['reference_asset_id'] is None and value['reference_asset_sha256'] is None) or
        (isinstance(value['reference_asset_id'], str) and re.fullmatch(r'ir_[0-9a-f]{32}', value['reference_asset_id']) and _sha(value['reference_asset_sha256'])))


def validate_backup_record(kind, item, payload, entity_lookup, *, payload_lookup=None):
    """Raise ValueError on inconsistency, including when a sidecar is missing.

    Historical check states are recomputed from all recorded gates. This checks
    internal consistency, not a new physical or numerical acceptance. Fresh
    acceptance still rechecks artifacts and current configured authorities.
    """
    try:
        _validate(kind, item, payload, entity_lookup, payload_lookup)
    except (KeyError, TypeError, ServiceError, OverflowError, RecursionError) as exc:
        raise ValueError('Invalid industrial backup record') from exc


def _validate(kind, item, payload, lookup, payload_lookup):
    _assert(kind in {'industrial_cases', 'industrial_checks'} and isinstance(item, dict))
    is_case = kind == 'industrial_cases'
    fields = CASE_FIELDS if is_case else CHECK_FIELDS
    _assert(set(item) == fields and item['schema_version'] == '1.0')
    _assert(isinstance(item['id'], str) and re.fullmatch(('ic_' if is_case else 'ick_') + r'[0-9a-f]{32}', item['id']))
    project = lookup('projects', item['project_id'])
    _assert(project is not None and project['id'] == item['project_id'])
    maximum = MAX_DOSSIER_BYTES if is_case else MAX_CHECK_BYTES
    _assert(type(item['bytes']) is int and 0 < item['bytes'] <= maximum and item['availability'] in {'READY', 'MISSING'})
    digest = item['dossier_sha256'] if is_case else item['check_sha256']
    _assert(_sha(digest) and isinstance(item['_path'], str) and item['_path'].replace('\\', '/').rsplit('/', 1)[-1] == digest + '.json')
    _assert(isinstance(item['created_at'], str) and len(item['created_at']) <= 64 and datetime.fromisoformat(item['created_at']).tzinfo is not None)
    if is_case:
        _binding(item['binding'])
        _assert(item['declared_use'] == item['binding']['declared_use'] and item['candidate_job_id'] == item['binding']['candidate_job_id'] and item['acceptance_scope'] == item['binding']['acceptance_scope'])
        _assert(isinstance(item['title'], str) and 1 <= len(item['title']) <= 200 and type(item['revision']) is int and item['revision'] >= 1)
        parent = lookup('industrial_cases', item['parent_case_id']) if item['parent_case_id'] else None
        _assert((item['parent_case_id'] is None and item['revision'] == 1) or
            (parent is not None and parent['project_id'] == item['project_id'] and item['revision'] == parent['revision'] + 1))
        matrix, candidate = lookup('matrices', item['matrix_id']), lookup('jobs', item['candidate_job_id'])
        _assert(matrix is not None and candidate is not None and matrix['project_id'] == candidate['project_id'] == item['project_id'])
        _assert(matrix['id'] == candidate['matrix_id'] and matrix['hash'] == candidate['matrix_hash'] == item['binding']['matrix_sha256'])
        _assert(candidate['status'] in TERMINAL and content_hash(candidate['config']['target']) == item['binding']['target_sha256'])
        if item['binding']['engineering_sha256'] is not None:
            frozen_model = candidate.get('engineering_model') or {}
            asset = lookup('engineering_assets', frozen_model.get('id'))
            _assert(asset is not None and asset['project_id'] == item['project_id'] and asset['matrix_id'] == matrix['id'] and
                asset['sidecar_sha256'] == frozen_model.get('sidecar_sha256') == item['binding']['engineering_sha256'])
        if item['binding']['reference_asset_id'] is not None:
            asset = lookup('industrial_reference_assets', item['binding']['reference_asset_id'])
            _assert(asset is not None and asset['project_id'] == item['project_id'] and asset['n'] == matrix['n'] and asset['sha256'] == item['binding']['reference_asset_sha256'])
        baseline_id = item['binding']['baseline_job_id']
        baseline = lookup('jobs', baseline_id) if baseline_id else None
        _assert(baseline_id is None or (baseline is not None and baseline['id'] != candidate['id'] and baseline['project_id'] == item['project_id'] and baseline['matrix_hash'] == matrix['hash'] and baseline['status'] in TERMINAL))
        if payload is None: return
        _assert(isinstance(payload, dict) and set(payload) == {'schema_version', 'manifest', 'binding', 'parent_case_id', 'revision', 'snapshots'})
        manifest = validate_manifest(payload['manifest'])
        _assert(payload['manifest'] == manifest and payload['schema_version'] == '1.0' and payload['binding'] == item['binding'])
        _assert(payload['parent_case_id'] == item['parent_case_id'] and payload['revision'] == item['revision'])
        for key in ('title', 'declared_use', 'matrix_id', 'candidate_job_id', 'acceptance_scope'): _assert(manifest[key] == item[key])
        _assert(manifest['baseline_job_id'] == baseline_id)
        snapshots = payload['snapshots']
        _assert(isinstance(snapshots, dict) and set(snapshots) == {'matrix', 'engineering', 'candidate', 'baseline', 'publication', 'reference_asset', 'baseline_publication'})
        _assert(snapshots['matrix'] == {key: matrix.get(key) for key in ('id', 'hash', 'n', 'source')})
        for key in ('id', 'project_id', 'matrix_id', 'matrix_hash', 'status', 'config'):
            _assert(snapshots['candidate'][key] == sanitize_public(candidate[key]))
        _assert((snapshots['baseline'] is None) == (baseline is None))
        if baseline:
            for key in ('id', 'project_id', 'matrix_id', 'matrix_hash', 'status', 'config'): _assert(snapshots['baseline'][key] == sanitize_public(baseline[key]))
        model = lookup('engineering_assets', manifest['engineering_asset_id']) if manifest['engineering_asset_id'] else None
        if model:
            _assert(model['project_id'] == item['project_id'] and model['matrix_id'] == matrix['id'] and model['matrix_hash'] == matrix['hash'])
            _assert(model['sidecar_sha256'] == item['binding']['engineering_sha256'])
            for key in ('id', 'sidecar_sha256', 'matrix_hash'):
                _assert(snapshots['engineering'][key] == model[key] == (candidate.get('engineering_model') or {}).get(key))
        else: _assert(manifest['engineering_asset_id'] is None and snapshots['engineering'] is None and item['binding']['engineering_sha256'] is None)
        unsigned = lambda value: {key: child for key, child in value.items() if key != 'attestation'} if value else None
        _assert(content_hash(unsigned(manifest['physical'])) == item['binding']['physical_sha256'])
        _assert(content_hash(unsigned(manifest['reference'])) == item['binding']['reference_sha256'])
        reference = manifest['reference']
        if reference:
            _assert(reference['matrix_sha256'] == matrix['hash'] and reference['target_sha256'] == item['binding']['target_sha256'])
            if reference['artifact_id']:
                asset = lookup('industrial_reference_assets', reference['artifact_id'])
                _assert(asset is not None and asset['project_id'] == item['project_id'] and asset['n'] == matrix['n'])
                _assert(asset['id'] == item['binding']['reference_asset_id'] and asset['sha256'] == item['binding']['reference_asset_sha256'] == reference['artifact_sha256'])
                for key in ('id', 'sha256', 'project_id', 'n', 'modes', 'bytes'): _assert(snapshots['reference_asset'][key] == asset[key])
            else: _assert(len(reference['vectors_real']) == matrix['n'] and snapshots['reference_asset'] is None and item['binding']['reference_asset_id'] is None)
        else: _assert(snapshots['reference_asset'] is None and item['binding']['reference_asset_id'] is None)
        publication = snapshots['publication']
        if publication:
            _assert(set(publication) == {'sha256', 'artifacts', 'status'} and _sha(publication['sha256']) and isinstance(publication['artifacts'], dict) and 1 <= len(publication['artifacts']) <= 64)
            _assert(all(isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', name) and _sha(sha) for name, sha in publication['artifacts'].items()))
    else:
        case = lookup('industrial_cases', item['case_id'])
        _assert(case is not None and case['project_id'] == item['project_id'] and item['status'] in {'PASS', 'FAIL', 'UNVERIFIED'})
        if payload is None: return
        fields = {'schema_version', 'id', 'case_id', 'project_id', 'created_at', 'status', 'industrial_accepted', 'gates',
            'dossier_sha256', 'binding', 'claim_scope', 'limitations', 'acceptance_scope'}
        _assert(isinstance(payload, dict) and set(payload) == fields and payload['schema_version'] == '1.0')
        for key in ('id', 'case_id', 'project_id', 'created_at', 'status'): _assert(payload[key] == item[key])
        _assert(payload['binding'] == case['binding'] and payload['dossier_sha256'] == case['dossier_sha256'])
        _assert(payload['acceptance_scope'] == case['acceptance_scope'])
        gates = payload['gates']
        _assert(isinstance(gates, list) and 10 <= len(gates) <= 11 and all(isinstance(gate, dict) and set(gate) == {'name', 'status', 'reason', 'details'} for gate in gates))
        names = [gate['name'] for gate in gates]
        _assert(len(set(names)) == len(names) and REQUIRED_GATES <= set(names) <= REQUIRED_GATES | {'job_outcome'})
        _assert(all(gate['status'] in {'PASS', 'FAIL', 'UNVERIFIED'} and isinstance(gate['reason'], str) and 1 <= len(gate['reason']) <= 256 and isinstance(gate['details'], dict) for gate in gates))
        status = 'FAIL' if any(gate['status'] == 'FAIL' for gate in gates) else 'UNVERIFIED' if any(gate['status'] == 'UNVERIFIED' for gate in gates) else 'PASS'
        _assert(item['status'] == status and payload['industrial_accepted'] is (status == 'PASS'))
        _assert(status != 'PASS' or case['declared_use'] == 'industrial')
        _assert(isinstance(payload['claim_scope'], str) and 1 <= len(payload['claim_scope']) <= 2000 and isinstance(payload['limitations'], list) and len(payload['limitations']) <= 32)
        case_payload = payload_lookup('industrial_cases', case['id']) if payload_lookup else None
        if status == 'PASS' and case_payload:
            manifest = case_payload['manifest']
            _assert(manifest['physical'] is not None and manifest['reference'] is not None)
            _assert(manifest['acceptance_scope'] != 'resonant_frequencies' or manifest['engineering_asset_id'] is not None)
            _assert(manifest['physical']['attestation'] is not None and manifest['reference']['attestation'] is not None)
    _assert(len(canonical(payload)) == item['bytes'] and content_hash(payload) == digest)
