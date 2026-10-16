"""Small real solves exercise acceptance; fixtures are not industrial evidence."""
import copy
import hashlib
import hmac
import json
from pathlib import Path

import numpy as np
import pytest
from scipy import sparse

from emsolver.access import Identity, identity_context
from emsolver.contracts import content_hash
from emsolver.engineering import EngineeringAssets, canonical
from emsolver.errors import ServiceError
from emsolver.industrial import IndustrialCases, attestation_message
from emsolver.service import Service


@pytest.fixture
def accepted_input(tmp_path):
    service = Service(tmp_path / 'data', start_runner=False)
    project = service.create_project('Acceptance fixture')
    path = service.root / 'inbox' / 'matrix.npz'
    sparse.save_npz(path, sparse.diags(np.arange(1., 9.), format='csr'))
    matrix = service.import_matrix(project['id'], path)
    model = EngineeringAssets(service).create(matrix['id'], {
        'schema_version': '1.0', 'model_version': 'fixture-v1', 'matrix_sha256': matrix['hash'],
        'authorized': True, 'source': {'title': 'Test fixture only', 'url': 'https://example.org/fixture'},
        'license': 'CC0-1.0', 'coordinate_units': 'm', 'field_units': 'normalized',
        'coordinates': [[float(i), 0., 0.] for i in range(8)], 'dof_nodes': list(range(8)),
        'dof_components': ['scalar'] * 8,
        'frequency_mapping': {'kind': 'frequency_hz', 'eigenvalue_units': 'Hz', 'reviewed': True,
                              'applicability': 'Test fixture only; no physical certification'}})
    EngineeringAssets(service).select(matrix['id'], model['id'])
    plan = service.create_plan(config={'problem': {'matrix_id': matrix['id']},
        'target': {'k': 2, 'sigma': 0.}, 'resources': {'threads': 1}})
    job = service.submit_job(plan['id'])
    service._execute(job['id'])
    job = service.get_job(job['id'])
    assert job['status'] == 'SUCCEEDED'
    manifest = {'schema_version': '1.0', 'title': 'Test workflow, not an industrial claim',
        'declared_use': 'industrial', 'matrix_id': matrix['id'], 'engineering_asset_id': model['id'],
        'candidate_job_id': job['id'], 'baseline_job_id': None,
        'physical': {'equation': 'Standard Hermitian test equation', 'assumptions': ['linear lossless media'],
            'materials': [{'name': 'vacuum', 'relative_permittivity': 1., 'relative_permeability': 1., 'conductivity_s_per_m': 0.}],
            'boundary': {'kind': 'PEC', 'description': 'Test walls'},
            'mesh': {'sha256': '1' * 64, 'generator': 'fixture v1', 'units': 'm',
                     'source': {'title': 'Fixture mesh', 'url': 'https://example.org/mesh'}},
            'review': {'source': {'title': 'Review fixture', 'url': 'https://example.org/review'}, 'artifact_sha256': '2' * 64},
            'attestation': None},
        'reference': {'matrix_sha256': matrix['hash'], 'target_sha256': content_hash(job['config']['target']),
            'method': 'independent_solver', 'producer': 'Independent test fixture',
            'source': {'title': 'Reference fixture', 'url': 'https://example.org/reference'},
            'artifact_sha256': '3' * 64, 'eigenvalues': [1., 2.],
            'vectors_real': np.eye(8)[:, :2].tolist(), 'vectors_imag': None, 'attestation': None},
        'criteria': {}}
    try: yield service, project, matrix, job, manifest, IndustrialCases(service)
    finally: service.close()


