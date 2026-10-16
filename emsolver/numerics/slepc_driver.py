"""Standalone optional solver process. No main-package or reference imports."""
from __future__ import annotations

import argparse
import errno
import glob
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import sys
import time
import tempfile
import zipfile


MAX_JSON_BYTES = 1024**2
MAX_INPUT_BYTES = 1024**3
OOC_PROFILE = 'mumps_ooc_symmetric_v1'


def strict_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('DUPLICATE_FIELD')
        value[key] = item
    return value


def nonfinite(_):
    raise ValueError('NONFINITE_VALUE')


def read_spec(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_JSON_BYTES:
        raise ValueError('INVALID_SPECIFICATION')
    value = json.loads(path.read_bytes(), object_pairs_hook=strict_pairs, parse_constant=nonfinite)
    if (not isinstance(value, dict) or set(value) != {'schema_version', 'input_sha256', 'config'} or
            value['schema_version'] != '1.0' or not isinstance(value['input_sha256'], str) or
            not re.fullmatch(r'[0-9a-f]{64}', value['input_sha256'])):
        raise ValueError('INVALID_SPECIFICATION')
    config = value['config']
    fields = {'problem': {'hermitian_rtol'}, 'target': {'mode', 'k', 'sigma'},
              'solver': {'method', 'subspace_max', 'max_outer_iterations', 'tolerance', 'random_seed', 'monitor_enabled'},
              'resources': {'mpi_ranks', 'threads'}}
    ooc = isinstance(config, dict) and isinstance(config.get('solver'), dict) and 'factor_profile' in config['solver']
    if ooc:
        fields['solver'].add('factor_profile')
        fields['resources'].add('mumps_workspace_mb')
    if not isinstance(config, dict) or set(config) != set(fields):
        raise ValueError('INVALID_SPECIFICATION')
    for key, required in fields.items():
        if not isinstance(config[key], dict) or set(config[key]) != required:
            raise ValueError('INVALID_SPECIFICATION')
    if config['target']['mode'] != 'nearest_k' or config['solver']['method'] != 'krylovschur':
        raise ValueError('UNSUPPORTED_SOLVER_COMBINATION')
    for group, key, lower, upper in (('target', 'k', 1, 512), ('solver', 'subspace_max', 3, 1024),
                                    ('solver', 'max_outer_iterations', 1, 1000000),
                                    ('solver', 'random_seed', 0, 2**63 - 1),
                                    ('resources', 'mpi_ranks', 1, 16), ('resources', 'threads', 1, 256)):
        if type(config[group][key]) is not int or not lower <= config[group][key] <= upper:
            raise ValueError('INVALID_SPECIFICATION')
    if type(config['solver']['monitor_enabled']) is not bool:
        raise ValueError('INVALID_SPECIFICATION')
    if ooc and (config['solver']['factor_profile'] != OOC_PROFILE or
            config['resources']['mpi_ranks'] != 1 or config['resources']['threads'] != 1 or
            type(config['resources']['mumps_workspace_mb']) is not int or
            not 64 <= config['resources']['mumps_workspace_mb'] <= 768):
        raise ValueError('INVALID_SPECIFICATION')
    for group, key, positive in (('target', 'sigma', False), ('solver', 'tolerance', True),
                                  ('problem', 'hermitian_rtol', True)):
        number = config[group][key]
        if type(number) not in (int, float) or not math.isfinite(number) or (positive and number <= 0):
            raise ValueError('INVALID_SPECIFICATION')
    return value


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024**2), b''):
            digest.update(block)
    return digest.hexdigest()


