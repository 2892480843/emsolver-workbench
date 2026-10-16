import copy
import hashlib
import json
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from emsolver.access import Identity, identity_context
from emsolver.engineering import EngineeringAssets, frequency_values
from emsolver.errors import ServiceError
from emsolver.service import Service


@pytest.fixture
def prepared(tmp_path):
    service = Service(tmp_path / 'data', start_runner=False)
    project = service.create_project('Engineering private')
    matrix = service.generate_sample(project['id'], kind='near_singular', n=8)
    yield service, project, matrix, EngineeringAssets(service)
    service.close()


def manifest(matrix):
    n = matrix['n']
    return {'schema_version': '1.0', 'model_version': 'user-model-v1', 'matrix_sha256': matrix['hash'],
            'authorized': True, 'public': False, 'source': {'title': 'Authorized test model', 'url': 'https://example.org/model'},
            'license': 'LicenseRef-Private', 'coordinate_units': 'm', 'field_units': 'normalized',
            'coordinates': [[float(i), float(i % 2), 0.] for i in range(n)],
            'dof_nodes': list(range(n)), 'dof_components': ['scalar'] * n,
            'cells': [{'kind': 'line', 'nodes': [i, i + 1]} for i in range(n - 1)],
            'frequency_mapping': {'kind': 'omega_squared', 'eigenvalue_units': 's^-2', 'reviewed': True,
                                  'applicability': 'Explicit user model assertion; no device claim'},
            'nullspace': {'interpretation': 'expected_nullspace', 'basis': [{'indices': [4], 'real': [1.]}]}}


def test_immutable_sidecar_freeze_nullspace_and_future_only_selection(prepared):
    service, _, matrix, assets = prepared
    original = manifest(matrix)
    first = assets.create(matrix['id'], original)
    assert '_path' not in first
    assert first['nullspace_report']['status'] == 'VERIFIED'
    assert first['nullspace_report']['basis_count'] == 1
    selected = assets.select(matrix['id'], first['id'])
    frozen = assets.freeze(service.store.get('matrices', matrix['id']))
    assert frozen['sidecar_sha256'] == first['sidecar_sha256']
    changed = copy.deepcopy(original)
    changed['model_version'] = 'user-model-v2'
    changed['coordinates'][0][2] = 0.5
    second = assets.create(matrix['id'], changed)
    assets.select(matrix['id'], second['id'])
    assert frozen['id'] == first['id'] and first['sidecar_sha256'] != second['sidecar_sha256']
    assert assets.freeze(service.store.get('matrices', matrix['id']))['id'] == second['id']
    assert selected['engineering_asset_id'] == first['id']
    stored = service.store.get('engineering_assets', first['id'])
    assert Path(stored['_path']).stat().st_mode & 0o222 == 0
    assert service.project_usage(matrix['project_id'])['input_bytes'] >= stored['bytes']


@pytest.mark.parametrize('mutation', ['hash', 'nan', 'mapping', 'duplicate', 'cell', 'unknown', 'unauthorized', 'source_credentials'])
def test_invalid_manifests_never_create_assets(prepared, mutation):
    service, _, matrix, assets = prepared
    body = manifest(matrix)
    if mutation == 'hash': body['matrix_sha256'] = '0' * 64
    if mutation == 'nan': body['coordinates'][0][0] = float('nan')
    if mutation == 'mapping': body['dof_nodes'].pop()
    if mutation == 'duplicate': body['dof_nodes'][1] = body['dof_nodes'][0]
    if mutation == 'cell': body['cells'][0]['nodes'] = [0, 999]
    if mutation == 'unknown': body['executable'] = 'arbitrary'
    if mutation == 'unauthorized': body['authorized'] = False
    if mutation == 'source_credentials': body['source']['url'] = 'https://user:password@example.org/model?token=private'
    with pytest.raises(ServiceError): assets.create(matrix['id'], body)
    assert service.store.count('engineering_assets') == 0


