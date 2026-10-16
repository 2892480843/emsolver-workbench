import copy
import os
from pathlib import Path
import json
import subprocess
import sys
import time

import numpy as np
import pytest
from scipy import sparse

from emsolver.contracts import resolve_config
from emsolver.optional_backends import cleanup_slepc, driver_config, probe_slepc, sandbox_command, solve_slepc


def slepc_config():
    return resolve_config({'target': {'k': 3, 'sigma': .1},
                           'solver': {'backend': 'slepc', 'method': 'krylovschur'},
                           'resources': {'threads': 1, 'mpi_ranks': 2}})


def test_unconfigured_optional_backend_is_not_declared_supported(monkeypatch):
    monkeypatch.delenv('EMSOLVER_SLEPC_IMAGE', raising=False)
    monkeypatch.delenv('EMSOLVER_DOCKER_SOCKET', raising=False)
    result = probe_slepc()
    assert result['available'] is False and result['supported'] is False
    assert result['gpu']['supported'] is False
    assert result['tested_combinations'] == []


def test_driver_configuration_excludes_private_paths_and_unimplemented_routes():
    config = slepc_config()
    config['problem']['matrix_id'] = 'private-matrix-id'
    actual = driver_config(config)
    assert set(actual) == {'problem', 'target', 'solver', 'resources'}
    assert 'matrix_id' not in actual['problem']
    assert actual['solver']['method'] == 'krylovschur'
    assert actual['resources']['mpi_ranks'] == 2
    assert actual['solver']['tolerance'] < config['validation']['residual_scaled_max']
    assert actual['solver']['random_seed'] == config['solver']['random_seed']
    for change in ({'method': 'arpack'}, {'transform': 'none'}):
        changed = copy.deepcopy(config)
        changed['solver'].update(change)
        with pytest.raises(ValueError):
            driver_config(changed)


@pytest.mark.parametrize('field,value', [
    ('subspace_max', 1025), ('block.checkpoint_kind', 'exact_block'), ('block.checkpoint_interval', 1),
    ('linear.preconditioner', 'jacobi'), ('linear.adaptive_accuracy.enabled', True),
    ('linear.precision', 'mixed_float32'), ('output.eigenvectors', False),
    ('target.k', 600), ('random_seed', 2**63),
])
def test_driver_rejects_ignored_or_unsupported_flags_instead_of_clamping(field, value):
    config = slepc_config()
    path = field.split('.')
    target = config if path[0] in {'output', 'target'} else config['solver']
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValueError, match='UNSUPPORTED_SLEPC_COMBINATION'):
        driver_config(config)


def test_missing_or_untrusted_seed_cannot_trigger_global_worker_selftests(monkeypatch):
    import emsolver.optional_backends as module
    monkeypatch.setattr(module, '_SEED_REQUIRED', False, raising=False)
    monkeypatch.setattr(module, '_SEEDED_PROFILE', None, raising=False)
    called = []
    monkeypatch.setattr(module, '_invoke_json', lambda *args, **kwargs: called.append(args))
    with pytest.raises(ValueError):
        module.seed_slepc_profile(None)
    result = module.probe_slepc()
    assert result['supported'] is False and called == []
    assert result['reason'] == 'SLEPC_PROFILE_REQUIRED'


def test_sandbox_only_mounts_explicit_candidate_workspace_and_pins_image(tmp_path):
    inputs, outputs = tmp_path / 'input', tmp_path / 'output'
    inputs.mkdir(); outputs.mkdir()
    image = 'sha256:' + 'a' * 64
    command = sandbox_command('docker', image, 'emsolver-slepc-test', slepc_config(),
                              scalar='complex', inputs=inputs, outputs=outputs)
    assert '--network=none' in command and '--read-only' in command and '--pull=never' in command
    assert '--cap-drop=ALL' in command and '--security-opt=no-new-privileges' in command
    assert image in command and '--memory-swap' in command and '--pids-limit=128' in command
    memory = int(slepc_config()['resources']['memory_limit_gib'] * 1024**3) // 2
    assert command[command.index('--memory') + 1] == str(memory)
    assert command[command.index('--memory-swap') + 1] == str(memory)
    assert command.count('--mount') == 2
    mounts = [command[index + 1] for index, value in enumerate(command) if value == '--mount']
    assert any('target=/input,readonly' in value for value in mounts)
    assert any('target=/output' in value for value in mounts)
    assert not any('docker.sock' in value or 'reference' in value for value in mounts)
    assert '/usr/bin/timeout' in command and '/usr/bin/mpiexec' in command
    assert '2' in command and '/opt/emsolver/slepc_driver.py' in command


def test_remote_or_unowned_docker_socket_is_not_accepted(monkeypatch, tmp_path):
    monkeypatch.setenv('EMSOLVER_SLEPC_IMAGE', 'configured:local')
    for endpoint in ('tcp://127.0.0.1:2375', 'ssh://host', str(tmp_path / 'ordinary-file')):
        Path(tmp_path / 'ordinary-file').touch()
        monkeypatch.setenv('EMSOLVER_DOCKER_SOCKET', endpoint)
        result = probe_slepc()
        assert result['supported'] is False and result['tested_combinations'] == []


