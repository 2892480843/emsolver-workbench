"""Service metadata edits stay atomic across independent database connections."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
import threading
import uuid

import pytest

from emsolver.access import Identity, identity_context
from emsolver.errors import ServiceError
from emsolver.service import Service


@pytest.fixture(params=['sqlite', 'postgresql'])
def services(request, tmp_path, monkeypatch):
    schema = None
    dsn = os.environ.get('EMSOLVER_TEST_DATABASE_URL')
    if request.param == 'postgresql':
        if not dsn: pytest.skip('Set EMSOLVER_TEST_DATABASE_URL for real PostgreSQL service checks')
        psycopg = pytest.importorskip('psycopg')
        schema = 'test_collaboration_' + uuid.uuid4().hex
        monkeypatch.setenv('EMSOLVER_DATABASE_URL', dsn)
        monkeypatch.setenv('EMSOLVER_DATABASE_SCHEMA', schema)
    else:
        monkeypatch.delenv('EMSOLVER_DATABASE_URL', raising=False)
    first = Service(tmp_path / 'first', start_runner=False)
    second = Service(tmp_path / ('second' if schema else 'first'), start_runner=False)
    try:
        yield first, second
    finally:
        second.close(); first.close()
        if schema:
            with psycopg.connect(dsn, autocommit=True) as connection:
                connection.execute(psycopg.sql.SQL('DROP SCHEMA {} CASCADE').format(psycopg.sql.Identifier(schema)))


def _interleave_unprotected_reads(services, monkeypatch, kind):
    """Reproduce the old lost update only while reads lack a DB transaction.

    Once a writer holds the database transaction, another writer cannot reach
    the same read concurrently, so pausing both readers would manufacture a
    deadlock. Unprotected legacy readers can safely meet at this barrier.
    """
    barrier = threading.Barrier(2)
    originals = []
    for service in services:
        original = service.store.get
        originals.append((service.store, original))
        def get(selected_kind, identity, *, original=original, service=service):
            item = original(selected_kind, identity)
            if selected_kind == kind and not service.store.db.in_transaction:
                barrier.wait(timeout=10)
            return item
        monkeypatch.setattr(service.store, 'get', get)
    return originals


def test_two_services_preserve_independent_project_edits(services, monkeypatch):
    first, second = services
    project = first.create_project('Original', 'Original description')
    originals = _interleave_unprotected_reads(services, monkeypatch, 'projects')
    with ThreadPoolExecutor(max_workers=2) as executor:
        operations = [executor.submit(first.update_project, project['id'], name='Renamed'),
                      executor.submit(second.update_project, project['id'], description='Concurrent description')]
        for operation in operations: operation.result(timeout=15)
    for store, get in originals: monkeypatch.setattr(store, 'get', get)
    stored = first.store.get('projects', project['id'])
    assert stored['name'] == 'Renamed'
    assert stored['description'] == 'Concurrent description'
    assert stored['version'] == 3


def test_two_services_preserve_independent_matrix_edits(services, monkeypatch):
    first, second = services
    project = first.create_project('Project')
    first.store.put('matrices', 'm', {'id': 'm', 'project_id': project['id'], 'name': 'Original', 'metadata': {}, 'version': 1})
    originals = _interleave_unprotected_reads(services, monkeypatch, 'matrices')
    with ThreadPoolExecutor(max_workers=2) as executor:
        operations = [executor.submit(first.update_matrix, 'm', name='Renamed'),
                      executor.submit(second.update_matrix, 'm', metadata={'description': 'New metadata'})]
        for operation in operations: operation.result(timeout=15)
    for store, get in originals: monkeypatch.setattr(store, 'get', get)
    stored = first.store.get('matrices', 'm')
    assert stored['name'] == 'Renamed'
    assert stored['metadata']['description'] == 'New metadata'
    assert stored['version'] == 3


@pytest.mark.parametrize('kind', ['project', 'matrix'])
def test_stale_expected_version_returns_conflict_without_overwriting(services, kind):
    first, second = services
    project = first.create_project('Original')
    if kind == 'matrix':
        first.store.put('matrices', 'm', {'id': 'm', 'project_id': project['id'], 'name': 'Original', 'metadata': {}})
    identity = project['id'] if kind == 'project' else 'm'
    edit = first.update_project if kind == 'project' else first.update_matrix
    stale_edit = second.update_project if kind == 'project' else second.update_matrix
    changed = edit(identity, name='Current', expected_version=1)
    assert changed['version'] == 2
    with pytest.raises(ServiceError) as caught:
        stale_edit(identity, name='Stale', expected_version=1)
    assert caught.value.code == 'VERSION_CONFLICT' and caught.value.status == 409
    assert first.store.get('projects' if kind == 'project' else 'matrices', identity)['name'] == 'Current'


@pytest.mark.parametrize('kind', ['project', 'matrix'])
def test_denied_edit_retains_audit_after_transaction_rollback(services, kind):
    first, _ = services
    project = first.create_project('Private')
    first.store.put('matrices', 'm', {'id': 'm', 'project_id': project['id'], 'name': 'Private', 'metadata': {}})
    edit = first.update_project if kind == 'project' else first.update_matrix
    with identity_context(Identity('outsider')):
        with pytest.raises(ServiceError) as caught:
            edit(project['id'] if kind == 'project' else 'm', name='Unauthorized')
    assert caught.value.code == 'FORBIDDEN'
    denials = [entry for entry in first.store.audit() if entry['action'] == 'authorization.denied']
    assert len(denials) == 1 and denials[0]['actor'] == 'outsider'
    assert denials[0]['object_id'] == project['id']


def test_project_priority_orders_numerically_on_both_backends(services):
    first, _ = services
    low = first.create_project('Low')
    high = first.create_project('High')
    first.update_project(low['id'], policy={'priority': 2})
    first.update_project(high['id'], policy={'priority': 10})
    for project, job_id in ((low, 'first'), (high, 'second')):
        first.store.put('jobs', job_id, {'id': job_id, 'project_id': project['id'], 'status': 'QUEUED'})
    assert first.jobs.next_queued()['id'] == 'second'


@pytest.mark.parametrize('expected_version', [True, 0, -1, '1', 1.5])
def test_expected_version_requires_a_positive_integer(services, expected_version):
    first, _ = services
    project = first.create_project('Original')
    with pytest.raises(ServiceError) as caught:
        first.update_project(project['id'], name='Changed', expected_version=expected_version)
    assert caught.value.code == 'INVALID_EXPECTED_VERSION' and caught.value.status == 400
    assert first.store.get('projects', project['id'])['version'] == 1


def test_operations_policy_update_invalidates_a_previously_loaded_project_version(services):
    from emsolver.operations import Operations
    first, second = services
    project = first.create_project('Original')
    Operations(first).update_policy(project['id'], {'priority': 4})
    with pytest.raises(ServiceError) as caught:
        second.update_project(project['id'], name='Stale', expected_version=1)
    assert caught.value.code == 'VERSION_CONFLICT'
    assert first.store.get('projects', project['id'])['version'] == 2


@pytest.mark.parametrize('change', ['archive', 'engineering_binding'])
def test_other_matrix_writes_invalidate_a_previously_loaded_version(services, change):
    from emsolver.engineering import EngineeringAssets
    first, second = services
    project = first.create_project('Project')
    matrix = first.generate_sample(project['id'], n=8)
    if change == 'archive':
        first.archive_matrix(matrix['id'])
    else:
        assets = EngineeringAssets(first)
        asset = assets.create_demo(matrix['id'])
        assets.select(matrix['id'], asset['id'])
    with pytest.raises(ServiceError) as caught:
        second.update_matrix(matrix['id'], name='Stale', expected_version=1)
    assert caught.value.code == 'VERSION_CONFLICT'
    assert first.store.get('matrices', matrix['id'])['version'] == 2


def test_derived_matrix_starts_with_its_own_initial_version(services):
    first, _ = services
    project = first.create_project('Project')
    matrix = first.generate_sample(project['id'], n=8)
    first.update_matrix(matrix['id'], name='Renamed')
    derived = first.derive_matrix(matrix['id'], 'upper')
    assert derived['version'] == 1
    assert first.store.get('matrices', matrix['id'])['version'] == 2


def test_separate_service_rechecks_project_membership_after_revocation(services):
    first, second = services
    project = first.create_project('Private')
    matrix = first.generate_sample(project['id'], n=8)
    first.update_project(project['id'], members=[{'subject': 'collaborator', 'role': 'admin'}])
    with identity_context(Identity('collaborator')):
        second.update_matrix(matrix['id'], name='Authorized', expected_version=1)
    first.update_project(project['id'], members=[{'subject': 'local-user', 'role': 'admin'}])
    with identity_context(Identity('collaborator')):
        for edit, identity in ((second.update_project, project['id']), (second.update_matrix, matrix['id'])):
            with pytest.raises(ServiceError) as caught:
                edit(identity, name='Revoked', expected_version=1)
            assert caught.value.code == 'FORBIDDEN' and caught.value.status == 403
    assert first.store.get('matrices', matrix['id'])['name'] == 'Authorized'
    assert len([row for row in first.store.audit() if row['actor'] == 'collaborator' and row['result'] == 'denied']) == 2


def test_archiving_and_concurrent_metadata_edit_preserve_both_changes(services, monkeypatch):
    first, second = services
    project = first.create_project('Project')
    first.store.put('matrices', 'm', {'id': 'm', 'project_id': project['id'], 'name': 'Original', 'metadata': {}, 'version': 1})
    originals = _interleave_unprotected_reads(services, monkeypatch, 'matrices')
    with ThreadPoolExecutor(max_workers=2) as executor:
        operations = [executor.submit(first.archive_matrix, 'm'),
                      executor.submit(second.update_matrix, 'm', metadata={'description': 'Concurrent metadata'})]
        for operation in operations: operation.result(timeout=15)
    for store, get in originals: monkeypatch.setattr(store, 'get', get)
    stored = first.store.get('matrices', 'm')
    assert stored['archived'] is True
    assert stored['metadata']['description'] == 'Concurrent metadata'
    assert stored['version'] == 3
