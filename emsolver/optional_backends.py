"""Explicit local Docker SLEPc candidates; the main verifier remains authoritative."""
from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import uuid

from .contracts import resolve_config
from .integrity import file_hash
from .resource_budgets import memory_budgets


MAX_JSON_BYTES = 1024**2
OOC_PROFILE = 'mumps_ooc_symmetric_v1'
_CACHE = {}
_CACHE_LOCK = threading.Lock()
_SEED_REQUIRED = False
_SEEDED_PROFILE = None
_SEED_ENDPOINT = None


def _unavailable():
    return {'available': False, 'supported': False, 'methods': [], 'tested_combinations': [],
            'gpu': {'available': False, 'supported': False, 'reason': 'No CUDA device/backend has been validated.'}}


def _supported_profile(image_id, architecture, driver_hash, versions, *, ooc_tested=False):
    combinations = [{'scalar': scalar, 'mpi_ranks': ranks, 'threads_per_rank': 1,
                     'method': 'krylovschur', 'transform': 'shift_invert', 'linear_solver': 'preonly_lu_mumps',
                     'self_test': {'passed': True, 'n': 12, 'k': 2, 'absolute_residual_bound': 1e-10,
                                   'eigenvalue_error_bound': 1e-10}}
                    for scalar in ('float64', 'complex128') for ranks in (1, 2)]
    result = _unavailable()
    result.update(available=True, supported=True, methods=['krylovschur'], scalar_types=['float64', 'complex128'],
                  mpi_ranks=[1, 2], threads_per_rank=[1], image_id=image_id, architecture=architecture,
                  driver_sha256=driver_hash, versions=versions, tested_combinations=combinations,
                  target_modes=['nearest_k'], problem_types=['standard_hermitian'], devices=['cpu'],
                  limits={'max_matrix_dimension': 1000000, 'max_subspace': 1024, 'max_candidate_columns': 512,
                          'max_candidate_bytes': 512 * 1024**2, 'max_random_seed': 2**63 - 1},
                  certification='Only candidates; independent original-problem validation and target certification remain required.',
                  factor_profiles=['in_core_lu_v1'] + ([OOC_PROFILE] if ooc_tested else []),
                  matrix_loading='Input CSR is replicated within MPI ranks; PETSc matrix ownership is distributed.')
    return result


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('DUPLICATE_BACKEND_FIELD')
        value[key] = item
    return value


def _constant(_):
    raise ValueError('NONFINITE_BACKEND_FIELD')


def _json(raw):
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError('BACKEND_RESULT_TOO_LARGE')
    value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant)
    json.dumps(value, allow_nan=False)
    if not isinstance(value, dict):
        raise ValueError('INVALID_BACKEND_RESULT')
    return value


def _settings(*, require_image=True):
    image = os.environ.get('EMSOLVER_SLEPC_IMAGE', '')
    endpoint = os.environ.get('EMSOLVER_DOCKER_SOCKET', '')
    if (require_image and not image) or not endpoint:
        raise ValueError('BACKEND_NOT_CONFIGURED')
    if require_image and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/:@-]{0,254}', image):
        raise ValueError('INVALID_BACKEND_IMAGE')
    if endpoint.startswith('unix://'):
        endpoint = endpoint[7:]
    socket = Path(endpoint)
    if not socket.is_absolute() or socket.is_symlink() or socket != socket.resolve():
        raise ValueError('UNTRUSTED_DOCKER_SOCKET')
    info = socket.stat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError('UNTRUSTED_DOCKER_SOCKET')
    executable = shutil.which('docker')
    if executable is None:
        raise ValueError('DOCKER_UNAVAILABLE')
    environment = {key: os.environ[key] for key in ('PATH', 'LANG', 'LC_ALL') if key in os.environ}
    environment['DOCKER_HOST'] = 'unix://' + str(socket)
    return executable, image, environment, (str(socket), info.st_dev, info.st_ino)


def _invoke_json(command, environment, timeout=10):
    """Bound both output streams while invoking the explicit local daemon."""
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(command, env=environment, stdin=subprocess.DEVNULL,
                                   stdout=output, stderr=errors, start_new_session=True)
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                if time.monotonic() >= deadline or output.tell() > MAX_JSON_BYTES or errors.tell() > MAX_JSON_BYTES:
                    raise ValueError('BACKEND_PROBE_BUDGET_EXCEEDED')
                time.sleep(.02)
            if process.returncode != 0:
                raise ValueError('BACKEND_PROBE_FAILED')
            output.seek(0)
            return _json(output.read(MAX_JSON_BYTES + 1))
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)


