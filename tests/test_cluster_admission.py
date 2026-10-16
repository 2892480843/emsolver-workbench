"""Cross-process intake locks bound duplicate work and validation resources."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import multiprocessing
import os
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest
from starlette.concurrency import run_in_threadpool

from emsolver.cluster_admission import acquire_admission, admission
from emsolver.errors import ServiceError
from emsolver.store import Store


@pytest.fixture(params=['sqlite', 'postgresql'])
def service(request, tmp_path, monkeypatch):
    monkeypatch.setenv('EMSOLVER_CLUSTER_VALIDATION_SLOTS', '1')
    root = tmp_path / 'data'
    root.mkdir(mode=0o700)
    schema, dsn = None, os.environ.get('EMSOLVER_TEST_DATABASE_URL')
    if request.param == 'postgresql':
        if not dsn: pytest.skip('Set EMSOLVER_TEST_DATABASE_URL for real PostgreSQL admission checks')
        psycopg = pytest.importorskip('psycopg')
        from emsolver.postgres_store import PostgresStore
        schema = 'test_admission_' + uuid.uuid4().hex
        store = PostgresStore(dsn, schema=schema)
    else:
        store = Store(root / 'metadata.sqlite3')
    instance = SimpleNamespace(root=root, store=store)
    try:
        yield instance
    finally:
        store.close()
        if schema:
            with psycopg.connect(dsn, autocommit=True) as connection:
                connection.execute(psycopg.sql.SQL('DROP SCHEMA {} CASCADE').format(psycopg.sql.Identifier(schema)))


def test_same_attempt_and_global_slot_are_nonblocking_and_released(service):
    first = acquire_admission(service, 'w', 'j', 1)
    try:
        for job in ('j', 'other'):
            with pytest.raises(ServiceError) as caught:
                acquire_admission(service, 'w', job, 1)
            assert caught.value.status == 503
    finally:
        first.close(); first.close()
    second = acquire_admission(service, 'w', 'other', 1)
    second.close()
    assert not service.store.db.in_transaction


def test_identical_attempt_two_threads_only_one_admitted(service, monkeypatch):
    monkeypatch.setenv('EMSOLVER_CLUSTER_VALIDATION_SLOTS', '2')
    first = acquire_admission(service, 'w', 'j', 1)
    try:
        def contender():
            with pytest.raises(ServiceError) as caught:
                acquire_admission(service, 'w', 'j', 1)
            return caught.value.status
        with ThreadPoolExecutor(max_workers=1) as executor:
            assert executor.submit(contender).result(timeout=10) == 503
        # The duplicate did not leak or consume the second global slot.
        independent = acquire_admission(service, 'w', 'other', 1)
        independent.close()
    finally:
        first.close()


def test_exception_releases_slot_and_nested_bindings_survive_threadpool(service):
    async def nested():
        def inspect():
            with admission(service, 'w', 'j', 1):
                return not service.store.db.in_transaction
        return await run_in_threadpool(inspect)
    with pytest.raises(RuntimeError, match='test failure'):
        with admission(service, 'w', 'j', 1):
            assert asyncio.run(nested())
            raise RuntimeError('test failure')
    with admission(service, 'w', 'j', 1):
        pass


def test_bound_key_does_not_bypass_a_different_attempt(service):
    handle = acquire_admission(service, 'w', 'j', 1)
    try:
        with handle.bind():
            with pytest.raises(ServiceError) as caught:
                with admission(service, 'w', 'different', 1):
                    pass
            assert caught.value.status == 503
    finally:
        handle.close()


def test_close_during_bound_work_defers_release_until_work_exits(service):
    handle = acquire_admission(service, 'w', 'j', 1)
    with handle.bind():
        handle.close()
        with pytest.raises(ServiceError):
            acquire_admission(service, 'w', 'other', 1)
    with admission(service, 'w', 'other', 1):
        pass


def _process_contender(root, dsn, schema, queue):
    """Spawned process opens its own Store/session rather than inheriting locks."""
    if dsn:
        from emsolver.postgres_store import PostgresStore
        store = PostgresStore(dsn, schema=schema)
    else:
        store = Store(Path(root) / 'metadata.sqlite3')
    try:
        try:
            handle = acquire_admission(SimpleNamespace(root=Path(root), store=store), 'w', 'j', 1)
        except ServiceError as error:
            queue.put(error.status)
        else:
            handle.close()
            queue.put('unexpected acquisition')
    finally:
        store.close()


def test_attempt_lock_is_shared_across_processes_and_instances(service, monkeypatch):
    monkeypatch.setenv('EMSOLVER_CLUSTER_VALIDATION_SLOTS', '2')
    handle = acquire_admission(service, 'w', 'j', 1)
    context = multiprocessing.get_context('spawn')
    queue = context.Queue()
    process = context.Process(target=_process_contender,
                              args=(str(service.root), getattr(service.store, '_dsn', None),
                                    getattr(service.store, 'schema', None), queue))
    try:
        process.start()
        assert queue.get(timeout=20) == 503
        process.join(timeout=10)
        assert process.exitcode == 0
    finally:
        if process.is_alive(): process.terminate(); process.join(timeout=5)
        handle.close()
        queue.close()


@pytest.mark.parametrize('slots', ['0', '5', 'invalid'])
def test_invalid_global_slot_configuration_fails_closed(service, monkeypatch, slots):
    monkeypatch.setenv('EMSOLVER_CLUSTER_VALIDATION_SLOTS', slots)
    with pytest.raises(ServiceError) as caught:
        acquire_admission(service, 'w', 'j', 1)
    assert caught.value.code == 'CLUSTER_CONFIGURATION_INVALID' and caught.value.status == 503


@pytest.mark.parametrize('count', [2, 4])
def test_configured_slots_allow_exactly_the_bounded_parallelism(service, monkeypatch, count):
    monkeypatch.setenv('EMSOLVER_CLUSTER_VALIDATION_SLOTS', str(count))
    handles = []
    try:
        for index in range(count):
            handles.append(acquire_admission(service, 'w', 'job_' + str(index), 1))
        with pytest.raises(ServiceError) as caught:
            acquire_admission(service, 'w', 'excess', 1)
        assert caught.value.status == 503
    finally:
        for handle in handles: handle.close()
    with admission(service, 'w', 'excess', 1):
        pass