def sign_manifest(ledger, project_id, manifest, monkeypatch, tmp_path):
    key = b'fixed independent test authority key'
    keyfile = tmp_path / 'authority.json'
    keyfile.write_text(json.dumps({'keys': [{'issuer': 'test-lab', 'key_hex': key.hex(),
                                          'claims': ['reference', 'physical_review']}]}))
    keyfile.chmod(0o600)
    monkeypatch.setenv('EMSOLVER_INDUSTRIAL_ATTESTATION_KEYS_FILE', str(keyfile))
    draft = ledger.create(project_id, manifest)
    binding = draft['binding']
    for part, purpose in [('reference', 'reference'), ('physical', 'physical_review')]:
        manifest[part]['attestation'] = {'issuer': 'test-lab',
            'signature': hmac.new(key, attestation_message(binding, purpose), hashlib.sha256).hexdigest()}
    return manifest


def gate(check, name): return next(item for item in check['gates'] if item['name'] == name)


def test_unsigned_evidence_is_unverified_and_frozen(accepted_input):
    service, project, _, _, manifest, ledger = accepted_input
    case = ledger.create(project['id'], manifest)
    checked = ledger.check(case['id'])
    assert checked['status'] == 'UNVERIFIED'
    assert checked['industrial_accepted'] is False
    assert gate(checked, 'residual')['status'] == 'PASS'
    assert gate(checked, 'reference_comparison')['status'] == 'PASS'
    assert gate(checked, 'reference_provenance')['status'] == 'UNVERIFIED'
    assert gate(checked, 'physical_review')['status'] == 'UNVERIFIED'
    frozen = ledger.dossier(case['id'])
    manifest['physical']['assumptions'].append('changed')
    assert ledger.dossier(case['id']) == frozen
    assert ledger.create(project['id'], frozen['manifest'])['id'] == case['id']
    record = service.store.get('industrial_cases', case['id'])
    assert Path(record['_path']).stat().st_mode & 0o222 == 0
    assert '_path' not in json.dumps(ledger.export(case['id']))


def test_configured_independent_attestations_bind_all_evidence(accepted_input, monkeypatch, tmp_path):
    _, project, _, _, manifest, ledger = accepted_input
    manifest = sign_manifest(ledger, project['id'], manifest, monkeypatch, tmp_path)
    case = ledger.create(project['id'], manifest)
    result = ledger.check(case['id'])
    assert result['status'] == 'PASS', result
    assert result['industrial_accepted'] is True
    assert 'configured' in result['claim_scope']
    altered = copy.deepcopy(manifest)
    altered['physical']['materials'][0]['relative_permittivity'] = 2.
    revision = ledger.revise(case['id'], altered)
    assert revision['revision'] == 2 and revision['parent_case_id'] == case['id']
    rejected = ledger.check(revision['id'])
    assert rejected['status'] == 'FAIL'
    assert gate(rejected, 'reference_provenance')['reason'] == 'INVALID_ATTESTATION'
    assert ledger.check_record(result['id']) == result


def test_missing_reference_and_teaching_cannot_be_industrially_accepted(accepted_input, monkeypatch, tmp_path):
    _, project, _, _, manifest, ledger = accepted_input
    manifest = sign_manifest(ledger, project['id'], manifest, monkeypatch, tmp_path)
    manifest['declared_use'] = 'teaching'
    manifest['physical']['attestation'] = None
    manifest['reference']['attestation'] = None
    result = ledger.check(ledger.create(project['id'], manifest)['id'])
    assert result['industrial_accepted'] is False
    assert result['status'] == 'UNVERIFIED'
    manifest['declared_use'] = 'industrial'
    manifest['reference'] = None
    result = ledger.check(ledger.create(project['id'], manifest)['id'])
    assert result['industrial_accepted'] is False
    assert gate(result, 'reference_comparison')['status'] == 'UNVERIFIED'


def test_wrong_reference_direction_and_unfair_performance_fail_closed(accepted_input):
    service, project, _, job, manifest, ledger = accepted_input
    manifest['reference']['vectors_real'] = np.eye(8)[:, [0, 0]].tolist()
    manifest['baseline_job_id'] = job['id']
    with pytest.raises(ServiceError): ledger.create(project['id'], manifest)
    manifest['baseline_job_id'] = None
    manifest['criteria']['min_speedup'] = 1.
    result = ledger.check(ledger.create(project['id'], manifest)['id'])
    assert gate(result, 'reference_comparison')['status'] == 'FAIL'
    assert gate(result, 'performance')['status'] == 'UNVERIFIED'
    assert result['status'] == 'FAIL'
    stored = service.store.get('industrial_checks', result['id'])
    assert stored['bytes'] < 1024**2