def test_cleanup_does_not_follow_symlinks_or_uncontrolled_container_names(tmp_path, monkeypatch):
    import emsolver.optional_backends as module
    outside = tmp_path / 'outside'
    outside.mkdir()
    (tmp_path / ('.slepc-' + 'a' * 32)).symlink_to(outside, target_is_directory=True)
    folder = tmp_path / ('.slepc-' + 'b' * 32)
    folder.mkdir(mode=0o700)
    (folder / 'container.json').write_text(json.dumps({'schema_version': '1.0', 'name': 'unrelated-container', 'image_id': 'sha256:' + 'a' * 64}))
    called = []
    monkeypatch.setattr(module, '_remove', lambda *args: called.append(args))
    cleanup_slepc(tmp_path)
    assert called == [] and outside.is_dir()


real_slepc = pytest.mark.skipif(os.environ.get('EMSOLVER_TEST_SLEPC') != '1', reason='Explicit local optional SLEPc image is required')


@real_slepc
@pytest.mark.parametrize('complex_input,ranks', [(False, 1), (False, 2), (True, 1), (True, 2)])
def test_actual_linux_slepc_candidates_pass_independent_original_matrix_checks(tmp_path, complex_input, ranks):
    config = slepc_config()
    config['resources']['mpi_ranks'] = ranks
    n = 60
    off = np.full(n - 1, .035)
    if complex_input:
        off = off * np.exp(.4j)
    matrix = sparse.diags([off, np.linspace(-4., 5., n), off.conjugate()], [-1, 0, 1], format='csr')
    events = []
    result = solve_slepc(matrix, config, tmp_path / 'published', event_callback=events.append)
    values, vectors = result['values'], result['vectors']
    assert len(values) >= 3 and vectors.shape == (n, len(values))
    assert result['strategy']['actual_backend'] == 'slepc' and result['strategy']['actual_method'] == 'krylovschur'
    assert result['metrics']['mpi_ranks'] == ranks and result['metrics']['factor_solver'] == 'mumps'
    total = int(config['resources']['memory_limit_gib'] * 1024**3)
    assert result['metrics']['container_memory_limit_bytes'] == total // 2
    assert result['metrics']['host_memory_limit_bytes'] + result['metrics']['container_memory_limit_bytes'] == total
    assert result['metrics']['total_job_memory_limit_bytes'] == total
    assert events and all(event['event'] == 'SLEPC_ITERATION' for event in events)
    assert result['candidate_status'] == 'CONVERGED'
    # Reference is created after candidate computation and is never mounted.
    expected = np.linalg.eigvalsh(matrix.toarray())
    wanted = expected[np.argsort(abs(expected - .1))[:3]]
    assert max(np.min(abs(values - value)) for value in wanted) < 1e-10
    residual = matrix @ vectors - vectors * values
    assert np.max(abs(residual)) < 1e-9
    assert np.linalg.norm(vectors.conjugate().T @ vectors - np.eye(len(values)), ord=2) < 1e-8
    assert list(tmp_path.iterdir()) == []


@real_slepc
def test_actual_launcher_cancellation_removes_owned_container_and_private_files(tmp_path, monkeypatch):
    import emsolver.optional_backends as module
    assert probe_slepc()['supported'] is True
    config = slepc_config()
    matrix = sparse.diags([np.full(59, .02), np.linspace(-2, 3, 60), np.full(59, .02)], [-1, 0, 1], format='csr')
    calls, removed = [], []
    original = module._remove
    def remove(*args):
        removed.append(args[1])
        return original(*args)
    monkeypatch.setattr(module, '_remove', remove)
    def cancel():
        calls.append(True)
        if len(calls) >= 2:
            raise RuntimeError('USER_CANCELLED')
    with pytest.raises(RuntimeError, match='USER_CANCELLED'):
        solve_slepc(matrix, config, tmp_path / 'published', check=cancel)
    assert len(removed) == 1 and removed[0].startswith('emsolver-slepc-solve-')
    assert list(tmp_path.iterdir()) == []
    executable, _, environment, _ = module._settings()
    completed = module.subprocess.run([executable, 'inspect', removed[0]], env=environment, capture_output=True)
    assert completed.returncode != 0


@real_slepc
def test_actual_launcher_deadline_is_enforced_and_private_files_are_removed(tmp_path):
    assert probe_slepc()['supported'] is True
    config = slepc_config()
    config['resources']['walltime_seconds'] = .001
    matrix = sparse.diags(np.linspace(-2, 3, 60), format='csr')
    with pytest.raises(RuntimeError, match='WALLTIME_EXCEEDED'):
        solve_slepc(matrix, config, tmp_path / 'published')
    assert list(tmp_path.iterdir()) == []