def read_csr(path, expected_hash):
    """Accept only the main worker's bounded canonical CSR snapshot, no pickle."""
    import numpy as np
    from scipy import sparse
    path = Path(path)
    if (path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_INPUT_BYTES or
            file_hash(path) != expected_hash):
        raise ValueError('INPUT_CHANGED')
    required = {'indices.npy', 'indptr.npy', 'format.npy', 'shape.npy', 'data.npy'}
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if (len(entries) != len(required) or {entry.filename for entry in entries} != required or
                    sum(entry.file_size for entry in entries) > MAX_INPUT_BYTES):
                raise ValueError('INVALID_CSR_SNAPSHOT')
            for entry in entries:
                if entry.flag_bits & 1:
                    raise ValueError('INVALID_CSR_SNAPSHOT')
                with archive.open(entry) as stream:
                    version = np.lib.format.read_magic(stream)
                    reader = np.lib.format.read_array_header_1_0 if version == (1, 0) else np.lib.format.read_array_header_2_0
                    shape, _, dtype = reader(stream)
                    size = math.prod(shape) * dtype.itemsize
                    if dtype.hasobject or size > MAX_INPUT_BYTES or size > entry.file_size - stream.tell():
                        raise ValueError('INVALID_CSR_SNAPSHOT')
        with np.load(path, allow_pickle=False) as arrays:
            if arrays['format'].item() != b'csr':
                raise ValueError('INVALID_CSR_SNAPSHOT')
            shape = arrays['shape']
            if shape.shape != (2,) or shape.dtype.kind not in 'iu' or not 1 <= int(shape[0]) <= 1000000 or shape[0] != shape[1]:
                raise ValueError('INVALID_CSR_SNAPSHOT')
            data, indices, indptr = arrays['data'], arrays['indices'], arrays['indptr']
            n = int(shape[0])
            if (data.ndim != 1 or data.dtype.kind not in 'fc' or indices.ndim != 1 or indices.dtype.kind not in 'iu' or
                    indptr.shape != (n + 1,) or indptr.dtype.kind not in 'iu' or len(indices) != len(data) or
                    int(indptr[0]) != 0 or int(indptr[-1]) != len(data) or np.any(indptr[1:] < indptr[:-1]) or
                    np.any(indices < 0) or np.any(indices >= n) or not np.isfinite(data).all()):
                raise ValueError('INVALID_CSR_SNAPSHOT')
            matrix = sparse.csr_matrix((data.copy(), indices.copy(), indptr.copy()), shape=(n, n))
            if not matrix.has_canonical_format:
                raise ValueError('INVALID_CSR_SNAPSHOT')
            return matrix
    except (KeyError, OSError, TypeError, EOFError, zipfile.BadZipFile) as exc:
        raise ValueError('INVALID_CSR_SNAPSHOT') from exc


def _atomic_bytes(path, raw):
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError('RESULT_TOO_LARGE')
    temp = path.with_suffix(path.suffix + '.tmp')
    descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as output:
        output.write(raw)
    temp.replace(path)


def writable_csr_arrays(matrix, integer_type, scalar_type):
    """Keep canonical CSR buffers alive and writable while PETSc borrows them.

    PETSc may write to buffers even though no new nonzero positions are added.
    A read-only mmap is therefore unsafe; copy it before handing it to C.
    """
    import numpy as np
    if not matrix.has_canonical_format:
        raise ValueError('INVALID_CSR_SNAPSHOT')
    if np.iscomplexobj(matrix.data) and np.dtype(scalar_type).kind != 'c':
        raise ValueError('SCALAR_TYPE_MISMATCH')
    if matrix.nnz > np.iinfo(integer_type).max or matrix.shape[0] > np.iinfo(integer_type).max:
        raise ValueError('INVALID_CSR_SNAPSHOT')
    result = []
    for source, dtype in ((matrix.indptr, integer_type), (matrix.indices, integer_type), (matrix.data, scalar_type)):
        value = np.asarray(source, dtype=dtype, order='C')
        if not value.flags.writeable:
            value = value.copy()
        result.append(value)
    return tuple(result)


def _ooc_diagnostics(preconditioner, directory):
    diagnostics = {'factor_profile': OOC_PROFILE, 'mumps_symmetric_mode': 2,
                   'ooc_inertia_is_certificate': False}
    if directory is not None:
        files = [path for path in directory.iterdir() if path.is_file() and not path.is_symlink()]
        diagnostics.update(ooc_files_before_factor_destroy=len(files),
                           ooc_bytes_before_factor_destroy=sum(path.stat().st_size for path in files))
    if preconditioner is not None:
        try:
            factor = preconditioner.getFactorMatrix()
            diagnostics['mumps_icntl'] = {str(index): int(factor.getMumpsIcntl(index)) for index in (22, 23)}
            diagnostics['mumps_infog'] = {str(index): int(factor.getMumpsInfog(index))
                for index in (1, 2, 12, 16, 17, 20, 21, 22, 26, 27, 28, 29)}
        except Exception:
            # Setup can fail before PETSc has allocated a factor object.
            pass
    return diagnostics