def test_project_roles_integrity_and_archived_writes(accepted_input):
    service, project, _, job, manifest, ledger = accepted_input
    service.update_project(project['id'], members=[{'subject': 'reader', 'role': 'viewer'}, {'subject': 'analyst', 'role': 'analyst'}])
    case = ledger.create(project['id'], manifest)
    with identity_context(Identity('reader')):
        assert ledger.get(case['id'])['id'] == case['id']
        with pytest.raises(ServiceError): ledger.create(project['id'], manifest)
        with pytest.raises(ServiceError): ledger.check(case['id'])
    with identity_context(Identity('outsider', operations_admin=True)):
        for call in [lambda: ledger.get(case['id']), lambda: ledger.list(project['id']), lambda: ledger.export(case['id'])]:
            with pytest.raises(ServiceError): call()
    with identity_context(Identity('analyst')):
        assert ledger.check(case['id'])['case_id'] == case['id']
    folder = service.root / 'runs' / job['id'] / 'artifacts'
    with (folder / 'eigenvalues.npy').open('ab') as output: output.write(b'tamper')
    assert gate(ledger.check(case['id']), 'artifacts')['status'] == 'FAIL'
    service.update_project(project['id'], archived=True)
    with pytest.raises(ServiceError): ledger.revise(case['id'], manifest)
    record = service.store.get('industrial_cases', case['id'])
    path = Path(record['_path']); path.chmod(0o600); path.write_bytes(b'{}')
    with pytest.raises(ServiceError): ledger.dossier(case['id'])


@pytest.mark.parametrize('field', ['unknown', 'nan', 'reference_target', 'reference_matrix', 'shape', 'private_path'])
def test_closed_manifest_rejects_invalid_evidence(accepted_input, field):
    _, project, _, _, manifest, ledger = accepted_input
    if field == 'unknown': manifest['execute'] = 'anything'
    if field == 'nan': manifest['physical']['materials'][0]['relative_permittivity'] = float('nan')
    if field == 'reference_target': manifest['reference']['target_sha256'] = '0' * 64
    if field == 'reference_matrix': manifest['reference']['matrix_sha256'] = '0' * 64
    if field == 'shape': manifest['reference']['vectors_real'].pop()
    if field == 'private_path': manifest['physical']['equation'] = '/Users/secret/private.npz'
    with pytest.raises(ServiceError): ledger.create(project['id'], manifest)


def test_template_without_engineering_and_reference_creates_honest_matrix_only_case(accepted_input):
    _, project, _, job, _, ledger = accepted_input
    template = ledger.template(project['id'], job['id'])
    manifest = template['manifest']
    manifest['engineering_asset_id'] = None
    manifest['acceptance_scope'] = 'resonant_frequencies'
    case = ledger.create(project['id'], manifest)
    result = ledger.check(case['id'])
    assert result['status'] == 'UNVERIFIED'
    assert gate(result, 'residual')['status'] == 'PASS'
    assert gate(result, 'physical_review')['status'] == 'UNVERIFIED'
    assert gate(result, 'frequency')['status'] == 'UNVERIFIED'