@real_slepc
@pytest.mark.parametrize('resource,value', [('memory_limit_gib', .02), ('disk_limit_gib', 1e-7)])
def test_actual_container_memory_and_private_disk_limits_are_enforced(tmp_path, resource, value):
    assert probe_slepc()['supported'] is True
    config = slepc_config()
    config['resources'][resource] = value
    matrix = sparse.diags([np.full(59, .02), np.linspace(-2, 3, 60), np.full(59, .02)], [-1, 0, 1], format='csr')
    with pytest.raises(RuntimeError, match='RESOURCE_EXHAUSTED'):
        solve_slepc(matrix, config, tmp_path / 'published')
    assert list(tmp_path.iterdir()) == []


@real_slepc
def test_actual_candidate_hash_tampering_is_rejected_before_arrays_are_used(tmp_path, monkeypatch):
    import emsolver.optional_backends as module
    assert probe_slepc()['supported'] is True
    actual = module._json
    def tamper(raw):
        value = actual(raw)
        if 'values_sha256' in value:
            value['values_sha256'] = '0' * 64
        return value
    monkeypatch.setattr(module, '_json', tamper)
    matrix = sparse.diags([np.full(59, .02), np.linspace(-2, 3, 60), np.full(59, .02)], [-1, 0, 1], format='csr')
    with pytest.raises(ValueError, match='INVALID_BACKEND_ARRAY'):
        solve_slepc(matrix, slepc_config(), tmp_path / 'published')
    assert list(tmp_path.iterdir()) == []


@real_slepc
def test_seeded_worker_reuses_exact_parent_profile_without_selftests_and_rejects_changes(monkeypatch):
    import emsolver.optional_backends as module
    parent = probe_slepc()
    assert parent['supported'] is True
    monkeypatch.setattr(module, '_SEED_REQUIRED', False)
    monkeypatch.setattr(module, '_SEEDED_PROFILE', None)
    monkeypatch.setattr(module, '_SEED_ENDPOINT', None)
    invoke = module._invoke_json
    calls = []
    def inspect_only(command, *args, **kwargs):
        assert 'run' not in command, 'Seeded worker cannot run global self-tests'
        calls.append(command)
        return invoke(command, *args, **kwargs)
    monkeypatch.setattr(module, '_invoke_json', inspect_only)
    module.seed_slepc_profile(parent)
    assert module.probe_slepc() == parent and calls
    for field in ('image_id', 'driver_sha256'):
        invalid = copy.deepcopy(parent)
        invalid[field] = 'sha256:' + '0' * 64 if field == 'image_id' else '0' * 64
        with pytest.raises(ValueError):
            module.seed_slepc_profile(invalid)
    invalid = copy.deepcopy(parent)
    invalid['private_path'] = '/outside/reference'
    with pytest.raises(ValueError):
        module.seed_slepc_profile(invalid)
    with pytest.raises(ValueError):
        module.seed_slepc_profile({'text': 'x' * (65536 + 1)})


@real_slepc
def test_external_worker_kill_is_followed_by_owned_container_cleanup(tmp_path):
    import psutil
    import emsolver.optional_backends as module
    assert probe_slepc()['supported'] is True
    script = '''from pathlib import Path
import sys,numpy as np
from scipy import sparse
from emsolver.optional_backends import solve_slepc
from emsolver.contracts import resolve_config
config=resolve_config({'target':{'k':3,'sigma':.1},'solver':{'backend':'slepc','method':'krylovschur','subspace_max':30},'resources':{'threads':1,'mpi_ranks':2,'memory_limit_gib':2,'walltime_seconds':30}})
n=100000
matrix=sparse.diags([np.full(n-1,.02),np.linspace(-2,3,n),np.full(n-1,.02)],[-1,0,1],format='csr')
solve_slepc(matrix,config,Path(sys.argv[1])/'artifacts')
'''
    executable, _, environment, _ = module._settings()
    process = subprocess.Popen([sys.executable, '-c', script, str(tmp_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    name = None
    paused = False
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and process.poll() is None:
            markers = list(tmp_path.glob('.slepc-*/container.json'))
            if markers:
                name = json.loads(markers[0].read_text())['name']
                inspected = subprocess.run([executable, 'inspect', '--format', '{{.State.Running}}', name], env=environment, capture_output=True)
                if inspected.returncode == 0 and inspected.stdout.strip() == b'true':
                    paused = subprocess.run([executable, 'pause', name], env=environment, capture_output=True).returncode == 0
                    if paused:
                        break
            time.sleep(.01)
        assert name is not None and paused and process.poll() is None
        parent = psutil.Process(process.pid)
        for child in parent.children(recursive=True):
            child.kill()
        process.kill(); process.wait(timeout=5)
        # This models the Service parent after its process-tree kill. A
        # container must not be mistaken for a descendant of the Docker CLI.
        result = cleanup_slepc(tmp_path)
        assert result['pending'] == 0 and result['containers_stopped'] == 1
        inspected = subprocess.run([executable, 'inspect', name], env=environment, capture_output=True)
        assert inspected.returncode != 0
        assert list(tmp_path.iterdir()) == []
    finally:
        if process.poll() is None:
            process.kill(); process.wait(timeout=5)
        if name:
            module._remove(executable, name, environment)
        cleanup_slepc(tmp_path)
