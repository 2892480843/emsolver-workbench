"""Independent reference assets stay bounded, immutable and project scoped."""
import io
import json
from pathlib import Path
import stat
import zipfile

import numpy as np
import pytest

from emsolver.access import Identity, identity_context
from emsolver.errors import ServiceError
from emsolver.industrial_reference import IndustrialReferenceAssets, header_metadata, validate_record
from emsolver.service import Service


@pytest.fixture
def ledger(tmp_path):
    service = Service(tmp_path / 'data', start_runner=False)
    project = service.create_project('Independent references')
    try:
        yield service, project, IndustrialReferenceAssets(service)
    finally:
        service.close()


def reference_file(service, n=60001, modes=2, **arrays):
    path = service.root / 'inbox' / 'reference.npz'
    vectors = np.zeros((n, modes), dtype=np.float64)
    for column in range(modes): vectors[column, column] = 1.
    np.savez_compressed(path, eigenvalues=np.arange(1., modes + 1.), eigenvectors=vectors, **arrays)
    return path


def test_large_reference_registration_is_immutable_project_scoped_and_deduplicated(ledger):
    service, project, assets = ledger
    source = reference_file(service)
    item = assets.create(project['id'], source)
    assert item['n'] == 60001 and item['modes'] == 2
    assert '_path' not in item
    assert assets.create(project['id'], source)['id'] == item['id']
    path = assets.path(item['id'])
    assert path.stat().st_mode & 0o222 == 0
    assert validate_record(assets.record(item['id']), path)['n'] == 60001
    with identity_context(Identity('outsider')):
        with pytest.raises(ServiceError): assets.get(item['id'])
    service.update_project(project['id'], members=[{'subject': 'reader', 'role': 'viewer'}])
    with identity_context(Identity('reader')):
        assert assets.get(item['id'])['sha256'] == item['sha256']
        with pytest.raises(ServiceError): assets.create(project['id'], source)


@pytest.mark.parametrize('extra', ['extra', 'object', 'nan'])
def test_nonfinite_object_or_extra_arrays_are_rejected(ledger, extra):
    service, project, assets = ledger
    source = reference_file(service, n=8)
    if extra == 'extra': np.savez(source, eigenvalues=[1.], eigenvectors=np.ones((8, 1)), extra=[1])
    elif extra == 'object': np.savez(source, eigenvalues=[1.], eigenvectors=np.array([[object()]], dtype=object))
    else: np.savez(source, eigenvalues=[1.], eigenvectors=np.array([[np.nan]]))
    with pytest.raises(ServiceError): assets.create(project['id'], source)
    assert not service.store.select('industrial_reference_assets', project_id=project['id'])


def test_container_rejects_duplicate_symlink_and_oversized_declared_shape(tmp_path):
    raw = io.BytesIO()
    np.save(raw, np.ones((1, 1)))
    for kind in ('duplicate', 'symlink', 'shape'):
        path = tmp_path / (kind + '.npz')
        with zipfile.ZipFile(path, 'w') as output:
            output.writestr('eigenvalues.npy', raw.getvalue())
            if kind == 'duplicate': output.writestr('eigenvalues.npy', raw.getvalue())
            info = zipfile.ZipInfo('eigenvectors.npy')
            if kind == 'symlink': info.external_attr = (stat.S_IFLNK | 0o777) << 16
            if kind == 'shape':
                header = io.BytesIO()
                np.lib.format.write_array_header_1_0(header, {'descr': '<f8', 'fortran_order': False, 'shape': (2000001, 1)})
                value = header.getvalue()
            else: value = raw.getvalue()
            output.writestr(info, value)
        with pytest.raises(ServiceError): header_metadata(path)


def test_tamper_and_record_shape_change_fail_closed(ledger):
    service, project, assets = ledger
    item = assets.create(project['id'], reference_file(service, n=8))
    record = assets.record(item['id'])
    with pytest.raises(ServiceError): validate_record({**record, 'n': 9}, Path(record['_path']))
    path = Path(record['_path'])
    path.chmod(0o600)
    with path.open('ab') as output: output.write(b'tampered')
    with pytest.raises(ServiceError): assets.path(item['id'])


def test_comparison_reads_actual_values_and_rejects_wrong_modes(ledger, tmp_path):
    service, project, assets = ledger
    item = assets.create(project['id'], reference_file(service, n=60001))
    candidate = tmp_path / 'candidate'
    candidate.mkdir()
    with np.load(assets.path(item['id']), allow_pickle=False) as source:
        np.save(candidate / 'eigenvalues.npy', source['eigenvalues'])
        np.save(candidate / 'eigenvectors.npy', source['eigenvectors'])
    criteria = {'max_eigenvalue_absolute_error': 1e-8, 'max_eigenvalue_relative_error': 1e-6, 'max_subspace_sine': 1e-4}
    report = assets.compare(item['id'], candidate, criteria)
    assert report['passed'] is True
    assert report['candidate_eigenvalues'] == [1., 2.] and report['reference_eigenvalues'] == [1., 2.]
    np.save(candidate / 'eigenvalues.npy', np.array([10., 20.]))
    assert assets.compare(item['id'], candidate, criteria)['passed'] is False


def test_missing_payload_metadata_is_strictly_validated(ledger):
    service, project, assets = ledger
    item = assets.create(project['id'], reference_file(service, n=8))
    record = assets.record(item['id'])
    assert validate_record(record, None)['n'] == 8
    for change in ({'n': 2000001}, {'modes': 33}, {'n': 1000000, 'modes': 32},
                   {'dtype': 'object'}, {'dtype': '<U2'}, {'eigenvalue_dtype': 'complex128'}):
        with pytest.raises(ServiceError): validate_record({**record, **change}, None)