def test_backup_validator_rejects_inflated_results_and_cross_project_bindings(accepted_input):
    from emsolver.industrial_backup import validate_backup_record
    service, project, _, _, manifest, ledger = accepted_input
    case = ledger.create(project['id'], manifest)
    check = ledger.check(case['id'])
    lookup = service.store.get
    for kind, identity, payload in [('industrial_cases', case['id'], ledger.dossier(case['id'])),
                                     ('industrial_checks', check['id'], check)]:
        record = lookup(kind, identity)
        validate_backup_record(kind, record, payload, lookup)
        validate_backup_record(kind, record, None, lookup)
        bad = copy.deepcopy(record); bad['bytes'] = -1
        with pytest.raises(ValueError): validate_backup_record(kind, bad, None, lookup)
    record = lookup('industrial_checks', check['id'])
    inflated = copy.deepcopy(check); inflated['industrial_accepted'] = True
    record.update(check_sha256=content_hash(inflated), bytes=len(canonical(inflated)))
    record['_path'] = str(service.root / 'industrial' / (record['check_sha256'] + '.json'))
    with pytest.raises(ValueError): validate_backup_record('industrial_checks', record, inflated, lookup)


def test_api_template_create_check_export_and_request_budgets(accepted_input):
    from fastapi.testclient import TestClient
    from emsolver.api import create_app
    from emsolver.industrial_api import router
    service, project, _, job, _, _ = accepted_input
    app = create_app(service.root, start_runner=False)
    app.include_router(router)
    with TestClient(app) as client:
        base = '/api/v1/projects/' + project['id']
        response = client.get(base + '/industrial-template', params={'job_id': job['id']})
        assert response.status_code == 200, response.text
        manifest = response.json()['manifest']
        created = client.post(base + '/industrial-cases', json={'manifest': manifest})
        assert created.status_code == 201, created.text
        path = '/api/v1/industrial-cases/' + created.json()['id']
        checked = client.post(path + '/checks')
        assert checked.status_code == 201 and checked.json()['status'] == 'UNVERIFIED'
        assert client.get(path).json()['latest_check']['id'] == checked.json()['id']
        assert client.get(path + '/report').json()['dossier']['manifest'] == manifest
        assert client.get(path + '/checks').json()['items'][0]['id'] == checked.json()['id']
        assert str(service.root) not in client.get(path + '/report').text
        assert client.post(base + '/industrial-cases', content=b'x' * (1024**2 + 1)).status_code == 413
        assert client.post(base + '/industrial-cases', content='{"manifest":{},"manifest":{}}').status_code == 400
        assert client.get(base + '/industrial-cases', params={'limit': 101}).status_code == 422


def test_authority_keys_in_business_data_are_never_trusted(accepted_input, monkeypatch):
    service, project, _, _, manifest, ledger = accepted_input
    keyfile = service.root / 'industrial' / 'authority.json'
    keyfile.parent.mkdir(exist_ok=True)
    keyfile.write_text(json.dumps({'keys': [{'issuer': 'test-lab', 'key_hex': '11' * 32, 'claims': ['reference', 'physical_review']}]}))
    keyfile.chmod(0o600)
    monkeypatch.setenv('EMSOLVER_INDUSTRIAL_ATTESTATION_KEYS_FILE', str(keyfile))
    manifest['physical']['attestation'] = {'issuer': 'test-lab', 'signature': '0' * 64}
    result = ledger.check(ledger.create(project['id'], manifest)['id'])
    assert gate(result, 'physical_review')['status'] == 'UNVERIFIED'
    assert gate(result, 'physical_review')['reason'] == 'TRUST_AUTHORITY_UNAVAILABLE'


def test_concurrent_checks_are_bounded_and_matrix_scope_does_not_claim_frequency(accepted_input):
    service, project, _, _, manifest, ledger = accepted_input
    manifest['engineering_asset_id'] = None
    case = ledger.create(project['id'], manifest)
    service._industrial_check_lock.acquire()
    try:
        with pytest.raises(ServiceError) as failure: ledger.check(case['id'])
        assert failure.value.code == 'INDUSTRIAL_CHECK_BUSY'
    finally: service._industrial_check_lock.release()
    result = ledger.check(case['id'])
    assert gate(result, 'frequency')['reason'] == 'NO_FREQUENCY_CLAIM_MATRIX_SCOPE'