def _image(executable, name, environment):
    value = _invoke_json([executable, 'image', 'inspect', '--format', '{{json .}}', name], environment)
    image_id = value.get('Id')
    if (not isinstance(image_id, str) or not re.fullmatch(r'sha256:[a-f0-9]{64}', image_id) or
            value.get('Os') != 'linux' or value.get('Architecture') not in {'arm64', 'amd64'}):
        raise ValueError('UNSUPPORTED_BACKEND_IMAGE')
    labels = (value.get('Config') or {}).get('Labels') or {}
    if labels.get('org.emsolver.slepc.protocol') != '1.0' or labels.get('org.emsolver.slepc.tested') != 'float64:1,2;complex128:1,2':
        raise ValueError('BACKEND_BUILD_PROFILE_MISSING')
    return image_id, value['Architecture']


def _validate_seed(profile):
    try:
        encoded = json.dumps(profile, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
        if len(encoded.encode('utf-8')) > 65536 or not isinstance(profile, dict):
            raise ValueError('INVALID_SLEPC_PROFILE')
        image_id, driver_hash = profile.get('image_id'), profile.get('driver_sha256')
        if (not isinstance(image_id, str) or not re.fullmatch(r'sha256:[a-f0-9]{64}', image_id) or
                not isinstance(driver_hash, str) or not re.fullmatch(r'[a-f0-9]{64}', driver_hash) or
                profile.get('architecture') not in {'arm64', 'amd64'}):
            raise ValueError('INVALID_SLEPC_PROFILE')
        versions = profile.get('versions')
        if not isinstance(versions, dict) or set(versions) != {'petsc_version', 'slepc_version', 'mpi_library', 'integer_dtype', 'platform'}:
            raise ValueError('INVALID_SLEPC_PROFILE')
        for key in ('petsc_version', 'slepc_version'):
            version = versions[key]
            if not isinstance(version, list) or len(version) != 3 or any(type(part) is not int or not 0 <= part <= 10000 for part in version):
                raise ValueError('INVALID_SLEPC_PROFILE')
        runtime = versions['platform']
        if (versions['integer_dtype'] != 'int32' or not isinstance(versions['mpi_library'], str) or
                not 1 <= len(versions['mpi_library']) <= 512 or any(ord(char) < 32 for char in versions['mpi_library']) or
                not isinstance(runtime, dict) or set(runtime) != {'system', 'machine', 'python'} or runtime['system'] != 'Linux' or
                runtime['machine'] not in {'aarch64', 'arm64', 'x86_64', 'amd64'} or
                not isinstance(runtime['python'], str) or not re.fullmatch(r'3\.[0-9]{1,2}\.[0-9]{1,3}', runtime['python'])):
            raise ValueError('INVALID_SLEPC_PROFILE')
        factor_profiles = profile.get('factor_profiles')
        ooc_tested = isinstance(factor_profiles, list) and OOC_PROFILE in factor_profiles
        expected = _supported_profile(image_id, profile['architecture'], driver_hash, versions, ooc_tested=ooc_tested)
        if encoded != json.dumps(expected, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False):
            raise ValueError('INVALID_SLEPC_PROFILE')
        return expected
    except (TypeError, KeyError, RecursionError, OverflowError) as error:
        raise ValueError('INVALID_SLEPC_PROFILE') from error


def seed_slepc_profile(profile):
    """Accept only the parent's server-owned, frozen capability proof.

    Calling this function enters worker mode even on rejection. A rejected or
    missing seed can never fall back to launching global self-tests in a job.
    The Service verifies equality to its frozen plan before invoking a worker.
    """
    global _SEED_REQUIRED, _SEEDED_PROFILE, _SEED_ENDPOINT
    _SEED_REQUIRED, _SEEDED_PROFILE, _SEED_ENDPOINT = True, None, None
    verified = _validate_seed(profile)
    executable, configured, environment, endpoint = _settings()
    image_id, architecture = _image(executable, configured, environment)
    if (image_id != verified['image_id'] or architecture != verified['architecture'] or
            file_hash(Path(__file__).parent / 'numerics' / 'slepc_driver.py') != verified['driver_sha256']):
        raise ValueError('SLEPC_PROFILE_MISMATCH')
    _SEEDED_PROFILE, _SEED_ENDPOINT = copy.deepcopy(verified), endpoint
    return copy.deepcopy(verified)


def driver_config(config):
    config = resolve_config(config)
    solver, target, resources = config['solver'], config['target'], config['resources']
    ooc = solver['linear']['preconditioner'] == OOC_PROFILE
    if (solver['backend'] != 'slepc' or solver['method'] != 'krylovschur' or solver['transform'] != 'shift_invert' or
            target['mode'] != 'nearest_k' or resources['device'] != 'cpu' or resources['mpi_ranks'] not in {1, 2} or
            resources['threads'] != 1 or config['problem']['type'] != 'standard_hermitian' or
            solver['fallback_profiles'] or solver['linear']['method'] != 'sparse_lu' or
            solver['linear']['precision'] != 'float64' or solver['linear']['preconditioner'] not in {'none', 'auto_budgeted', OOC_PROFILE} or
            solver['linear']['adaptive_accuracy']['enabled'] or solver['subspace_max'] > 1024 or
            target['k'] > 512 or solver['subspace_max'] <= target['k'] or solver['random_seed'] > 2**63 - 1 or
            solver['block']['checkpoint_interval'] or solver['block']['checkpoint_kind'] != 'subspace' or
            not config['output']['eigenvectors'] or config['problem'].get('matrix_b_id')):
        raise ValueError('UNSUPPORTED_SLEPC_COMBINATION')
    if ooc and (resources['mpi_ranks'] != 1 or config['problem']['scalar'] == 'complex128'):
        raise ValueError('UNSUPPORTED_SLEPC_COMBINATION')
    tolerance = min(1e-10, config['validation']['residual_scaled_max'] * .1)
    if tolerance <= 0:
        raise ValueError('NUMERICAL_TOLERANCE_LIMIT')
    result = {'problem': {'hermitian_rtol': config['problem']['hermitian_rtol']},
            'target': {'mode': target['mode'], 'k': target['k'], 'sigma': target['sigma']},
            'solver': {'method': 'krylovschur', 'subspace_max': solver['subspace_max'],
                       'max_outer_iterations': solver['max_outer_iterations'], 'tolerance': tolerance,
                       'random_seed': solver['random_seed'], 'monitor_enabled': config['observability']['enabled']},
            'resources': {'mpi_ranks': resources['mpi_ranks'], 'threads': resources['threads']}}
    if ooc:
        workspace = min(768, math.floor(memory_budgets(config)['container_bytes'] * .35 / 1024**2))
        if workspace < 64:
            raise RuntimeError('RESOURCE_EXHAUSTED: OOC factorization needs at least 64 MiB workspace')
        result['solver']['factor_profile'] = OOC_PROFILE
        result['resources']['mumps_workspace_mb'] = workspace
    return result


def sandbox_command(executable, image_id, name, config, *, scalar, inputs=None, outputs=None, probe=False, probe_ooc=False):
    config = resolve_config(config)
    driver_config(config)
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', image_id) or not re.fullmatch(r'emsolver-slepc-[a-z0-9-]{1,80}', name):
        raise ValueError('INVALID_BACKEND_IDENTITY')
    if scalar not in {'real', 'complex'}:
        raise ValueError('INVALID_BACKEND_SCALAR')
    if config['solver']['linear']['preconditioner'] == OOC_PROFILE and scalar != 'real':
        raise ValueError('UNSUPPORTED_SLEPC_COMBINATION')
    resources = config['resources']
    memory = memory_budgets(config)['container_bytes']
    if memory <= 0:
        raise RuntimeError('RESOURCE_EXHAUSTED: no container memory budget remains')
    command = [executable, 'run', '--pull=never', '--name', name, '--network=none', '--read-only',
               '--cap-drop=ALL', '--security-opt=no-new-privileges', '--pids-limit=128',
               '--memory', str(memory), '--memory-swap', str(memory), '--cpus', str(resources['threads'] * resources['mpi_ranks']),
               '--user', f'{os.getuid()}:{os.getgid()}', '--tmpfs', '/tmp:rw,nosuid,size=64m,mode=1777',
               '--env', 'HOME=/tmp', '--env', f'OMP_NUM_THREADS={resources["threads"]}',
               '--env', f'OPENBLAS_NUM_THREADS={resources["threads"]}', '--env', f'MKL_NUM_THREADS={resources["threads"]}']
    if not (probe or probe_ooc):
        for path, target, readonly in ((inputs, '/input', True), (outputs, '/output', False)):
            path = Path(path).resolve()
            if not path.is_dir() or any(character in str(path) for character in ',\n\r'):
                raise ValueError('UNSAFE_BACKEND_MOUNT')
            command += ['--mount', f'type=bind,source={path},target={target}' + (',readonly' if readonly else '')]
    # The in-container watchdog survives abrupt loss of the host worker. A
    # force-killed client must not leave an indefinitely computing container.
    command += ['--entrypoint', '/usr/bin/timeout', image_id, '--signal=TERM', '--kill-after=2',
                str(resources['walltime_seconds']) + 's', '/usr/bin/mpiexec', '-n', str(resources['mpi_ranks']),
                '/usr/bin/python3', '/opt/emsolver/slepc_driver.py', '--scalar', scalar]
    command += ['--probe-ooc'] if probe_ooc else ['--probe'] if probe else ['--spec', '/input/spec.json', '--output', '/output']
    return command


def _remove(executable, name, environment):
    for attempt in range(3):
        try:
            result = subprocess.run([executable, 'rm', '-f', name], env=environment, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5, check=False)
            if result.returncode == 0:
                return True
            if b'no such container' not in result.stderr.lower():
                return False
            # A killed client may have left a create/start request in flight.
            if attempt == 2:
                return True
            time.sleep(.05)
        except (OSError, subprocess.SubprocessError):
            return False
    return False


def cleanup_slepc(run_dir):
    """Parent-side cleanup after a worker is killed or cannot be reattached.

    Only this job's 0700 UUID workspaces and exact matching container markers
    authorize daemon actions. No global container listing or prune is used.
    """
    result = {'containers_stopped': 0, 'pending': 0}
    directory = Path(run_dir)
    if directory.is_symlink() or not directory.is_dir():
        return result
    try:
        executable, _, environment, _ = _settings(require_image=False)
    except (OSError, ValueError):
        result['pending'] = sum(bool(re.fullmatch(r'\.slepc-[a-f0-9]{32}', path.name)) for path in directory.iterdir())
        return result
    for workspace in directory.iterdir():
        match = re.fullmatch(r'\.slepc-([a-f0-9]{32})', workspace.name)
        if not match:
            continue
        if workspace.is_symlink() or not workspace.is_dir() or workspace.stat().st_uid != os.getuid():
            result['pending'] += 1
            continue
        marker = workspace / 'container.json'
        try:
            if (marker.is_symlink() or not marker.is_file() or marker.stat().st_uid != os.getuid() or
                    marker.stat().st_size > 4096):
                raise ValueError('INVALID_CONTAINER_MARKER')
            value = _json(marker.read_bytes())
            name = 'emsolver-slepc-solve-' + match.group(1)
            if (set(value) != {'schema_version', 'name', 'image_id'} or value['schema_version'] != '1.0' or
                    value['name'] != name or not re.fullmatch(r'sha256:[a-f0-9]{64}', value['image_id'])):
                raise ValueError('INVALID_CONTAINER_MARKER')
            if not _remove(executable, name, environment):
                raise ValueError('CONTAINER_CLEANUP_PENDING')
            shutil.rmtree(workspace)
            result['containers_stopped'] += 1
        except (OSError, ValueError, TypeError, RecursionError):
            result['pending'] += 1
    return result


def probe_slepc():
    result = _unavailable()
    if _SEED_REQUIRED:
        if _SEEDED_PROFILE is None:
            result['reason'] = 'SLEPC_PROFILE_REQUIRED'
            return result
        try:
            executable, configured, environment, endpoint = _settings()
            image_id, architecture = _image(executable, configured, environment)
            if (endpoint != _SEED_ENDPOINT or image_id != _SEEDED_PROFILE['image_id'] or
                    architecture != _SEEDED_PROFILE['architecture'] or
                    file_hash(Path(__file__).parent / 'numerics' / 'slepc_driver.py') != _SEEDED_PROFILE['driver_sha256']):
                raise ValueError('SLEPC_PROFILE_MISMATCH')
            return copy.deepcopy(_SEEDED_PROFILE)
        except (OSError, ValueError, TypeError, subprocess.SubprocessError):
            result['reason'] = 'SLEPC_PROFILE_MISMATCH'
            return result
    try:
        executable, configured, environment, endpoint = _settings()
        image_id, architecture = _image(executable, configured, environment)
        driver_hash = file_hash(Path(__file__).parent / 'numerics' / 'slepc_driver.py')
        key = (image_id, endpoint, driver_hash)
        with _CACHE_LOCK:
            cached = _CACHE.get(key)
            if cached is not None and time.monotonic() - cached[0] < 30:
                return copy.deepcopy(cached[1])
            combinations, versions = [], None
            for scalar in ('real', 'complex'):
                for ranks in (1, 2):
                    config = resolve_config({'solver': {'backend': 'slepc', 'method': 'krylovschur'},
                                             'resources': {'threads': 1, 'mpi_ranks': ranks, 'memory_limit_gib': 1, 'walltime_seconds': 15}})
                    name = 'emsolver-slepc-probe-' + uuid.uuid4().hex
                    try:
                        value = _invoke_json(sandbox_command(executable, image_id, name, config, scalar=scalar, probe=True), environment, timeout=20)
                    finally:
                        _remove(executable, name, environment)
                    expected_dtype = 'float64' if scalar == 'real' else 'complex128'
                    if (value.get('schema_version') != '1.0' or value.get('driver_sha256') != driver_hash or
                            value.get('self_test_passed') is not True or value.get('scalar_dtype') != expected_dtype or
                            value.get('mpi_size') != ranks or value.get('mumps_available') is not True or
                            value.get('platform', {}).get('system') != 'Linux'):
                        raise ValueError('BACKEND_SELF_TEST_FAILED')
                    combinations.append({'scalar': expected_dtype, 'mpi_ranks': ranks, 'threads_per_rank': 1,
                                         'method': 'krylovschur', 'transform': 'shift_invert', 'linear_solver': 'preonly_lu_mumps',
                                         'self_test': {'passed': True, 'n': 12, 'k': 2, 'absolute_residual_bound': 1e-10,
                                                       'eigenvalue_error_bound': 1e-10}})
                    versions = {key: value[key] for key in ('petsc_version', 'slepc_version', 'mpi_library', 'integer_dtype', 'platform')}
            # The old four combinations remain independently usable if the
            # additional real symmetric-indefinite OOC self-test fails.
            ooc_tested = False
            config = resolve_config({'solver': {'backend': 'slepc', 'method': 'krylovschur',
                                                'linear': {'preconditioner': OOC_PROFILE}},
                                     'resources': {'threads': 1, 'mpi_ranks': 1,
                                                   'memory_limit_gib': 1, 'walltime_seconds': 15}})
            name = 'emsolver-slepc-probe-ooc-' + uuid.uuid4().hex
            try:
                value = _invoke_json(sandbox_command(executable, image_id, name, config,
                    scalar='real', probe_ooc=True), environment, timeout=20)
                ooc_tested = (value.get('driver_sha256') == driver_hash and
                    value.get('self_test_passed') is True and value.get('factor_profile') == OOC_PROFILE and
                    value.get('scalar_dtype') == 'float64' and value.get('mpi_size') == 1 and
                    value.get('metrics', {}).get('mumps_symmetric_mode') == 2 and
                    value.get('metrics', {}).get('mumps_icntl') == {'22': 1, '23': 64})
            except (OSError, ValueError, TypeError, subprocess.SubprocessError):
                pass
            finally:
                _remove(executable, name, environment)
            result = _supported_profile(image_id, architecture, driver_hash, versions, ooc_tested=ooc_tested)
            _CACHE[key] = (time.monotonic(), copy.deepcopy(result))
            return result
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError):
        result['reason'] = 'Explicit local image/socket and all real/complex 1/2-rank self-tests are required.'
        return result


