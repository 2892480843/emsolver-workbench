"""List-query bounds and access isolation, using temporary metadata only."""
import asyncio
import json
import threading

import httpx
import pytest

from emsolver.access import Identity, current_identity, identity_context
from emsolver.api import create_app
from emsolver.errors import ServiceError
from emsolver.service import Service


@pytest.fixture
def service(tmp_path):
    instance = Service(tmp_path, start_runner=False)
    yield instance
    instance.close()


def project(service, identifier, subject='reader'):
    item = {'id': identifier, 'name': identifier,
            'members': [{'subject': subject, 'role': 'viewer'}]}
    service.store.put('projects', identifier, item)
    return item


def selected_queries(service, operation):
    queries = []
    service.store.db.set_trace_callback(queries.append)
    try:
        result = operation()
    finally:
        service.store.db.set_trace_callback(None)
    return result, [query for query in queries if query.lstrip().upper().startswith('SELECT')]


def test_matrix_visibility_cost_depends_on_projects_and_is_fresh_per_request(service):
    allowed = project(service, 'p')
    project(service, 'hidden', 'other-reader')
    expected = []
    for index in range(60):
        parent = ('p', 'hidden', 'missing')[index % 3]
        item = {'id': f'm_{index}', 'project_id': parent, 'name': str(index),
                '_path': '/private/input', '_internal': True}
        service.store.put('matrices', item['id'], item)
        if parent == 'p':
            expected.append({key: value for key, value in item.items() if not key.startswith('_')})
    with identity_context(Identity('reader')):
        rows, queries = selected_queries(service, service.list_matrices)
        assert rows == expected
        assert len(queries) <= 4, 'project visibility must be resolved once per distinct parent'
    with identity_context(Identity('other-reader')):
        assert [item['id'] for item in service.list_matrices()] == [f'm_{index}' for index in range(1, 60, 3)]
    allowed['members'] = []
    service.store.put('projects', 'p', allowed)
    with identity_context(Identity('reader')):
        assert service.list_matrices() == []
    # Even a workspace administrator cannot make an orphaned matrix visible.
    assert len(service.list_matrices()) == 40


def test_config_lookup_cost_depends_on_referenced_matrices_not_config_count(service):
    allowed = project(service, 'p')
    project(service, 'hidden', 'other-reader')
    for identifier, parent in (('m_a', 'p'), ('m_b', 'p'), ('m_hidden', 'hidden'), ('m_orphan', 'missing')):
        service.store.put('matrices', identifier, {'id': identifier, 'project_id': parent})
    references = ('m_a', 'm_hidden', 'm_b', 'm_orphan', 'missing-matrix')
    expected = []
    for index in range(100):
        matrix_id = references[index % len(references)]
        item = {'id': f'c_{index}', 'config': {'problem': {'matrix_id': matrix_id}},
                'hash': str(index), 'created_at': 'frozen'}
        service.store.put('configs', item['id'], item)
        if matrix_id in {'m_a', 'm_b'}:
            expected.append(item)
    with identity_context(Identity('reader')):
        rows, queries = selected_queries(service, service.list_configs)
        assert rows == expected
        assert len(queries) <= 9, 'reuse repeated matrix and project metadata within this call'
    with identity_context(Identity('other-reader')):
        assert [item['id'] for item in service.list_configs()] == [f'c_{index}' for index in range(1, 100, 5)]
    allowed['members'] = []
    service.store.put('projects', 'p', allowed)
    with identity_context(Identity('reader')):
        assert service.list_configs() == []


def test_scoped_import_list_decodes_only_requested_project_without_truncation(service, monkeypatch):
    project(service, 'p')
    project(service, 'hidden', 'other-reader')
    expected = []
    with service.store.transaction():
        for index in range(445):
            item = {'id': f'i_{index}', 'project_id': 'p', 'status': 'SUCCEEDED',
                    '_path': '/private/staged', '_identity': {'subject': 'owner'}}
            service.store.put('imports', item['id'], item)
            service.store.put('imports', f'other_{index}',
                              {'id': f'other_{index}', 'project_id': 'hidden', 'status': 'SUCCEEDED'})
            expected.append({'id': item['id'], 'project_id': 'p', 'status': 'SUCCEEDED'})
    original = json.loads
    decoded_imports = []

    def track(value, *args, **kwargs):
        result = original(value, *args, **kwargs)
        if isinstance(result, dict) and result.get('status') == 'SUCCEEDED':
            decoded_imports.append(result['id'])
        return result

    monkeypatch.setattr('emsolver.store.json.loads', track)
    with identity_context(Identity('reader')):
        assert service.import_manager.list('p') == expected
    assert decoded_imports == [item['id'] for item in expected]


def test_import_list_keeps_scope_errors_and_denial_audits(service):
    project(service, 'p')
    project(service, 'hidden', 'other-reader')
    for identifier, parent in (('i_visible', 'p'), ('i_denied1', 'hidden'),
                               ('i_denied2', 'hidden'), ('i_orphan', 'missing')):
        service.store.put('imports', identifier, {'id': identifier, 'project_id': parent})
    with identity_context(Identity('reader')):
        rows = service.import_manager.list()
        assert [row['id'] for row in rows] == ['i_visible']
        denied = [row for row in service.store.audit() if row['action'] == 'authorization.denied']
        assert len(denied) == 2
        with pytest.raises(ServiceError) as error:
            service.import_manager.list('hidden')
        assert error.value.code == 'FORBIDDEN'
        with pytest.raises(ServiceError) as error:
            service.import_manager.list('missing')
        assert error.value.code == 'NOT_FOUND'
    with identity_context(Identity('other-reader')):
        assert [row['id'] for row in service.import_manager.list()] == ['i_denied1', 'i_denied2']


def test_import_submission_keeps_event_loop_responsive_and_request_identity(tmp_path, monkeypatch):
    monkeypatch.setattr('emsolver.api.authenticate', lambda _: Identity('analyst'))

    async def exercise():
        app = create_app(tmp_path, start_runner=False)
        async with app.router.lifespan_context(app):
            service = app.state.service
            service.store.put('projects', 'p', {'id': 'p', 'name': 'p',
                'members': [{'subject': 'analyst', 'role': 'analyst'}]})
            entered, release, finished = threading.Event(), threading.Event(), threading.Event()
            identities = []
            original = service.import_manager.submit

            def blocking_submit(*args, **kwargs):
                identities.append(current_identity().subject)
                entered.set()
                try:
                    assert release.wait(2), 'test did not release the simulated slow integrity check'
                    return original(*args, **kwargs)
                finally:
                    finished.set()

            monkeypatch.setattr(service.import_manager, 'submit', blocking_submit)
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
                upload = asyncio.create_task(client.post('/api/v1/imports', data={'project_id': 'p'},
                    files={'file': ('matrix.npz', b'temporary metadata fixture', 'application/octet-stream')}))
                try:
                    assert await asyncio.to_thread(entered.wait, 2)
                    assert not finished.is_set(), 'synchronous submit blocked the request event loop'
                    response = await asyncio.wait_for(client.get('/api/v1/identity'), timeout=1)
                    assert response.status_code == 200
                    assert response.json()['subject'] == 'analyst'
                    assert not finished.is_set()
                finally:
                    release.set()
                    result = await upload
                assert result.status_code == 202, result.text
                assert result.json()['status'] == 'QUEUED'
                assert identities == ['analyst']
                stored = service.store.get('imports', result.json()['id'])
                assert stored['_identity']['subject'] == 'analyst'
                assert not any(key.startswith('_') for key in result.json())

    asyncio.run(exercise())