def _write_ooc_failure(output, error, preconditioner, directory, stage):
    diagnostics = _ooc_diagnostics(preconditioner, directory)
    info = diagnostics.get('mumps_infog', {}).get('1')
    code, reason = 'SLEPC_DRIVER_FAILED', 'mumps_factor_or_solve_failed'
    if info in {-5, -7, -8, -9, -11, -12, -13, -14, -15}:
        code, reason = 'RESOURCE_EXHAUSTED', 'mumps_workspace_exhausted'
    elif info in {-6, -10}:
        code, reason = 'SLEPC_FACTORIZATION_FAILED', 'mumps_singular_factor'
    elif isinstance(error, OSError) and error.errno == errno.ENOSPC:
        code, reason = 'RESOURCE_EXHAUSTED', 'ooc_disk_exhausted'
    failure = {'schema_version': '1.0', 'error_code': code, 'failure_stage': stage,
               'reason': reason, 'diagnostics': diagnostics}
    if output is not None:
        _atomic_bytes(output / 'failure.json', json.dumps(failure, allow_nan=False).encode('utf-8'))
    return code


def solve_problem(matrix, config, output=None):
    import numpy as np
    from petsc4py import PETSc
    from slepc4py import SLEPc
    from mpi4py import MPI
    comm = MPI.COMM_WORLD
    if comm.size != config['resources']['mpi_ranks']:
        raise ValueError('MPI_RANK_MISMATCH')
    n = matrix.shape[0]
    k = config['target']['k']
    ncv = min(n, config['solver']['subspace_max'])
    if ncv <= k or k >= n:
        raise ValueError('SUBSPACE_LIMIT')
    scalar = np.dtype(PETSc.ScalarType)
    ooc = config['solver'].get('factor_profile') == OOC_PROFILE
    if ooc and (comm.size != 1 or config['resources']['threads'] != 1 or scalar.kind != 'f' or
                np.iscomplexobj(matrix.data) or output is None):
        raise ValueError('UNSUPPORTED_SLEPC_COMBINATION')
    if scalar.kind != 'c' and np.iscomplexobj(matrix.data) and np.any(matrix.data.imag):
        raise ValueError('SCALAR_TYPE_MISMATCH')
    maximum = float(np.max(np.abs(matrix.data), initial=0.))
    if maximum:
        difference = matrix - matrix.conjugate().T
        hermitian = float(np.max(np.abs(difference.data), initial=0.)) / maximum
        if not math.isfinite(hermitian) or hermitian > config['problem']['hermitian_rtol']:
            raise ValueError('NOT_HERMITIAN')
    buffers, ooc_directory = None, None
    if ooc:
        ooc_directory = output / 'ooc'
        ooc_directory.mkdir(mode=0o700)
        buffers = writable_csr_arrays(matrix, PETSc.IntType, PETSc.ScalarType)
        operator = PETSc.Mat().createAIJWithArrays(size=(n, n), csr=buffers, comm=PETSc.COMM_WORLD)
        operator.setOption(PETSc.Mat.Option.SYMMETRIC, True)
        operator.setOption(PETSc.Mat.Option.SPD, False)
        start, end = 0, n
    else:
        max_row = max(1, int(np.max(np.diff(matrix.indptr), initial=0)))
        operator = PETSc.Mat().createAIJ(size=(n, n), nnz=max_row, comm=PETSc.COMM_WORLD)
        start, end = operator.getOwnershipRange()
        for row in range(start, end):
            left, right = matrix.indptr[row:row + 2]
            operator.setValues(row, matrix.indices[left:right].astype(PETSc.IntType, copy=False),
                               matrix.data[left:right].astype(PETSc.ScalarType, copy=False))
        operator.assemble()
    operator.setOption(PETSc.Mat.Option.HERMITIAN, True)
    solver = SLEPc.EPS().create(PETSc.COMM_WORLD)
    solver.setOperators(operator)
    solver.setProblemType(SLEPc.EPS.ProblemType.HEP)
    solver.setType(SLEPc.EPS.Type.KRYLOVSCHUR)
    solver.setDimensions(k, ncv)
    solver.setTolerances(config['solver']['tolerance'], config['solver']['max_outer_iterations'])
    solver.setTarget(config['target']['sigma'])
    solver.setWhichEigenpairs(SLEPc.EPS.Which.TARGET_MAGNITUDE)
    initial = operator.createVecRight()
    rng = np.random.default_rng(config['solver']['random_seed'])
    seed = rng.standard_normal(n)
    if scalar.kind == 'c':
        seed = seed + 1j * rng.standard_normal(n)
    initial.getArray()[:] = seed[start:end]
    initial.assemble()
    solver.setInitialSpace(initial)
    del seed
    transform = solver.getST()
    transform.setType(SLEPc.ST.Type.SINVERT)
    transform.setShift(config['target']['sigma'])
    linear = transform.getKSP()
    linear.setType(PETSc.KSP.Type.PREONLY)
    preconditioner = linear.getPC()
    # For real AIJ with SPD explicitly false PETSc 3.18 chooses MUMPS sym=2.
    # PCCHOLESKY is the PETSc API name, not a positive-definiteness assumption.
    preconditioner.setType(PETSc.PC.Type.CHOLESKY if ooc else PETSc.PC.Type.LU)
    preconditioner.setFactorSolverType('mumps')
    option_restore, options = {}, None
    if ooc:
        linear.setErrorIfNotConverged(True)
        options = PETSc.Options()
        current_options = options.getAll()
        configured = {'st_mat_mumps_icntl_22': 1,
                      'st_mat_mumps_icntl_23': config['resources']['mumps_workspace_mb'],
                      'st_mat_mumps_ooc_tmpdir': str(ooc_directory), 'st_mat_mumps_icntl_4': 0}
        for key, value in configured.items():
            option_restore[key] = current_options.get(key)
            options[key] = value
    events, dropped = 0, 0
    event_file = None
    if output is not None and comm.rank == 0:
        descriptor = os.open(output / 'events.jsonl', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        event_file = os.fdopen(descriptor, 'wb')
    def monitor(_, iteration, converged, eigenvalues, errors):
        nonlocal events, dropped
        if comm.rank != 0 or event_file is None:
            return
        if events >= 256:
            dropped += 1
            return
        finite_errors = [float(error) for error in errors[:16] if np.isfinite(error)]
        value = {'stage': 'SOLVING', 'event': 'SLEPC_ITERATION', 'iteration': int(iteration),
                 'n_converged': int(converged), 'backend_error_estimate_max': max(finite_errors, default=None),
                 'residual_scope': 'SLEPc backend estimate; original-problem verification is performed by the main worker'}
        event_file.write(json.dumps(value, allow_nan=False).encode('utf-8') + b'\n')
        event_file.flush()
        events += 1
    if config['solver']['monitor_enabled']:
        solver.setMonitor(monitor)
    started = time.monotonic()
    stage = 'factorization_and_solve'
    try:
        solver.solve()
        elapsed = time.monotonic() - started
        converged = min(solver.getConverged(), 512)
        values, vectors = [], []
        stage = 'candidate_collection'
        real, imaginary = operator.createVecs()
        ranges = operator.getOwnershipRanges()
        counts, offsets = np.diff(ranges).astype('i'), np.asarray(ranges[:-1], dtype='i')
        mpi_type = MPI.C_DOUBLE_COMPLEX if scalar.kind == 'c' else MPI.DOUBLE
        for index in range(converged):
            value = solver.getEigenpair(index, real, imaginary)
            if not np.isfinite(value) or abs(value.imag) > 1e-10 * max(1., abs(value.real)):
                raise ValueError('NONREAL_HERMITIAN_EIGENVALUE')
            local = real.getArray(readonly=True).copy()
            global_vector = np.empty(n, dtype=PETSc.ScalarType) if comm.rank == 0 else None
            comm.Gatherv(local, (global_vector, counts, offsets, mpi_type) if comm.rank == 0 else None, root=0)
            if comm.rank == 0:
                values.append(float(value.real))
                vectors.append(global_vector)
        metrics = {'backend': 'slepc', 'method': solver.getType(), 'outer_iterations': solver.getIterationNumber(),
                   'converged_reason': int(solver.getConvergedReason()), 'n_converged': converged,
                   'mpi_ranks': comm.size, 'threads_per_rank': config['resources']['threads'],
                   'solve_seconds': elapsed, 'events_emitted': events, 'events_dropped': dropped,
                   'secondary_observation_enabled': config['solver']['monitor_enabled'],
                   'linear_method': linear.getType(), 'preconditioner': preconditioner.getType(),
                   'factor_solver': preconditioner.getFactorSolverType(),
                   'matrix_loading': 'replicated safe CSR input; distributed PETSc ownership and operator',
                   'candidate_collection': 'rank-zero gather of candidate vectors only'}
        if ooc:
            metrics.update(_ooc_diagnostics(preconditioner, ooc_directory))
            if metrics.get('mumps_icntl') != {'22': 1, '23': config['resources']['mumps_workspace_mb']}:
                raise ValueError('OOC_OPTIONS_NOT_APPLIED')
            metrics.update(matrix_loading='single-rank borrowed writable canonical CSR buffers',
                           factorization='symmetric_indefinite_ldlt', assumes_positive_definite=False)
        if comm.rank == 0:
            return np.asarray(values), np.column_stack(vectors) if vectors else np.empty((n, 0), dtype=PETSc.ScalarType), metrics
        return None
    except Exception as exc:
        if ooc:
            code = _write_ooc_failure(output, exc, preconditioner, ooc_directory, stage)
            raise ValueError(code) from exc
        raise
    finally:
        if event_file is not None:
            event_file.close()
        solver.destroy()
        initial.destroy()
        operator.destroy()
        # The arrays must outlive the PETSc matrix that borrows their pointers.
        buffers = None
        if options is not None:
            for key, previous in option_restore.items():
                if previous is None:
                    options.delValue(key)
                else:
                    options[key] = previous


def probe(*, ooc=False):
    from petsc4py import PETSc
    from slepc4py import SLEPc
    from mpi4py import MPI
    import numpy as np
    from scipy import sparse
    scalar = np.dtype(PETSc.ScalarType)
    n = 12
    off = np.full(n - 1, .02, dtype=PETSc.ScalarType)
    if scalar.kind == 'c':
        off *= np.exp(.3j)
    matrix = sparse.diags([off, np.linspace(-2., 3., n), off.conjugate()], [-1, 0, 1], format='csr')
    config = {'problem': {'hermitian_rtol': 1e-12}, 'target': {'mode': 'nearest_k', 'k': 2, 'sigma': .1},
              'solver': {'method': 'krylovschur', 'subspace_max': 10, 'max_outer_iterations': 100, 'tolerance': 1e-12,
                         'random_seed': 20261016, 'monitor_enabled': False},
              'resources': {'mpi_ranks': MPI.COMM_WORLD.size, 'threads': 1}}
    if ooc:
        if scalar.kind != 'f' or MPI.COMM_WORLD.size != 1:
            raise ValueError('UNSUPPORTED_SLEPC_COMBINATION')
        config['solver']['factor_profile'] = OOC_PROFILE
        config['resources']['mumps_workspace_mb'] = 64
        with tempfile.TemporaryDirectory(prefix='emsolver-ooc-probe-') as directory:
            result = solve_problem(matrix, config, Path(directory))
    else:
        result = solve_problem(matrix, config)
    if MPI.COMM_WORLD.rank != 0:
        return None
    values, vectors, metrics = result
    residual = float(np.max(np.abs(matrix @ vectors - vectors * values), initial=0.))
    expected = np.linalg.eigvalsh(matrix.toarray())
    order = np.argsort(abs(expected - .1))[:2]
    error = max((float(np.min(abs(values - value))) for value in expected[order]), default=float('inf'))
    passed = len(values) >= 2 and residual <= 1e-10 and error <= 1e-10
    if ooc:
        passed = (passed and metrics.get('mumps_symmetric_mode') == 2 and
                  metrics.get('mumps_icntl') == {'22': 1, '23': 64} and
                  metrics.get('mumps_infog', {}).get('12') == int(np.sum(expected < .1)))
    result = {'schema_version': '1.0', 'driver_sha256': file_hash(__file__),
            'platform': {'system': platform.system(), 'machine': platform.machine(), 'python': platform.python_version()},
            'available': True, 'self_test_passed': bool(passed),
            'petsc_version': list(PETSc.Sys.getVersion()), 'slepc_version': list(SLEPc.Sys.getVersion()),
            'scalar_dtype': scalar.name, 'integer_dtype': np.dtype(PETSc.IntType).name,
            'mpi_size': MPI.COMM_WORLD.size, 'mpi_library': MPI.Get_library_version().strip('\x00\n '),
            'mumps_available': bool(PETSc.Sys.hasExternalPackage('mumps')),
            'self_test': {'n': n, 'k': 2, 'max_absolute_residual': residual, 'max_eigenvalue_error': error,
                          'reference_scope': 'bounded 12x12 self-test only; no user/reference mount'}, 'metrics': metrics}
    if ooc:
        result['factor_profile'] = OOC_PROFILE
    return result


def select_bindings(scalar):
    """Debian supplies independent real/complex PETSc/SLEPc installations."""
    if scalar not in {'real', 'complex'}:
        raise ValueError('SCALAR_TYPE_UNSUPPORTED')
    for variable, directory, package in (('PETSC_DIR', '/usr/lib/petscdir', 'petsc'),
                                         ('SLEPC_DIR', '/usr/lib/slepcdir', 'slepc')):
        choices = sorted(glob.glob(directory + '/*/*-' + scalar))
        if not choices:
            raise ValueError('BACKEND_UNAVAILABLE')
        os.environ[variable] = choices[-1]
        sys.path.insert(0, choices[-1] + '/lib/python3/dist-packages')


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--probe', action='store_true')
    parser.add_argument('--probe-ooc', action='store_true')
    parser.add_argument('--scalar', choices=('real', 'complex'), default='complex')
    parser.add_argument('--spec', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    try:
        select_bindings(args.scalar)
        from petsc4py import PETSc
        from slepc4py import SLEPc
        from mpi4py import MPI
        import numpy as np
        if args.probe or args.probe_ooc:
            result = probe(ooc=args.probe_ooc)
            if result is not None:
                print(json.dumps(result, allow_nan=False), flush=True)
            passed = MPI.COMM_WORLD.bcast(result['self_test_passed'] if result else None, root=0)
            return 0 if passed else 1
        if args.spec is None or args.output is None or args.output.is_symlink() or not args.output.is_dir():
            raise ValueError('INVALID_OUTPUT_DIRECTORY')
        spec = read_spec(args.spec)
        source = args.spec.parent / 'matrix.npz'
        matrix = read_csr(source, spec['input_sha256'])
        result = solve_problem(matrix, spec['config'], args.output)
        if result is not None:
            values, vectors, metrics = result
            if file_hash(source) != spec['input_sha256']:
                raise ValueError('INPUT_CHANGED')
            for name, array in (('values.npy', values), ('vectors.npy', vectors)):
                path = args.output / name
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, 'wb') as stream:
                    np.save(stream, array, allow_pickle=False)
            _atomic_bytes(args.output / 'result.json', json.dumps({
                'schema_version': '1.0', 'candidate_status': 'CONVERGED' if metrics['converged_reason'] > 0 else 'INCOMPLETE',
                'input_sha256': spec['input_sha256'], 'n': matrix.shape[0], 'n_candidates': len(values),
                'values_sha256': file_hash(args.output / 'values.npy'), 'vectors_sha256': file_hash(args.output / 'vectors.npy'),
                'metrics': metrics, 'certification': 'CANDIDATES_ONLY_MAIN_WORKER_MUST_VERIFY_ORIGINAL_PROBLEM'},
                allow_nan=False).encode('utf-8'))
        return 0
    except Exception as exc:
        code = str(exc) if isinstance(exc, ValueError) and re.fullmatch(r'[A-Z_]{3,64}', str(exc)) else 'SLEPC_DRIVER_FAILED'
        if (args.output is not None and args.output.is_dir() and not args.output.is_symlink() and
                'spec' in locals() and spec['config']['solver'].get('factor_profile') == OOC_PROFILE and
                not (args.output / 'failure.json').exists()):
            code = _write_ooc_failure(args.output, exc, None, None, 'setup')
        print(json.dumps({'schema_version': '1.0', 'available': False, 'error_code': code}), flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