def _read_events(path, callback, offset):
    if callback is None or not path.is_file() or path.is_symlink():
        return offset
    if path.stat().st_size > MAX_JSON_BYTES:
        raise ValueError('BACKEND_EVENT_BUDGET_EXCEEDED')
    with path.open('rb') as source:
        source.seek(offset)
        for _ in range(256):
            line = source.readline(4097)
            if not line:
                break
            if len(line) > 4096:
                raise ValueError('INVALID_BACKEND_EVENT')
            if not line.endswith(b'\n'):
                break
            value = _json(line)
            allowed = {'stage', 'event', 'iteration', 'n_converged', 'backend_error_estimate_max', 'residual_scope'}
            if set(value) - allowed or value.get('event') != 'SLEPC_ITERATION':
                raise ValueError('INVALID_BACKEND_EVENT')
            callback(value)
            offset = source.tell()
    return offset


def read_driver_failure(path):
    """Accept a bounded numerical diagnosis, never arbitrary driver text."""
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 32768:
        raise ValueError('INVALID_BACKEND_FAILURE')
    failure = _json(path.read_bytes())
    if (set(failure) != {'schema_version', 'error_code', 'failure_stage', 'reason', 'diagnostics'} or
            failure['schema_version'] != '1.0' or failure['error_code'] not in
            {'RESOURCE_EXHAUSTED', 'SLEPC_FACTORIZATION_FAILED', 'SLEPC_DRIVER_FAILED', 'UNSUPPORTED_SLEPC_COMBINATION'} or
            failure['failure_stage'] not in {'setup', 'factorization_and_solve', 'candidate_collection'} or
            failure['reason'] not in {'mumps_workspace_exhausted', 'mumps_singular_factor',
                'mumps_factor_or_solve_failed', 'ooc_disk_exhausted', 'invalid_ooc_profile'} or
            not isinstance(failure['diagnostics'], dict)):
        raise ValueError('INVALID_BACKEND_FAILURE')
    diagnostics = failure['diagnostics']
    numeric = {'mumps_symmetric_mode', 'ooc_bytes_before_factor_destroy', 'ooc_files_before_factor_destroy'}
    if set(diagnostics) - (numeric | {'factor_profile', 'mumps_icntl', 'mumps_infog', 'ooc_inertia_is_certificate'}):
        raise ValueError('INVALID_BACKEND_FAILURE')
    for key in numeric & set(diagnostics):
        if type(diagnostics[key]) is not int or not 0 <= diagnostics[key] < 2**63:
            raise ValueError('INVALID_BACKEND_FAILURE')
    if ('factor_profile' in diagnostics and diagnostics['factor_profile'] != OOC_PROFILE or
            'ooc_inertia_is_certificate' in diagnostics and diagnostics['ooc_inertia_is_certificate'] is not False):
        raise ValueError('INVALID_BACKEND_FAILURE')
    for key, allowed in (('mumps_icntl', {'22', '23'}),
                         ('mumps_infog', {'1', '2', '12', '16', '17', '20', '21', '22', '26', '27', '28', '29'})):
        if key in diagnostics:
            values = diagnostics[key]
            if (not isinstance(values, dict) or set(values) - allowed or
                    any(type(item) is not int or not -2**63 < item < 2**63 for item in values.values())):
                raise ValueError('INVALID_BACKEND_FAILURE')
    return failure