def test_gsm_scale_asset_uses_child_for_array_validation(ledger, monkeypatch):
    service, project, assets = ledger
    source = reference_file(service, n=589446, modes=1)

    def forbidden(*args, **kwargs):
        raise AssertionError('Full arrays must never be loaded in the web process')

    monkeypatch.setattr(np, 'load', forbidden)
    assert assets.create(project['id'], source)['n'] == 589446


def test_worker_deadline_aborts_without_publishing_asset(ledger, monkeypatch):
    from emsolver import industrial_reference
    service, project, assets = ledger
    source = reference_file(service, n=8)
    monkeypatch.setattr(industrial_reference, 'MAX_WALLTIME_SECONDS', 0)
    with pytest.raises(ServiceError, match='INDUSTRIAL_REFERENCE_RESOURCE_LIMIT'):
        assets.create(project['id'], source)
    assert not service.store.select('industrial_reference_assets', project_id=project['id'])


def test_multipart_api_upload_and_download_are_bounded(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from emsolver.api import create_app
    from emsolver import industrial_reference_api

    app = create_app(tmp_path / 'api', start_runner=False)
    if '/api/v1/projects/{project_id}/industrial-reference-assets' not in app.openapi()['paths']:
        app.include_router(industrial_reference_api.router)
    with TestClient(app) as client:
        project = client.post('/api/v1/projects', json={'name': 'Reference API'}).json()
        archive = io.BytesIO()
        np.savez_compressed(archive, eigenvalues=np.array([1.]), eigenvectors=np.ones((8, 1)))
        content = archive.getvalue()
        response = client.post('/api/v1/projects/' + project['id'] + '/industrial-reference-assets',
            files={'file': ('reference.npz', content, 'application/octet-stream')})
        assert response.status_code == 201, response.text
        item = response.json()
        assert '_path' not in item
        assert client.get('/api/v1/industrial-reference-assets/' + item['id']).json()['n'] == 8
        assert client.get('/api/v1/industrial-reference-assets/' + item['id'] + '/download').content == content
        monkeypatch.setattr(industrial_reference_api, 'MAX_UPLOAD_BYTES', 1)
        rejected = client.post('/api/v1/projects/' + project['id'] + '/industrial-reference-assets',
            files={'file': ('reference.npz', content, 'application/octet-stream')})
        assert rejected.status_code == 413


def test_reference_workers_do_not_multiply_the_memory_budget(ledger):
    service, project, assets = ledger
    source = reference_file(service, n=8)
    service._industrial_reference_lock.acquire()
    try:
        with pytest.raises(ServiceError, match='INDUSTRIAL_REFERENCE_BUSY'):
            IndustrialReferenceAssets(service).create(project['id'], source)
    finally:
        service._industrial_reference_lock.release()


def test_project_quota_failure_does_not_publish_or_leave_payload(ledger):
    service, project, assets = ledger
    source = reference_file(service, n=8)
    service.update_project(project['id'], policy={'storage_limit_gib': 1e-9})
    with pytest.raises(ServiceError, match='storage quota'):
        assets.create(project['id'], source)
    assert not service.store.select('industrial_reference_assets', project_id=project['id'])
    assert not list((service.root / 'industrial').glob('*.npz'))


def test_expansion_limit_is_checked_before_array_loading(ledger, monkeypatch):
    from emsolver import industrial_reference
    service, _, _ = ledger
    source = reference_file(service, n=8)
    monkeypatch.setattr(industrial_reference, 'MAX_UNCOMPRESSED_BYTES', 100)
    with pytest.raises(ServiceError): header_metadata(source)


@pytest.mark.parametrize('scalar', ['uint64', 'int64', 'float16', 'longdouble'])
def test_reference_rejects_values_that_are_not_losslessly_promoted_to_float64(ledger, scalar):
    service, project, assets = ledger
    source = service.root / 'inbox' / 'inexact-reference.npz'
    dtype = np.dtype(scalar)
    if scalar == 'longdouble' and dtype.itemsize <= 8: pytest.skip('No extended precision on this platform')
    value = 2**53 + 1 if dtype.kind in 'iu' else 1.
    np.savez(source, eigenvalues=np.array([value], dtype=dtype), eigenvectors=np.ones((1, 1), dtype=np.float64))
    with pytest.raises(ServiceError): header_metadata(source)
    with pytest.raises(ServiceError): assets.create(project['id'], source)


@pytest.mark.parametrize('scalar', ['int64', 'float16', 'longdouble'])
def test_reference_vector_and_restored_metadata_reject_lossy_scalar_types(ledger, scalar):
    service, project, assets = ledger
    dtype = np.dtype(scalar)
    if scalar == 'longdouble' and dtype.itemsize <= 8: pytest.skip('No extended precision on this platform')
    valid = assets.create(project['id'], reference_file(service, n=8))
    record = assets.record(valid['id'])
    with pytest.raises(ServiceError): validate_record({**record, 'dtype': str(dtype)}, None)
    with pytest.raises(ServiceError): validate_record({**record, 'eigenvalue_dtype': str(dtype)}, None)
    source = service.root / 'inbox' / 'inexact-vectors.npz'
    np.savez(source, eigenvalues=np.array([1.]), eigenvectors=np.ones((1, 1), dtype=dtype))
    with pytest.raises(ServiceError): header_metadata(source)
