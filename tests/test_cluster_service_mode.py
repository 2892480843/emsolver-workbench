"""Cluster API processes never launch or recover local numerical workers."""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
import zipfile

import pytest

from emsolver.cluster_protocol import execution_fingerprint
from emsolver.service import Service


def test_cluster_mode_skips_local_recovery_and_numerical_queue(tmp_path, monkeypatch):
    monkeypatch.delenv('EMSOLVER_DATABASE_URL', raising=False)
    local = Service(tmp_path, start_runner=False)
    local.store.put('jobs', 'remote', {'id': 'remote', 'status': 'RUNNING', 'stage': 'running', 'worker_pid': os.getpid()})
    local.close()
    def forbidden(*args, **kwargs): pytest.fail('Cluster mode attempted to control a local numerical worker')
    monkeypatch.setattr('emsolver.recovery.verified_worker', forbidden)
    monkeypatch.setattr(Service, '_kill_tree', forbidden)
    service = Service(tmp_path, execution_mode='cluster')
    second = Service(tmp_path, execution_mode='cluster')
    try:
        assert service.cluster is not None and second.cluster is not None
        assert service._thread is second._thread is None
        assert service.store.get('jobs', 'remote')['status'] == 'RUNNING'
        assert service._owns_runner is True and second._owns_runner is False
        assert service.import_manager._thread is not None
        assert second.import_manager._thread is None
        health = service.health()
        assert health['execution_mode'] == 'cluster' and health['persistence_backend'] == 'sqlite'
        assert health['queue_available'] is True
    finally:
        second.close(); service.close()


def test_mode_environment_and_invalid_modes(tmp_path, monkeypatch):
    monkeypatch.delenv('EMSOLVER_DATABASE_URL', raising=False)
    monkeypatch.setenv('EMSOLVER_EXECUTION_MODE', 'cluster')
    service = Service(tmp_path / 'cluster', start_runner=False)
    try:
        assert service.execution_mode == 'cluster'
    finally:
        service.close()
    local = Service(tmp_path / 'local', start_runner=False, execution_mode='local')
    try:
        assert local.execution_mode == 'local'
    finally:
        local.close()
    with pytest.raises(ValueError, match='execution_mode'):
        Service(tmp_path / 'invalid', start_runner=False, execution_mode='invalid')


def test_cluster_submission_freezes_execution_and_original_plan(tmp_path, monkeypatch):
    monkeypatch.delenv('EMSOLVER_DATABASE_URL', raising=False)
    service = Service(tmp_path, start_runner=False, execution_mode='cluster')
    try:
        project = service.create_project('Remote')
        matrix = service.generate_sample(project['id'], n=20)
        plan = service.create_plan(config={'problem': {'matrix_id': matrix['id']}, 'target': {'k': 2}, 'resources': {'threads': 1}})
        original = json.loads(json.dumps(plan))
        job = service.submit_job(plan['id'])
        assert job['execution'] == execution_fingerprint()
        assert job['config'] == original['config']
        assert job['plan_id'] == plan['id'] and job['matrix_hash'] == original['input_hash']
        assert service.store.get('plans', plan['id']) == original
    finally:
        service.close()


def test_postgres_service_backup_is_portable_and_restore_requires_reenrollment(tmp_path, monkeypatch):
    dsn = os.environ.get('EMSOLVER_TEST_DATABASE_URL')
    if not dsn: pytest.skip('Set EMSOLVER_TEST_DATABASE_URL for real PostgreSQL service checks')
    psycopg = pytest.importorskip('psycopg')
    schema = 'test_service_' + uuid.uuid4().hex
    monkeypatch.setenv('EMSOLVER_DATABASE_URL', dsn)
    monkeypatch.setenv('EMSOLVER_DATABASE_SCHEMA', schema)
    service = Service(tmp_path / 'source', start_runner=False, execution_mode='cluster')
    try:
        assert service.health()['persistence_backend'] == 'postgresql'
        project = service.create_project('Portable')
        backup = service.backup()
        with zipfile.ZipFile(backup) as archive:
            snapshot = tmp_path / 'snapshot.sqlite3'
            snapshot.write_bytes(archive.read('metadata.sqlite3'))
        with sqlite3.connect(snapshot) as connection:
            assert connection.execute("SELECT count(*) FROM entities WHERE kind='projects'").fetchone()[0] == 1
        destination = tmp_path / 'restored'
        result = service.restore_backup(backup, destination)
        assert result['cluster_reenrollment_required'] is True
        with sqlite3.connect(destination / 'metadata.sqlite3') as connection:
            assert connection.execute("SELECT count(*) FROM sqlite_schema WHERE name IN ('cluster_workers','cluster_job_attempts')").fetchone()[0] == 0
            assert json.loads(connection.execute("SELECT body FROM entities WHERE kind='projects'").fetchone()[0])['id'] == project['id']
    finally:
        service.close()
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(psycopg.sql.SQL('DROP SCHEMA {} CASCADE').format(psycopg.sql.Identifier(schema)))