def private_directory_bytes(directory, byte_limit):
    """Stop counting at the budget; factors can disappear during teardown."""
    total, pending = 0, [Path(directory)]
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except (FileNotFoundError, NotADirectoryError):
                        continue
                    if stat.S_ISDIR(info.st_mode):
                        pending.append(entry.path)
                    elif stat.S_ISREG(info.st_mode):
                        total += info.st_size
                        if total > byte_limit:
                            return total
        except (FileNotFoundError, NotADirectoryError):
            continue
    return total


def _check_private_budgets(workspace, disk_limit_bytes):
    for filename in ('stdout.log', 'stderr.log'):
        try:
            info = (workspace / filename).stat(follow_symlinks=False)
        except FileNotFoundError:
            continue
        if stat.S_ISREG(info.st_mode) and info.st_size > MAX_JSON_BYTES:
            raise ValueError('BACKEND_LOG_BUDGET_EXCEEDED')
    if private_directory_bytes(workspace, disk_limit_bytes) > disk_limit_bytes:
        raise RuntimeError('RESOURCE_EXHAUSTED: SLEPc private output exceeded disk budget')


def solve_slepc(input_path, config, outputdir, event_callback=None, check=None):
    """Return raw candidates in original units/row order, never a certificate.

    input_path may be a safely loaded original CSR supplied by the numerical
    adapter, or an input path whose normalization is delegated to load_matrix.
    Controls are private siblings of outputdir, never published artifacts.
    """
    import numpy as np
    from scipy import sparse
    config = resolve_config(config)
    subset = driver_config(config)
    started = time.monotonic()
    if check:
        check()
    capability = probe_slepc()
    if not capability['supported']:
        raise ValueError('BACKEND_UNAVAILABLE: validated local SLEPc profile is unavailable')
    ooc = config['solver']['linear']['preconditioner'] == OOC_PROFILE
    if ooc and OOC_PROFILE not in capability.get('factor_profiles', []):
        raise ValueError('BACKEND_UNAVAILABLE: OOC profile requires its own passed runtime self-test')
    executable, _, environment, _ = _settings()
    image_id = capability['image_id']
    if sparse.issparse(input_path):
        matrix = sparse.csr_matrix(input_path, copy=True)
    else:
        source = Path(input_path)
        if source.is_symlink():
            raise ValueError('UNSAFE_BACKEND_INPUT')
        from .numerics import load_matrix
        matrix = load_matrix(source, storage=config['problem']['storage'], hermitian_rtol=config['problem']['hermitian_rtol'])
    matrix = matrix.astype(np.complex128 if np.iscomplexobj(matrix.data) else np.float64)
    if ooc and np.iscomplexobj(matrix.data):
        raise ValueError('UNSUPPORTED_SLEPC_COMBINATION')
    if config['problem']['scalar'] == 'float64' and np.iscomplexobj(matrix.data):
        raise ValueError('SCALAR_TYPE_MISMATCH')
    matrix.sum_duplicates(); matrix.sort_indices(); matrix.eliminate_zeros()
    n, k = matrix.shape[0], config['target']['k']
    budgets = memory_budgets(config)
    limit = min(budgets['host_bytes'] / 4, 512 * 1024**2)
    if n * min(512, config['solver']['subspace_max']) * 16 > limit:
        raise ValueError('CANDIDATE_OUTPUT_BUDGET_EXCEEDED')
    scalar = 'complex' if np.iscomplexobj(matrix.data) or config['problem']['scalar'] == 'complex128' else 'real'
    parent = Path(outputdir).resolve().parent
    parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    workspace = parent / ('.slepc-' + token)
    workspace.mkdir(mode=0o700)
    inputs, outputs = workspace / 'input', workspace / 'output'
    inputs.mkdir(mode=0o700); outputs.mkdir(mode=0o700)
    process, name = None, 'emsolver-slepc-solve-' + token
    try:
        marker = workspace / 'container.json'
        descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump({'schema_version': '1.0', 'name': name, 'image_id': image_id}, stream)
        snapshot = inputs / 'matrix.npz'
        sparse.save_npz(snapshot, matrix)
        snapshot.chmod(0o400)
        digest = file_hash(snapshot)
        spec = {'schema_version': '1.0', 'input_sha256': digest, 'config': subset}
        spec_path = inputs / 'spec.json'
        spec_path.write_text(json.dumps(spec, allow_nan=False), encoding='utf-8'); spec_path.chmod(0o400)
        command = sandbox_command(executable, image_id, name, config, scalar=scalar, inputs=inputs, outputs=outputs)
        if check:
            check()
        if time.monotonic() - started >= config['resources']['walltime_seconds']:
            raise RuntimeError('WALLTIME_EXCEEDED: SLEPc candidate deadline reached before launch')
        disk_limit_bytes = int(config['resources']['disk_limit_gib'] * 1024**3)
        _check_private_budgets(workspace, disk_limit_bytes)
        with (workspace / 'stdout.log').open('wb') as stdout, (workspace / 'stderr.log').open('wb') as stderr:
            process = subprocess.Popen(command, env=environment, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, start_new_session=True)
            offset = 0
            while process.poll() is None:
                if check:
                    check()
                if time.monotonic() - started >= config['resources']['walltime_seconds']:
                    raise RuntimeError('WALLTIME_EXCEEDED: SLEPc candidate deadline reached')
                _check_private_budgets(workspace, disk_limit_bytes)
                offset = _read_events(outputs / 'events.jsonl', event_callback, offset)
                time.sleep(.02)
            # A short-lived child may finish before the first poll. Its final
            # logs and private products must pass the same bounds before use.
            _check_private_budgets(workspace, disk_limit_bytes)
            _read_events(outputs / 'events.jsonl', event_callback, offset)
        if check:
            check()
        if time.monotonic() - started >= config['resources']['walltime_seconds'] or process.returncode == 124:
            raise RuntimeError('WALLTIME_EXCEEDED: SLEPc candidate deadline reached')
        if process.returncode != 0:
            try:
                state = _invoke_json([executable, 'inspect', '--format', '{{json .State}}', name], environment)
            except ValueError:
                state = {}
            if state.get('OOMKilled'):
                raise RuntimeError('RESOURCE_EXHAUSTED: SLEPc container memory limit reached')
            if ooc and (outputs / 'failure.json').exists():
                failure = read_driver_failure(outputs / 'failure.json')
                from .reporting import atomic_text
                Path(outputdir).mkdir(parents=True, exist_ok=True)
                atomic_text(Path(outputdir) / 'slepc.failure.json', json.dumps(failure, allow_nan=False))
                if failure['error_code'] == 'RESOURCE_EXHAUSTED':
                    raise RuntimeError('RESOURCE_EXHAUSTED: ' + failure['reason'])
                raise ValueError(failure['error_code'])
            raise ValueError('SLEPC_EXECUTION_FAILED')
        result_path = outputs / 'result.json'
        if result_path.is_symlink() or not result_path.is_file() or result_path.stat().st_size > MAX_JSON_BYTES:
            raise ValueError('INVALID_BACKEND_RESULT')
        result = _json(result_path.read_bytes())
        expected = {'schema_version', 'candidate_status', 'input_sha256', 'n', 'n_candidates', 'values_sha256', 'vectors_sha256', 'metrics', 'certification'}
        count = result.get('n_candidates')
        if (set(result) != expected or result['schema_version'] != '1.0' or result['input_sha256'] != digest or result['n'] != n or
                type(count) is not int or not 0 <= count <= 512 or result['candidate_status'] not in {'CONVERGED', 'INCOMPLETE'} or
                result['certification'] != 'CANDIDATES_ONLY_MAIN_WORKER_MUST_VERIFY_ORIGINAL_PROBLEM' or file_hash(snapshot) != digest):
            raise ValueError('INVALID_BACKEND_RESULT')
        arrays = []
        for filename, key, shape, max_bytes in (('values.npy', 'values_sha256', (count,), count * 8 + 4096),
                                                ('vectors.npy', 'vectors_sha256', (n, count), n * count * 16 + 4096)):
            path = outputs / filename
            if (path.is_symlink() or not path.is_file() or path.stat().st_size > max_bytes or
                    not isinstance(result[key], str) or not re.fullmatch(r'[a-f0-9]{64}', result[key]) or file_hash(path) != result[key]):
                raise ValueError('INVALID_BACKEND_ARRAY')
            array = np.load(path, allow_pickle=False, mmap_mode='r')
            if array.shape != shape or array.dtype.kind not in 'fc' or not np.isfinite(array).all():
                raise ValueError('INVALID_BACKEND_ARRAY')
            arrays.append(np.asarray(array).copy())
        if np.iscomplexobj(arrays[0]):
            raise ValueError('INVALID_BACKEND_EIGENVALUES')
        metrics = result['metrics']
        if (not isinstance(metrics, dict) or metrics.get('backend') != 'slepc' or metrics.get('method') != 'krylovschur' or
                metrics.get('mpi_ranks') != config['resources']['mpi_ranks']):
            raise ValueError('INVALID_BACKEND_METRICS')
        metrics.update(container_image_id=image_id, container_architecture=capability['architecture'],
                       container_memory_limit_bytes=budgets['container_bytes'],
                       host_memory_limit_bytes=budgets['host_bytes'], total_job_memory_limit_bytes=budgets['total_bytes'],
                       memory_enforcement='One job budget: host process tree and container each receive half; container swap capped at its memory share',
                       container_memory_measurement='Kernel cgroup hard limit; host process-tree RSS excludes container processes',
                       scalar_dtype=arrays[1].dtype.name)
        strategy = {'actual_backend': 'slepc', 'actual_method': 'krylovschur', 'transform': 'shift_invert',
                    'linear_solver': {'method': 'preonly', 'preconditioner': 'lu', 'factor_solver': 'mumps'},
                    'shift_policy': {'applies': False, 'reason': 'This adapter uses one fixed ST shift; no project adaptive working-shift retry is executed'},
                    'initial_space': {'policy': 'seeded_backend_input_row_order_random_vector', 'random_seed': config['solver']['random_seed']},
                    'mpi_ranks': config['resources']['mpi_ranks'], 'threads_per_rank': 1, 'device': 'cpu',
                    'versions': capability['versions'], 'image_id': image_id, 'driver_sha256': capability['driver_sha256'],
                    'verification_input': 'original_unpermuted_matrix',
                    'candidate_certification': 'Main worker must recompute original-problem residuals and target certification',
                    'limitations': ['Replicated sparse input loading on each MPI rank', 'No validated GPU path',
                                    'SLEPc backend error estimates do not replace original-problem verification']}
        if ooc:
            strategy['linear_solver'].update(preconditioner='cholesky', factor_profile=OOC_PROFILE,
                factorization='symmetric_indefinite_ldlt', assumes_positive_definite=False,
                out_of_core=True, mumps_workspace_mb=subset['resources']['mumps_workspace_mb'])
        return {'values': arrays[0], 'vectors': arrays[1], 'metrics': metrics, 'strategy': strategy,
                'candidate_status': result['candidate_status'],
                'attempts': [{'method': 'krylovschur', 'sigma_target': config['target']['sigma'],
                              'sigma_work': config['target']['sigma'], 'status': result['candidate_status'], 'n_accepted': count}]}
    finally:
        if process is not None and process.poll() is None:
            process.kill(); process.wait(timeout=3)
        if _remove(executable, name, environment):
            shutil.rmtree(workspace, ignore_errors=True)
