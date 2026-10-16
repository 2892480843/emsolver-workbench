"""Actual Service cancellation crosses the host/container process boundary."""
import json
import os
import time

import pytest

from emsolver.integrity import file_hash
from emsolver.service import Service


@pytest.mark.skipif(os.environ.get('EMSOLVER_TEST_SLEPC') != '1', reason='Explicit tested optional SLEPc image is required')
def test_service_cancel_stops_actual_mpi_container_and_removes_private_workspace(tmp_path):
    from emsolver.optional_backends import _settings, _remove, cleanup_slepc, probe_slepc
    import subprocess
    assert probe_slepc()['supported'] is True
    executable, _, environment, _ = _settings()
    service = Service(tmp_path / 'data', start_runner=True)
    name = None
    run = None
    try:
        project = service.create_project('Actual MPI cancellation')
        matrix = service.generate_sample(project['id'], kind='complex', n=100000)
        asset = service.store.get('matrices', matrix['id'])
        digest = file_hash(asset['_path'])
        plan = service.create_plan(config={
            'problem': {'matrix_id': matrix['id']},
            'target': {'k': 3, 'sigma': .1},
            'solver': {'backend': 'slepc', 'method': 'krylovschur', 'subspace_max': 30},
            'resources': {'threads': 1, 'mpi_ranks': 2, 'memory_limit_gib': 2,
                          'walltime_seconds': 30, 'disk_limit_gib': 1},
        })
        submitted = service.submit_job(plan['id'])
        run = service.root / 'runs' / submitted['id']
        deadline = time.monotonic() + 20
        paused = False
        while time.monotonic() < deadline:
            markers = list(run.glob('.slepc-*/container.json'))
            if markers:
                name = json.loads(markers[0].read_text())['name']
                inspected = subprocess.run([executable, 'inspect', '--format', '{{.State.Running}}', name], env=environment, capture_output=True)
                if inspected.returncode == 0 and inspected.stdout.strip() == b'true':
                    paused = subprocess.run([executable, 'pause', name], env=environment, capture_output=True).returncode == 0
                    if paused:
                        break
            assert service.get_job(submitted['id'])['status'] not in {'SUCCEEDED', 'PARTIAL', 'FAILED', 'TIMED_OUT', 'CANCELLED'}
            time.sleep(.01)
        assert paused and name is not None
        assert service.cancel_job(submitted['id'])['status'] == 'CANCELLING'
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and service.get_job(submitted['id'])['status'] == 'CANCELLING':
            time.sleep(.02)
        finished = service.get_job(submitted['id'])
        assert finished['status'] == 'CANCELLED' and finished['result'] is None
        assert not list(run.glob('.slepc-*'))
        inspected = subprocess.run([executable, 'inspect', name], env=environment, capture_output=True)
        assert inspected.returncode != 0
        assert file_hash(asset['_path']) == digest
        terminals = [event for event in service.events(submitted['id']) if event['event'] in {'SUCCEEDED', 'PARTIAL', 'FAILED', 'TIMED_OUT', 'CANCELLED'}]
        assert [event['event'] for event in terminals] == ['CANCELLED']
    finally:
        service.close()
        if name:
            _remove(executable, name, environment)
        if run:
            cleanup_slepc(run)