def test_false_nullspace_is_reported_and_never_discards_modes(prepared):
    _, _, matrix, assets = prepared
    body = manifest(matrix)
    body['nullspace']['basis'][0]['indices'] = [0]
    result = assets.create(matrix['id'], body)
    assert result['nullspace_report']['status'] == 'UNVERIFIED'
    assert result['nullspace_report']['basis_count'] == 1
    assert result['nullspace_report']['automatic_removal'] is False


def test_field_preview_uses_frozen_coordinates_and_complex_components(prepared):
    service, project, matrix, assets = prepared
    body = manifest(matrix)
    body['dof_nodes'] = [i // 2 for i in range(8)]
    body['dof_components'] = ['x', 'y'] * 4
    body['nullspace'] = None
    model = assets.create(matrix['id'], body)
    assets.select(matrix['id'], model['id'])
    frozen = assets.freeze(service.store.get('matrices', matrix['id']))
    folder = service.root / 'runs' / 'j_field' / 'artifacts'
    folder.mkdir(parents=True)
    vectors = (np.arange(8.) + 1j * np.arange(8.)[::-1])[:, None]
    np.save(folder / 'eigenvectors.npy', vectors)
    digest = hashlib.sha256((folder / 'eigenvectors.npy').read_bytes()).hexdigest()
    (folder / 'manifest.json').write_text(json.dumps({'artifacts': {'eigenvectors.npy': digest}}))
    service.store.put('jobs', 'j_field', {'id': 'j_field', 'project_id': project['id'], 'matrix_id': matrix['id'],
        'matrix_hash': matrix['hash'], 'status': 'PARTIAL', 'engineering_model': frozen,
        'result': {'n_accepted': 1, 'validation': {'numerical_status': 'PASSED', 'target_certification': 'UNVERIFIED'}}})
    preview = assets.field_preview('j_field', mode=0, component='y', limit=2)
    assert len(preview['points']) == 2
    assert preview['points'][0]['coordinate'] == body['coordinates'][0]
    assert preview['points'][0]['components']['y']['real'] == 1.
    assert preview['points'][0]['components']['y']['imag'] == 6.
    assert preview['target_certification'] == 'UNVERIFIED'
    assert preview['points'][0]['complete_vector'] is False
    with pytest.raises(ServiceError): assets.field_preview('j_field', mode=2)
    with pytest.raises(ServiceError): assets.field_preview('j_field', limit=513)
    with identity_context(Identity('outsider')):
        with pytest.raises(ServiceError): assets.field_preview('j_field')
    (folder / 'eigenvectors.npy').write_bytes(b'changed')
    with pytest.raises(ServiceError): assets.field_preview('j_field')


def test_frequency_formulas_are_typed_bounded_and_preserve_zero():
    spec = {'kind': 'omega_squared', 'eigenvalue_units': 's^-2', 'reviewed': True, 'applicability': 'Known equation'}
    values = frequency_values([-1., 0., (2 * np.pi) ** 2], spec)['frequencies_hz']
    assert values == pytest.approx([None, 0., 1.], nan_ok=True)
    for invalid in [{**spec, 'kind': 'eval'}, {**spec, 'reviewed': False}, {**spec, 'eigenvalue_units': 'unknown'}, {**spec, 'code': 'run'}]:
        with pytest.raises(ServiceError): frequency_values([1.], invalid)
    wave = {**spec, 'kind': 'wavenumber_squared', 'eigenvalue_units': 'm^-2', 'wave_speed_m_per_s': 2.}
    assert frequency_values([(2 * np.pi) ** 2], wave)['frequencies_hz'] == pytest.approx([2.])


def test_router_enforces_admin_creation_and_bounds_body(prepared):
    service, project, matrix, _ = prepared
    from emsolver.api import create_app
    app = create_app(service.root, start_runner=False)
    with TestClient(app) as client:
        response = client.post('/api/v1/matrices/' + matrix['id'] + '/engineering', json={'manifest': manifest(matrix)})
        assert response.status_code == 201, response.text
        assert str(service.root) not in response.text
        assert client.get('/api/v1/engineering/' + response.json()['id'] + '/manifest').json()['matrix_sha256'] == matrix['hash']
        assert client.post('/api/v1/matrices/' + matrix['id'] + '/engineering', content=b'x' * (16 * 1024**2 + 1), headers={'Content-Type': 'application/json'}).status_code == 413
    with identity_context(Identity('outsider')):
        with pytest.raises(ServiceError): EngineeringAssets(service).create(matrix['id'], manifest(matrix))


def test_general_sparse_basis_and_dependent_declarations(prepared, tmp_path):
    from scipy import sparse
    service, project, _, assets = prepared
    original = sparse.eye(8, format='lil')
    original[0, 1] = original[1, 0] = 1.
    path = service.root / 'inbox' / 'original.npz'
    sparse.save_npz(path, original.tocsr())
    matrix = service.import_matrix(project['id'], path)
    body = manifest(matrix)
    body['nullspace']['basis'] = [{'indices': [0,1], 'real': [1.,-1.]}]
    verified = assets.create(matrix['id'], body)
    assert verified['nullspace_report']['status'] == 'VERIFIED'
    assert verified['nullspace_report']['residuals'] == pytest.approx([0.])
    body['nullspace']['basis'].append({'indices':[0,1], 'real':[2.,-2.]})
    with pytest.raises(ServiceError) as failure: assets.create(matrix['id'], body)
    assert failure.value.code == 'ENGINEERING_NULLSPACE_REJECTED'


def test_project_hash_dedup_and_missing_payload_fail_closed(prepared):
    service, project, matrix, assets = prepared
    body = manifest(matrix)
    body['nullspace'] = None
    first = assets.create(matrix['id'], body)
    used = service.project_usage(project['id'])['input_bytes']
    assert assets.create(matrix['id'], body)['id'] == first['id']
    duplicate = service.generate_sample(project['id'], kind='near_singular', n=8)
    assert duplicate['hash'] == matrix['hash']
    second = assets.create(duplicate['id'], body)
    assert second['id'] != first['id']
    assert second['sidecar_sha256'] == first['sidecar_sha256']
    assert service.project_usage(project['id'])['input_bytes'] == used
    with pytest.raises(ServiceError): assets.select(matrix['id'], second['id'])
    private = service.store.get('engineering_assets', first['id'])
    private['availability'] = 'MISSING'
    service.store.put('engineering_assets', first['id'], private)
    with pytest.raises(ServiceError) as failure: assets.manifest(first['id'])
    assert failure.value.code == 'ENGINEERING_PAYLOAD_MISSING'


def test_plan_and_job_freeze_prior_model_version(prepared):
    service, _, matrix, assets = prepared
    body = manifest(matrix)
    body['nullspace'] = None
    first = assets.create(matrix['id'], body)
    assets.select(matrix['id'], first['id'])
    plan = service.create_plan(config={'problem': {'matrix_id': matrix['id']}, 'target': {'k': 2, 'sigma': .1}})
    body['model_version'] = 'v2'
    second = assets.create(matrix['id'], body)
    assets.select(matrix['id'], second['id'])
    job = service.submit_job(plan['id'])
    assert job['engineering_model']['id'] == first['id']
    new_plan = service.create_plan(config=plan['config'])
    assert new_plan['engineering_model']['id'] == second['id']
    assert new_plan['hash'] != plan['hash']
    plan['engineering_model']['source']['title'] = 'changed client response'
    assert service.get_job(job['id'])['engineering_model']['source']['title'] != 'changed client response'


def test_admin_only_ingestion_and_authorized_viewer_http(prepared, tmp_path, monkeypatch):
    from emsolver.api import create_app
    service, project, matrix, assets = prepared
    service.update_project(project['id'], members=[{'subject':'viewer', 'role':'viewer'}, {'subject':'analyst','role':'analyst'}])
    body = manifest(matrix)
    body['nullspace'] = None
    created = assets.create(matrix['id'], body)
    identities = tmp_path / 'identity.json'
    identities.write_text(json.dumps({'users': [{'subject': name, 'token_sha256':hashlib.sha256(name.encode()).hexdigest(), 'global_admin': name == 'owner'} for name in ['owner','viewer','analyst','outsider']]}))
    monkeypatch.setenv('EMSOLVER_IDENTITIES_FILE', str(identities))
    with TestClient(create_app(service.root, start_runner=False)) as client:
        for name in ['viewer','analyst','outsider']:
            headers = {'Authorization': 'Bearer '+name}
            assert client.post('/api/v1/matrices/'+matrix['id']+'/engineering', content=b'invalid', headers=headers).status_code == 403
            assert client.post('/api/v1/matrices/'+matrix['id']+'/engineering/select', json={'asset_id':created['id']},headers=headers).status_code == 403
            assert client.post('/api/v1/matrices/'+matrix['id']+'/engineering/demo', json={'authorized':True},headers=headers).status_code == 403
        response = client.get('/api/v1/engineering/'+created['id']+'/manifest',headers={'Authorization':'Bearer viewer'})
        assert response.status_code == 200
        assert response.json()['coordinates'] == body['coordinates']
        assert str(service.root) not in response.text
        assert client.get('/api/v1/engineering/'+created['id']+'/manifest',headers={'Authorization':'Bearer outsider'}).status_code == 403
        assert client.post('/api/v1/matrices/'+matrix['id']+'/engineering', content=b'{"manifest":{},"manifest":{}}',headers={'Authorization':'Bearer owner'}).status_code == 400


def test_nullspace_uses_declared_original_triangle_storage(prepared):
    from scipy import sparse
    service, project, _, assets = prepared
    path = service.root / 'inbox' / 'triangle.npz'
    laplacian = sparse.diags([-np.ones(7), np.array([1.,2.,2.,2.,2.,2.,2.,1.]), -np.ones(7)], [-1,0,1],format='csr')
    sparse.save_npz(path, sparse.triu(laplacian,format='csr'))
    matrix = service.import_matrix(project['id'],path,storage='upper')
    body = manifest(matrix)
    body['nullspace']['basis'] = [{'indices':list(range(8)), 'real':[1.]*8}]
    assert assets.create(matrix['id'],body)['nullspace_report']['status'] == 'VERIFIED'


@pytest.mark.parametrize('kind,units,value,expected',[
    ('omega_squared','s^-2',(2*np.pi)**2,1.),
    ('angular_frequency','rad/s',2*np.pi,1.),
    ('frequency_hz','Hz',1.,1.),
    ('wavenumber_squared','m^-2',np.pi**2,1.),
])
def test_each_reviewed_frequency_equation_has_its_own_units_and_formula(kind,units,value,expected):
    spec={'kind':kind,'eigenvalue_units':units,'reviewed':True,'applicability':'Explicit test model'}
    if kind=='wavenumber_squared':spec['wave_speed_m_per_s']=2.
    assert frequency_values([0.,-1.,value],spec)['frequencies_hz']==pytest.approx([0.,None,expected])


def test_complex_sparse_nullspace_is_checked_against_original_hermitian_matrix(prepared):
    from scipy import sparse
    service,project,_,assets=prepared
    matrix=sparse.eye(8,dtype=np.complex128,format='lil')
    matrix[0,1]=-1j
    matrix[1,0]=1j
    path=service.root/'inbox'/'complex-kernel.npz'
    sparse.save_npz(path,matrix.tocsr())
    original=service.import_matrix(project['id'],path)
    declaration=manifest(original)
    declaration['nullspace']['basis']=[{'indices':[0,1],'real':[0.,1.],'imag':[1.,0.]}]
    report=assets.create(original['id'],declaration)['nullspace_report']
    assert report['status']=='VERIFIED' and report['automatic_removal'] is False


def test_analytic_demo_label_cannot_be_used_for_invented_geometry(prepared):
    _,_,matrix,assets=prepared
    demo=assets.create_demo(matrix['id'])
    data=assets.manifest(demo['id'])
    data['coordinates'][0][1]=1.
    with pytest.raises(ServiceError):assets.create(matrix['id'],data)
