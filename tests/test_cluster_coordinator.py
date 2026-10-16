"""Database-backed scheduling, lease fencing and completion retry contracts."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
import threading
from types import SimpleNamespace
import uuid

import pytest

from emsolver.cluster import ClusterCoordinator
from emsolver.cluster_protocol import execution_fingerprint
from emsolver.errors import ServiceError
from emsolver.jobs import transition
from emsolver.store import Store


class CoordinatorService(SimpleNamespace):
    def __init__(self, path, **options):
        if options:
            from emsolver.postgres_store import PostgresStore
            store = PostgresStore(**options)
        else:
            store = Store(path)
        super().__init__(store=store, options=options)

    def _event(self, job_id, event):
        return self.store.append_event(job_id, event)


@pytest.fixture(params=['sqlite'] + (['postgres'] if os.environ.get('EMSOLVER_TEST_DATABASE_URL') else []))
def scheduler(tmp_path, request):
    options = ({'dsn': os.environ['EMSOLVER_TEST_DATABASE_URL'],
                'schema': 'cluster_test_' + uuid.uuid4().hex}
               if request.param == 'postgres' else {})
    service = CoordinatorService(tmp_path / 'metadata.sqlite3', **options)
    service.store.put('projects', 'p1', {'id': 'p1'})
    service.store.put('projects', 'p2', {'id': 'p2'})
    coordinator = ClusterCoordinator(service)
    yield service, coordinator
    service.store.close()
    if options:
        import psycopg
        from psycopg import sql
        with psycopg.connect(options['dsn'], autocommit=True) as connection:
            connection.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(options['schema'])))


def capacities(**overrides):
    return {'threads': 4, 'memory_gib': 8, 'disk_gib': 10,
            'devices': ['cpu'], **overrides}


def capabilities(**overrides):
    return {**capacities(), 'backends': ['scipy'],
            'execution': execution_fingerprint(), **overrides}


def enroll(coordinator, name='node-a', projects=None, **resources):
    return coordinator.enroll(name, projects or ['p1'],
                              capacities(**resources), ['scipy'])


def job(service, identity='j1', project='p1', **resources):
    value = {'id': identity, 'project_id': project, 'status': 'QUEUED',
             'stage': 'queued', 'config': {
                 'resources': {'threads': 2, 'memory_limit_gib': 2,
                               'disk_limit_gib': 2, 'device': 'cpu',
                               'mpi_ranks': 1, **resources},
                 'solver': {'backend': 'scipy'}},
             'runtime': {}, 'result': None, 'metrics': {}}
    service.store.put('jobs', identity, value)
    return value


def complete(service, identity, calls=None):
    def publish():
        if calls is not None:
            calls.append(identity)
        value = transition(service.store.get('jobs', identity), 'SUCCEEDED')
        service.store.put('jobs', identity, value)
        return value
    return publish


def expire(service, identity):
    with service.store.transaction():
        service.store.db.execute(
            "UPDATE cluster_job_attempts SET expires_at=0 WHERE job_id=? AND state='ACTIVE'",
            (identity,))


def test_enrollment_persists_only_hash_and_survives_reopen(scheduler, tmp_path):
    service, coordinator = scheduler
    enrolled = enroll(coordinator)
    worker = enrolled['worker']
    token = enrolled['token']
    assert len(token) >= 48
    assert coordinator.authenticate(token)['id'] == worker['id']
    assert token not in json.dumps(coordinator.workers())
    assert 'token_hash' not in json.dumps(coordinator.workers())
    raw = service.store.db.execute('SELECT token_hash FROM cluster_workers').fetchone()[0]
    assert raw != token and len(raw) == 64
    assert token not in json.dumps(service.store.audit())
    second = CoordinatorService(tmp_path / 'metadata.sqlite3', **service.options)
    try:
        reopened = ClusterCoordinator(second)
        assert reopened.authenticate(token)['id'] == worker['id']
    finally:
        second.store.close()


def test_two_connections_claim_one_job_only_once(scheduler, tmp_path):
    service, first = scheduler
    job(service)
    worker_a = enroll(first)['worker']['id']
    worker_b = enroll(first, 'node-b')['worker']['id']
    other = CoordinatorService(tmp_path / 'metadata.sqlite3', **service.options)
    try:
        second = ClusterCoordinator(other)
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(lambda pair: pair[0].claim(pair[1], capabilities()),
                                   [(first, worker_a), (second, worker_b)]))
        assert sum(claim is not None for claim in claims) == 1
        assert service.store.get('jobs', 'j1')['status'] == 'RUNNING'
        assert len(first.lease_status('j1')['attempts']) == 1
    finally:
        other.store.close()


def test_node_runs_only_one_job_and_claim_keeps_frozen_config(scheduler):
    service, coordinator = scheduler
    original = job(service)
    job(service, 'j2')
    worker = enroll(coordinator)['worker']['id']
    claimed = coordinator.claim(worker, capabilities())
    assert claimed['job']['config'] == original['config']
    assert coordinator.claim(worker, capabilities()) is None
    public = coordinator.lease_status('j1')
    assert claimed['lease_token'] not in json.dumps(public)
    assert 'lease_hash' not in json.dumps(public)


def test_expiry_requeues_then_fences_late_attempt(scheduler):
    service, coordinator = scheduler
    job(service)
    worker = enroll(coordinator)['worker']['id']
    old = coordinator.claim(worker, capabilities())
    expire(service, 'j1')
    fresh = coordinator.claim(worker, capabilities())
    assert fresh['attempt'] == old['attempt'] + 1
    assert fresh['lease_token'] != old['lease_token']
    assert service.store.get('jobs', 'j1')['status'] == 'RUNNING'
    calls = []
    with pytest.raises(ServiceError):
        coordinator.finish(worker, 'j1', old['lease_token'], old['attempt'],
                           'a' * 64, complete(service, 'j1', calls))
    assert calls == []
    assert any(event['event'] == 'LEASE_EXPIRED'
               for event in service.store.events('j1'))


def test_expiry_exhausts_bounded_attempts(scheduler):
    service, coordinator = scheduler
    job(service)
    worker = enroll(coordinator)['worker']['id']
    for attempt in range(1, 4):
        claim = coordinator.claim(worker, capabilities())
        assert claim['attempt'] == attempt
        expire(service, 'j1')
    assert coordinator.claim(worker, capabilities()) is None
    assert service.store.get('jobs', 'j1')['status'] == 'FAILED'
    assert len(coordinator.lease_status('j1')['attempts']) == 3


def test_revocation_blocks_auth_and_active_lease_then_requeues(scheduler):
    service, coordinator = scheduler
    job(service)
    enrolled = enroll(coordinator)
    worker = enrolled['worker']['id']
    claimed = coordinator.claim(worker, capabilities())
    coordinator.revoke(worker)
    with pytest.raises(ServiceError) as exc:
        coordinator.authenticate(enrolled['token'])
    assert exc.value.status == 401
    with pytest.raises(ServiceError):
        coordinator.heartbeat(worker, 'j1', claimed['lease_token'], claimed['attempt'])
    assert service.store.get('jobs', 'j1')['status'] == 'QUEUED'
    assert coordinator.workers()[0]['revoked_at'] is not None


@pytest.mark.parametrize('finish_before_expiry', [False, True])
def test_cancelled_attempt_never_requeues_or_publishes(scheduler, finish_before_expiry):
    service, coordinator = scheduler
    job(service)
    worker = enroll(coordinator)['worker']['id']
    claimed = coordinator.claim(worker, capabilities())
    value = transition(service.store.get('jobs', 'j1'), 'CANCELLING')
    service.store.put('jobs', 'j1', value)
    calls = []
    if finish_before_expiry:
        result = coordinator.finish(worker, 'j1', claimed['lease_token'], claimed['attempt'],
                                    'a' * 64, complete(service, 'j1', calls))
        assert result['status'] == 'CANCELLED'
    else:
        expire(service, 'j1')
        coordinator.reap_expired()
    assert calls == []
    assert service.store.get('jobs', 'j1')['status'] == 'CANCELLED'
    assert coordinator.claim(worker, capabilities()) is None


def test_scope_resources_and_backend_must_fit_owner_and_reported_capacity(scheduler):
    service, coordinator = scheduler
    job(service, 'private', project='p2')
    job(service, 'too-large', threads=8)
    gpu = job(service, 'gpu-only', device='gpu')
    backend = job(service, 'other-backend')
    backend['config']['solver']['backend'] = 'slepc'
    service.store.put('jobs', backend['id'], backend)
    job(service, 'fits')
    worker = enroll(coordinator)['worker']['id']
    claim = coordinator.claim(worker, capabilities(threads=1000, memory_gib=1000))
    assert claim['job_id'] == 'fits'
    assert all(service.store.get('jobs', identity)['status'] == 'QUEUED'
               for identity in ['private', 'too-large', gpu['id'], backend['id']])


def test_reported_resources_cannot_be_missing_or_smaller_than_frozen_budget(scheduler):
    service, coordinator = scheduler
    job(service)
    worker = enroll(coordinator)['worker']['id']
    assert coordinator.claim(worker, capabilities(memory_gib=1)) is None
    assert coordinator.claim(worker, capabilities(disk_gib=1)) is None
    assert coordinator.claim(worker, capabilities(devices=[])) is None


def test_fingerprint_mismatch_cannot_claim(scheduler):
    service, coordinator = scheduler
    job(service)
    worker = enroll(coordinator)['worker']['id']
    with pytest.raises(ServiceError) as exc:
        coordinator.claim(worker, capabilities(execution={'protocol': 'old', 'source_sha256': 'x'}))
    assert exc.value.code == 'WORKER_EXECUTION_MISMATCH'
    assert service.store.get('jobs', 'j1')['status'] == 'QUEUED'


def test_completion_retry_matches_digest_and_invokes_callback_once(scheduler):
    service, coordinator = scheduler
    job(service)
    worker = enroll(coordinator)['worker']['id']
    claim = coordinator.claim(worker, capabilities())
    calls = []
    for _ in range(2):
        finished = coordinator.finish(worker, 'j1', claim['lease_token'], claim['attempt'],
                                      'a' * 64, complete(service, 'j1', calls))
        assert finished['status'] == 'SUCCEEDED'
    assert calls == ['j1']
    with pytest.raises(ServiceError) as exc:
        coordinator.finish(worker, 'j1', claim['lease_token'], claim['attempt'],
                           'b' * 64, complete(service, 'j1', calls))
    assert exc.value.code == 'COMPLETION_CONFLICT'
    assert calls == ['j1']


def test_callback_failure_rolls_back_attempt_and_job_publication(scheduler):
    service, coordinator = scheduler
    job(service)
    worker = enroll(coordinator)['worker']['id']
    claim = coordinator.claim(worker, capabilities())

    def fail():
        complete(service, 'j1')()
        raise RuntimeError('publication failed')

    with pytest.raises(RuntimeError):
        coordinator.finish(worker, 'j1', claim['lease_token'], claim['attempt'], 'a' * 64, fail)
    assert service.store.get('jobs', 'j1')['status'] == 'RUNNING'
    assert coordinator.require_lease(worker, 'j1', claim['lease_token'], claim['attempt'])['id'] == 'j1'
    coordinator.finish(worker, 'j1', claim['lease_token'], claim['attempt'],
                       'a' * 64, complete(service, 'j1'))


def test_heartbeat_uses_database_time_and_exposes_cancellation(scheduler):
    service, coordinator = scheduler
    job(service)
    worker = enroll(coordinator)['worker']['id']
    claim = coordinator.claim(worker, capabilities())
    value = transition(service.store.get('jobs', 'j1'), 'CANCELLING')
    service.store.put('jobs', 'j1', value)
    heartbeat = coordinator.heartbeat(worker, 'j1', claim['lease_token'], claim['attempt'],
                                      {'rss_bytes': 4096, 'token': 'do-not-store'})
    assert heartbeat['cancel_requested'] is True
    assert heartbeat['expires_at'] >= claim['expires_at']
    assert 'do-not-store' not in json.dumps(coordinator.lease_status('j1'))


def test_project_allowlist_requires_deliberate_nonempty_scope(scheduler):
    _, coordinator = scheduler
    for projects in ([], None, ['*', 'p1'], ['missing']):
        with pytest.raises(ServiceError):
            coordinator.enroll('node-a', projects, capacities(), ['scipy'])
    assert coordinator.enroll('all-projects', ['*'], capacities(), ['scipy'])['worker']['project_ids'] == ['*']


def test_expired_heartbeat_cannot_renew_and_wrong_worker_cannot_publish(scheduler):
    service, coordinator = scheduler
    job(service)
    worker_a = enroll(coordinator)['worker']['id']
    worker_b = enroll(coordinator, 'node-b')['worker']['id']
    claim = coordinator.claim(worker_a, capabilities())
    with pytest.raises(ServiceError):
        coordinator.finish(worker_b, 'j1', claim['lease_token'], claim['attempt'],
                           'a' * 64, complete(service, 'j1'))
    expire(service, 'j1')
    with pytest.raises(ServiceError) as exc:
        coordinator.heartbeat(worker_a, 'j1', claim['lease_token'], claim['attempt'])
    assert exc.value.code == 'LEASE_EXPIRED'


def test_worker_enrollment_and_revocation_require_owner(scheduler):
    from emsolver.access import Identity, identity_context
    _, coordinator = scheduler
    enrolled = enroll(coordinator)
    with identity_context(Identity('analyst')):
        for operation in (lambda: enroll(coordinator), coordinator.workers,
                          lambda: coordinator.revoke(enrolled['worker']['id'])):
            with pytest.raises(ServiceError) as exc:
                operation()
            assert exc.value.status == 403


def test_completion_during_expiry_rolls_back_publication(scheduler, monkeypatch):
    service, coordinator = scheduler
    job(service)
    worker = enroll(coordinator)['worker']['id']
    claim = coordinator.claim(worker, capabilities())
    original_now = coordinator._now
    observations = iter([original_now(), claim['expires_at'] + 1])
    monkeypatch.setattr(coordinator, '_now', lambda: next(observations))
    with pytest.raises(ServiceError) as exc:
        coordinator.finish(worker, 'j1', claim['lease_token'], claim['attempt'],
                           'a' * 64, complete(service, 'j1'))
    assert exc.value.code == 'LEASE_EXPIRED'
    assert service.store.get('jobs', 'j1')['status'] == 'RUNNING'


def test_two_connections_retry_completion_without_duplicate_publication(scheduler, tmp_path):
    service, coordinator = scheduler
    job(service)
    worker = enroll(coordinator)['worker']['id']
    claim = coordinator.claim(worker, capabilities())
    other = CoordinatorService(tmp_path / 'metadata.sqlite3', **service.options)
    calls = []
    try:
        second = ClusterCoordinator(other)
        with ThreadPoolExecutor(max_workers=2) as pool:
            finished = list(pool.map(
                lambda pair: pair[0].finish(worker, 'j1', claim['lease_token'], claim['attempt'],
                                            'a' * 64, complete(pair[1], 'j1', calls)),
                [(coordinator, service), (second, other)]))
        assert [value['status'] for value in finished] == ['SUCCEEDED', 'SUCCEEDED']
        assert calls == ['j1']
    finally:
        other.store.close()


def test_completion_and_cancel_follow_same_lock_order(scheduler):
    service, coordinator = scheduler
    service._mutex = threading.RLock()
    job(service)
    worker = enroll(coordinator)['worker']['id']
    claim = coordinator.claim(worker, capabilities())
    callback_entered = threading.Event()
    cancelling_holds_mutex = threading.Event()

    def publish():
        callback_entered.set()
        cancelling_holds_mutex.wait(.03)
        acquired = service._mutex.acquire(timeout=.5)
        assert acquired, 'Completion and cancellation acquired their locks in opposite orders'
        try:
            return complete(service, 'j1')()
        finally:
            service._mutex.release()

    def cancel():
        assert callback_entered.wait(2)
        with service._mutex:
            cancelling_holds_mutex.set()
            with service.store.transaction():
                current = service.store.get('jobs', 'j1')
                if current['status'] == 'RUNNING':
                    service.store.put('jobs', 'j1', transition(current, 'CANCELLING'))

    with ThreadPoolExecutor(max_workers=2) as pool:
        cancellation = pool.submit(cancel)
        completed = pool.submit(coordinator.finish, worker, 'j1', claim['lease_token'],
                                claim['attempt'], 'a' * 64, publish)
        assert completed.result(timeout=3)['status'] == 'SUCCEEDED'
        cancellation.result(timeout=3)


def test_claim_preserves_project_priority_fifo_ties_and_resource_admission(scheduler):
    service, coordinator = scheduler
    service.store.put('projects', 'p1', {'id': 'p1', 'policy': {'priority': -2}})
    service.store.put('projects', 'p2', {'id': 'p2', 'policy': {'priority': 8}})
    job(service, 'older-low-priority', project='p1')
    job(service, 'high-priority-too-large', project='p2', threads=8)
    job(service, 'first-high-priority', project='p2')
    job(service, 'second-high-priority', project='p2')
    worker = enroll(coordinator, projects=['p1', 'p2'])['worker']['id']
    for identity in ('first-high-priority', 'second-high-priority', 'older-low-priority'):
        claim = coordinator.claim(worker, capabilities())
        assert claim['job_id'] == identity
        coordinator.finish(worker, identity, claim['lease_token'], claim['attempt'],
                           'a' * 64, complete(service, identity))
    assert service.store.get('jobs', 'high-priority-too-large')['status'] == 'QUEUED'