def test_cluster_capabilities_disable_host_optional_backends_but_local_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.delenv('EMSOLVER_DATABASE_URL', raising=False)
    host = {'scipy': {'available': True, 'supported': True},
            'slepc': {'available': True, 'supported': True},
            'mpi': {'available': True, 'supported': True},
            'gpu': {'available': True, 'supported': True}, 'cpu_count': 8}
    monkeypatch.setattr('emsolver.numerics.capabilities', lambda: host)
    local = Service(tmp_path / 'local', start_runner=False, execution_mode='local')
    cluster = Service(tmp_path / 'cluster', start_runner=False, execution_mode='cluster')
    try:
        assert local.capabilities() == host
        result = cluster.capabilities()
        assert result['cluster_execution'] == {'supported_backends': ['scipy'], 'devices': ['cpu'],
                                               'mpi_ranks': [1], 'remote_checkpoint_publication': False}
        for backend in ('slepc', 'mpi', 'gpu'):
            assert result[backend]['available'] is False and result[backend]['supported'] is False
        assert local.capabilities() == host
    finally:
        cluster.close(); local.close()


def test_cluster_planning_and_submission_reject_unsupported_execution_profiles(tmp_path, monkeypatch):
    from copy import deepcopy
    from emsolver.errors import ServiceError
    monkeypatch.delenv('EMSOLVER_DATABASE_URL', raising=False)
    service = Service(tmp_path, start_runner=False, execution_mode='cluster')
    try:
        project = service.create_project('Restricted cluster')
        matrix = service.generate_sample(project['id'], n=20)
        plan = service.create_plan(config={'problem': {'matrix_id': matrix['id']}, 'target': {'k': 2}, 'resources': {'threads': 1}})
        for section, field, value in (('solver', 'backend', 'slepc'), ('resources', 'device', 'gpu'), ('resources', 'mpi_ranks', 2)):
            config = deepcopy(plan['config'])
            config[section][field] = value
            for operation in ('plan', 'submit'):
                with pytest.raises(ServiceError) as caught:
                    if operation == 'plan':
                        service.create_plan(config=config)
                    else:
                        saved = deepcopy(plan)
                        saved['id'] = 'unsupported_' + field
                        saved['config'] = config
                        service.store.put('plans', saved['id'], saved)
                        service.submit_job(saved['id'])
                assert caught.value.code == 'CLUSTER_BACKEND_UNAVAILABLE' and caught.value.status == 409
        assert service.store.count('jobs') == 0
    finally:
        service.close()


def test_cluster_thread_planning_uses_protocol_budget_instead_of_api_cpu_count(tmp_path, monkeypatch):
    from emsolver.errors import ServiceError
    monkeypatch.delenv('EMSOLVER_DATABASE_URL', raising=False)
    for mode in ('cluster', 'local'):
        service = Service(tmp_path / mode, start_runner=False, execution_mode=mode)
        try:
            capabilities = service.capabilities()
            capabilities['cpu_count'] = 1
            monkeypatch.setattr(service, 'capabilities', lambda: capabilities)
            project = service.create_project('Small API')
            matrix = service.generate_sample(project['id'], n=20)
            config = {'problem': {'matrix_id': matrix['id']}, 'target': {'k': 2}, 'resources': {'threads': 2}}
            if mode == 'cluster':
                assert service.create_plan(config=config)['config']['resources']['threads'] == 2
                config['resources']['threads'] = 257
                with pytest.raises(ServiceError) as caught:
                    service.create_plan(config=config)
                assert caught.value.code == 'INVALID_CONFIG'
            else:
                with pytest.raises(ServiceError) as caught:
                    service.create_plan(config=config)
                assert caught.value.code == 'THREAD_BUDGET_EXCEEDED'
        finally:
            service.close()