def test_complete_large_vectors_use_external_asset_without_geometry(accepted_input, monkeypatch, tmp_path):
    """50001-row diagonal fixture proves the path, never device correctness."""
    from emsolver.industrial_reference import IndustrialReferenceAssets
    from emsolver.industrial_backup import validate_backup_record
    service, project, _, _, original, ledger = accepted_input
    n = 50001
    source = service.root / 'inbox' / 'large-diagonal.npz'
    sparse.save_npz(source, sparse.diags(np.arange(1., n + 1.), format='csr'))
    matrix = service.import_matrix(project['id'], source)
    plan = service.create_plan(config={'problem': {'matrix_id': matrix['id']},
        'target': {'k': 2, 'sigma': 0.}, 'resources': {'threads': 1}})
    submitted = service.submit_job(plan['id']); service._execute(submitted['id'])
    job = service.get_job(submitted['id'])
    assert job['status'] == 'SUCCEEDED'
    basis = np.zeros((n, 2)); basis[0, 0] = 1.; basis[1, 1] = 1.
    reference_file = service.root / 'inbox' / 'independent-fixture.npz'
    np.savez(reference_file, eigenvalues=np.array([1., 2.]), eigenvectors=basis)
    assets = IndustrialReferenceAssets(service)
    asset = assets.create(project['id'], reference_file)
    manifest = copy.deepcopy(original)
    manifest.update(matrix_id=matrix['id'], engineering_asset_id=None, candidate_job_id=job['id'],
                    acceptance_scope='matrix_eigenproblem')
    manifest['physical']['mesh'] = None
    manifest['reference'].update(matrix_sha256=matrix['hash'], target_sha256=content_hash(job['config']['target']),
        artifact_id=asset['id'], artifact_sha256=asset['sha256'], eigenvalues=None, vectors_real=None, vectors_imag=None)
    manifest = sign_manifest(ledger, project['id'], manifest, monkeypatch, tmp_path)
    case = ledger.create(project['id'], manifest)
    result = ledger.check(case['id'])
    assert result['status'] == 'PASS', result
    assert result['industrial_accepted'] is True and result['acceptance_scope'] == 'matrix_eigenproblem'
    assert gate(result, 'frequency')['reason'] == 'NO_FREQUENCY_CLAIM_MATRIX_SCOPE'
    assert case['binding']['reference_asset_sha256'] == asset['sha256']
    assert ledger.dossier(case['id'])['snapshots']['reference_asset']['n'] == n
    assert '_path' not in json.dumps(ledger.dossier(case['id']))
    for kind, identity, payload in [('industrial_cases', case['id'], ledger.dossier(case['id'])), ('industrial_checks', result['id'], result)]:
        validate_backup_record(kind, service.store.get(kind, identity), payload, service.store.get,
            payload_lookup=lambda kind, identity: ledger.dossier(identity) if kind == 'industrial_cases' else None)
    reference = assets.path(asset['id']); reference.chmod(0o600)
    with reference.open('ab') as output: output.write(b'tamper')
    changed = ledger.check(case['id'])
    assert changed['status'] != 'PASS' and changed['industrial_accepted'] is False


def test_frequency_profile_rechecks_frozen_geometry_payload(accepted_input, monkeypatch, tmp_path):
    service, project, _, _, manifest, ledger = accepted_input
    manifest['acceptance_scope'] = 'resonant_frequencies'
    manifest = sign_manifest(ledger, project['id'], manifest, monkeypatch, tmp_path)
    case = ledger.create(project['id'], manifest)
    assert ledger.check(case['id'])['status'] == 'PASS'
    model = service.store.get('engineering_assets', manifest['engineering_asset_id'])
    path = Path(model['_path']); path.chmod(0o600); path.write_bytes(b'changed geometry')
    result = ledger.check(case['id'])
    assert result['status'] == 'FAIL' and result['industrial_accepted'] is False
    assert gate(result, 'frozen_binding')['status'] == 'FAIL'
