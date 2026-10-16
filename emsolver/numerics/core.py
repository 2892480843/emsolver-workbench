from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import platform
import sys
import time
import zipfile
from collections import deque
from fractions import Fraction
from pathlib import Path

import numpy as np
import psutil
import scipy
import yaml
from scipy import sparse
from scipy.linalg import eigvalsh_tridiagonal
from scipy.sparse import linalg as sla
from threadpoolctl import threadpool_info, threadpool_limits


_MAX_MODES = 512
_MAX_SUBSPACE = 1024


def _threadpools():
    return [{key:value for key, value in info.items() if key != 'filepath'} for info in threadpool_info()]


def _error(code, detail):
    raise ValueError(f'{code}: {detail}')


def _exception_code(exc, fallback='FACTORIZATION_FAILED'):
    code = str(exc).split(':', 1)[0]
    return code if code.replace('_', '').isalpha() and code.isupper() else fallback


def _clean(value):
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, np.ndarray):
        return _clean(value.tolist())
    if isinstance(value, np.generic):
        return _clean(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def _json(path, value):
    path.write_text(json.dumps(_clean(value), ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')


def _directory_bytes(root):
    root = Path(root)
    if not root.exists():
        return 0
    total = 0
    for path in root.rglob('*'):
        if path.is_symlink():
            _error('INVALID_OUTPUT','symbolic links are not allowed in numerical output')
        if path.is_file():
            total += path.stat().st_size
    return total


def _check_disk_budget(root,config,reserve_bytes=0):
    if _directory_bytes(root)+reserve_bytes > config['resources']['disk_limit_gib']*1024**3:
        raise RuntimeError('RESOURCE_EXHAUSTED: numerical output and checkpoint exceed disk budget')


def _numeric(a, field):
    if a.dtype.kind not in 'biufc' or a.dtype.hasobject:
        _error('INVALID_DTYPE', field)
    if not np.isfinite(a).all():
        _error('NONFINITE_INPUT', field)


def _check_npy_header(stream, size, max_bytes):
    """Check advertised array size before NumPy can allocate from a forged header."""
    try:
        version = np.lib.format.read_magic(stream)
        if version == (1, 0):
            shape, _, dtype = np.lib.format.read_array_header_1_0(stream)
        elif version in ((2, 0), (3, 0)):
            shape, _, dtype = np.lib.format.read_array_header_2_0(stream)
        else:
            _error('INVALID_ARCHIVE', 'unsupported NPY header')
        if dtype.hasobject or dtype.kind not in 'biufcSU':
            _error('INVALID_DTYPE', 'unsafe array dtype')
        count = math.prod(shape)
        nbytes = count*dtype.itemsize
        if nbytes > max_bytes:
            _error('INPUT_BUDGET_EXCEEDED', 'advertised array allocation')
        if stream.tell()+nbytes != size:
            _error('INVALID_ARCHIVE', 'array payload does not match header')
    except (EOFError, OSError, TypeError) as exc:
        raise ValueError('INVALID_ARCHIVE: malformed NPY header') from exc


def _read_npz(path, max_bytes):
    allowed = {'data', 'indices', 'indptr', 'shape', 'format', 'schema_version', 'row', 'col', '_is_array'}
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if not infos or len(infos) > 12 or sum(x.file_size for x in infos) > max_bytes:
            _error('INPUT_BUDGET_EXCEEDED', 'uncompressed archive size')
        names = [i.filename for i in infos]
        if len(names) != len(set(names)):
            _error('INVALID_ARCHIVE', 'duplicate fields')
        for name in names:
            if '/' in name or '\\' in name or not name.endswith('.npy') or name[:-4] not in allowed:
                _error('INVALID_ARCHIVE', 'unexpected archive field')
        for info in infos:
            with archive.open(info) as member:
                _check_npy_header(member, info.file_size, max_bytes)
    with np.load(path, allow_pickle=False) as archive:
        values = {key: archive[key] for key in archive.files}
    if not {'data', 'shape', 'format'} <= values.keys():
        _error('INVALID_SCHEMA', 'missing sparse fields')
    shape = values['shape']
    if shape.shape != (2,) or shape.dtype.kind not in 'iu':
        _error('INVALID_SHAPE', 'shape must contain two integers')
    shape = tuple(int(x) for x in shape)
    if shape[0] <= 0 or shape[0] != shape[1]:
        _error('INVALID_SHAPE', 'nonempty square matrix required')
    if (shape[0]+1)*8 > max_bytes:
        _error('INPUT_BUDGET_EXCEEDED', 'CSR row-pointer allocation exceeds import budget')
    fmt_array = values['format']
    if fmt_array.shape != () or fmt_array.dtype.kind not in 'SU':
        _error('INVALID_FORMAT', 'format must be a string scalar')
    fmt = fmt_array.item()
    if isinstance(fmt, bytes):
        fmt = fmt.decode('ascii')
    if 'schema_version' in values and str(values['schema_version'].item()) != '1.0':
        _error('INVALID_SCHEMA', 'unsupported schema_version')
    data = values['data']
    _numeric(data, 'data')
    if data.ndim != 1:
        _error('INVALID_STRUCTURE', 'data must be one-dimensional')
    if (shape[0]+1)*8+len(data)*(max(data.dtype.itemsize, 8)+8) > max_bytes:
        _error('INPUT_BUDGET_EXCEEDED', 'canonical sparse allocation exceeds import budget')
    if fmt in ('csr', 'csc'):
        if not {'indices', 'indptr'} <= values.keys():
            _error('INVALID_STRUCTURE', 'missing compressed indices')
        idx, ptr = values['indices'], values['indptr']
        if idx.dtype.kind not in 'iu' or ptr.dtype.kind not in 'iu' or idx.ndim != 1 or ptr.ndim != 1:
            _error('INVALID_INDICES', 'integer vectors required')
        if len(idx) != len(data):
            _error('INVALID_INDICES', 'indices: length must equal data length')
        if len(ptr) != shape[0]+1:
            _error('INVALID_INDICES', 'indptr: length must equal n+1')
        if int(ptr[0]) != 0 or int(ptr[-1]) != len(data):
            _error('INVALID_INDICES', 'indptr[0] or indptr[-1]: invalid pointer endpoint')
        invalid = np.flatnonzero((idx < 0) | (idx >= shape[1]))
        if len(invalid):
            _error('INVALID_INDICES', f'indices[{int(invalid[0])}]: index out of bounds')
        invalid = np.flatnonzero(ptr > len(data))
        if len(invalid):
            _error('INVALID_INDICES', f'indptr[{int(invalid[0])}]: pointer exceeds data length')
        invalid = np.flatnonzero(ptr[1:] < ptr[:-1])
        if len(invalid):
            _error('INVALID_INDICES', f'indptr[{int(invalid[0])+1}]: nonmonotonic pointer')
        cls = sparse.csr_matrix if fmt == 'csr' else sparse.csc_matrix
        a = cls((data, idx, ptr), shape=shape)
    elif fmt == 'coo':
        if not {'row', 'col'} <= values.keys():
            _error('INVALID_INDICES', 'missing COO coordinates')
        row, col = values['row'], values['col']
        if row.dtype.kind not in 'iu' or col.dtype.kind not in 'iu' or row.shape != data.shape or col.shape != data.shape:
            _error('INVALID_INDICES', 'invalid COO coordinates')
        for field, vector in (('row', row), ('col', col)):
            invalid = np.flatnonzero((vector < 0) | (vector >= shape[0]))
            if len(invalid):
                _error('INVALID_INDICES', f'{field}[{int(invalid[0])}]: COO index out of bounds')
        a = sparse.coo_matrix((data, (row, col)), shape=shape)
    else:
        _error('INVALID_FORMAT', 'supported formats are CSR, CSC and COO')
    a = a.tocsr()
    a._emsolver_normalization = {'input_format':fmt, 'input_stored_nnz':int(len(data)),
                                'input_stored_zero_entries':int(np.sum(data == 0))}
    return a


def _scale(a):
    return float(np.max(np.abs(a.data), initial=0.0))


def _divide_sparse(a, divisor):
    # Sparse scalar division multiplies by 1/divisor, which overflows for subnormals.
    scaled = a.copy()
    if np.iscomplexobj(scaled.data):
        scaled.data.real[:] = scaled.data.real/divisor
        scaled.data.imag[:] = scaled.data.imag/divisor
    else:
        scaled.data[:] = scaled.data/divisor
    return scaled


def _norm_inf_scaled(a):
    return float(np.max(np.asarray(abs(a).sum(axis=1)).ravel(), initial=0.0))


def _hermitian_error(a):
    scale = _scale(a)
    if scale == 0:
        return 0.0
    scaled = _divide_sparse(a, scale)
    denominator = _norm_inf_scaled(scaled)
    return _norm_inf_scaled(scaled - scaled.conjugate().T) / denominator


def load_matrix(path, storage='full', hermitian_rtol=1e-12, max_bytes=1073741824):
    """Load safe numeric NumPy/SciPy files and strictly validate sparse structure."""
    path = Path(path)
    if storage not in ('full', 'upper', 'lower'):
        _error('INVALID_STORAGE', 'full, upper or lower required')
    if not math.isfinite(hermitian_rtol) or not 0 <= hermitian_rtol <= 1e-3:
        _error('INVALID_TOLERANCE', 'hermitian_rtol')
    if max_bytes <= 0 or not path.is_file() or path.stat().st_size > max_bytes:
        _error('INPUT_BUDGET_EXCEEDED', 'file missing or over budget')
    try:
        if path.suffix.lower() == '.npz':
            a = _read_npz(path, max_bytes)
        elif path.suffix.lower() == '.npy':
            with path.open('rb') as stream:
                _check_npy_header(stream, path.stat().st_size, max_bytes)
            array = np.load(path, allow_pickle=False, mmap_mode='r')
            _numeric(array, 'array')
            if array.ndim != 2 or array.shape[0] <= 0 or array.shape[0] != array.shape[1]:
                _error('INVALID_SHAPE', 'nonempty square matrix required')
            if array.nbytes > max_bytes:
                _error('INPUT_BUDGET_EXCEEDED', 'dense input exceeds import budget')
            blocks = [sparse.csr_matrix(array[start:start+128]) for start in range(0, array.shape[0], 128)]
            a = sparse.vstack(blocks, format='csr')
            a._emsolver_normalization = {'input_format':'npy_dense', 'input_stored_nnz':int(a.nnz),
                                        'input_stored_entries':int(array.size), 'input_stored_zero_entries':int(array.size-a.nnz)}
        else:
            _error('INVALID_FORMAT', 'only .npy and .npz are accepted')
    except (OSError, TypeError, zipfile.BadZipFile, EOFError, UnicodeError) as exc:
        raise ValueError('INVALID_INPUT: malformed numeric archive') from exc
    except (ValueError, RuntimeError) as exc:
        code = str(exc).split(':', 1)[0]
        if code.isupper() and ' ' not in code:
            raise
        raise ValueError('INVALID_INPUT: malformed or unsafe numeric array') from exc
    normalization = getattr(a, '_emsolver_normalization', {'input_stored_nnz':int(a.nnz)})
    a = a.astype(np.complex128 if np.iscomplexobj(a.data) else np.float64)
    a.sum_duplicates()
    merged_nnz = a.nnz
    a.sort_indices()
    a.eliminate_zeros()
    normalization.update(duplicates_merged=int(normalization['input_stored_nnz']-merged_nnz),
                         zero_entries_removed=int(merged_nnz-a.nnz), canonical_nnz_before_reconstruction=int(a.nnz),
                         declared_storage=storage)
    _numeric(a.data, 'normalized data')
    if not np.isfinite(np.abs(a.data)).all():
        _error('NONFINITE_INPUT', 'magnitude overflow')
    if storage != 'full':
        wrong = sparse.tril(a, -1) if storage == 'upper' else sparse.triu(a, 1)
        if wrong.nnz:
            _error('TRIANGLE_MISMATCH', 'nonzero entries outside declared triangle')
        diagonal = sparse.diags(a.diagonal(), format='csr')
        a = (a + a.conjugate().T - diagonal.conjugate()).tocsr()
        # Keep the original diagonal, including its imaginary part, for validation.
    error = _hermitian_error(a)
    if not math.isfinite(error) or error > hermitian_rtol:
        _error('NOT_HERMITIAN', f'relative infinity-norm error {error:.6g}')
    if a.data.nbytes + a.indices.nbytes + a.indptr.nbytes > max_bytes:
        _error('INPUT_BUDGET_EXCEEDED', 'normalized sparse matrix exceeds budget')
    normalization.update(reconstructed_nnz=int(a.nnz), final_nnz=int(a.nnz), format='csr',
                         indices_sorted=bool(a.has_sorted_indices), duplicates_merged_policy='sum',
                         input_changed_by_symmetrization=False)
    a._emsolver_normalization = normalization
    return a


def inspect_matrix(matrix):
    normalization = getattr(matrix, '_emsolver_normalization', None)
    a = sparse.csr_matrix(matrix)
    coo = a.tocoo(copy=False)
    positions = np.linspace(0, a.nnz-1, min(a.nnz, 1500), dtype=int) if a.nnz else []
    diagonal = a.diagonal().real
    scale = _scale(a)
    row_counts = np.diff(a.indptr)
    counts, edges = np.histogram(row_counts, bins=min(64, int(np.max(row_counts)-np.min(row_counts))+1))
    return _clean({'n': int(a.shape[0]), 'nnz': int(a.nnz), 'dtype': str(a.dtype),
                   'is_diagonal':_is_diagonal(a), 'normalization':normalization,
                   'row_nnz_histogram':{'counts':counts.tolist(), 'bin_edges':edges.tolist(), 'max_bins':64,
                                        'policy':'uniform_width', 'rows_counted':int(a.shape[0])},
                   'hermitian_error': _hermitian_error(a),
                   'norm_inf': _norm_inf_scaled(_divide_sparse(a, scale))*scale if scale else 0.0,
                   'storage_bytes': int(a.data.nbytes+a.indices.nbytes+a.indptr.nbytes),
                   'density': float(a.nnz/(a.shape[0]*a.shape[1])),
                   'preview': [[int(coo.row[i]), int(coo.col[i]), float(abs(coo.data[i]))] for i in positions],
                   'diagonal_min': float(np.min(diagonal)), 'diagonal_max': float(np.max(diagonal)),
                   'row_nnz_min': int(np.min(np.diff(a.indptr))), 'row_nnz_max': int(np.max(np.diff(a.indptr)))})


def generate_sample(path, kind='indefinite', n=500, seed=20261016):
    if isinstance(n, bool) or not isinstance(n, int) or not 4 <= n <= 1000000:
        _error('INVALID_SAMPLE', 'n must be an integer between 4 and 1000000')
    if kind not in ('indefinite', 'clustered', 'repeated', 'near_singular', 'complex'):
        _error('INVALID_SAMPLE', 'unknown sample family')
    rng = np.random.default_rng(seed)
    d = np.linspace(-5., 5., n)
    if kind == 'clustered':
        d = np.concatenate((np.linspace(-5., -1., n//3), rng.uniform(-1e-5, 1e-5, n-2*(n//3)), np.linspace(1., 5., n//3)))
    elif kind == 'repeated':
        d[n//3:2*n//3] = 0.
    elif kind == 'near_singular':
        d[n//2] = 0.
    a = sparse.diags(d, format='csr')
    if kind in ('indefinite', 'complex'):
        off = rng.uniform(0.01, 0.08, n-1)
        if kind == 'complex':
            off = off*np.exp(1j*rng.uniform(-np.pi, np.pi, n-1))
        a = (a+sparse.diags([off, off.conjugate()], [1, -1])).tocsr()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    sparse.save_npz(path, a)
    return {'name':f'{kind}_{n}', 'description':'Seeded synthetic Hermitian sparse matrix; no reference answers are read.',
            'source':'synthetic', 'n':n, 'nnz':int(a.nnz), 'kind':kind, 'seed':int(seed)}


def capabilities():
    from .gpu import probe_metal
    from emsolver.optional_backends import probe_slepc
    slepc=probe_slepc()
    def available(module):
        try:
            return importlib.util.find_spec(module) is not None
        except (ImportError, ModuleNotFoundError):
            return False
    return {'scipy':{'available':True, 'supported':True, 'version':scipy.__version__, 'methods':['arpack','block_shift_invert','jacobi_davidson'],
                     'scalar_types':['float64','complex128'], 'target_certification':'diagonal_tridiagonal_bounded_exact_inertia_and_gapped_disk_counts'},
            'backend_protocol':'prepare/solve/monitor/finalize',
            'block_shift_invert':{'supported':True, 'target_modes':['nearest_k'], 'linear_method':'exact_sparse_lu',
                                 'residual_monitoring':'original_problem', 'locking':'revalidated_soft_lock',
                                 'checkpoint':'subspace_or_exact_fixed_shift_block', 'exact_resume':True,
                                 'exact_resume_profile':'fixed shift, no fallback, periodic full state, same source/config/input/environment',
                                 'adaptive_inner_precision':False, 'implementation':'emsolver/numerics/block.py'},
            'jacobi_davidson':{'supported':True,'target_modes':['nearest_k'],'transform':'precond',
                              'linear_method':'gmres','preconditioners':['none','jacobi','ilu_budgeted_v1','ilu_equilibrated_compensated_v2'],'adaptive_inner_accuracy':True,
                              'preconditioner_profiles':{'ilu_budgeted_v1':{'device':'cpu','precision':'float64_or_complex128',
                                                                          'mixed_precision':False,'factorization_reuse':True},
                                                         'ilu_equilibrated_compensated_v2':{'device':'cpu','precision':'float64_or_complex128',
                                                             'mixed_precision':False,'factorization_reuse':True,'constrained_projection':True}},
                              'mixed_precision':True,'bounded_restarts':True,'checkpoint':'periodic_subspace_warm_start_new_task',
                              'exact_resume':False,'implementation':'emsolver/numerics/correction.py'},
            'reordering':{'methods':['none','reverse_cuthill_mckee'], 'original_vector_order_restored':True},
            'fallback_profiles':{'block_to_arpack_v1':{'supported':True, 'from':'block_shift_invert',
                                                      'to':'arpack', 'max_executions':1,
                                                      'preserves_target_and_validation':True}},
            'adaptive_shift':{'default_policy':'observed_ritz_gap_v2','policies':['observed_ritz_gap_v2','feedback_bounded_v1'],
                              'feedback_inputs':['refined_Ritz_gaps','linear_residual','failure_codes'],
                              'target_immutable':True,'applicable_methods':['arpack','block_shift_invert']},
            'slepc':slepc,
            'mpi':{'available':slepc['available'], 'supported':slepc['supported'],
                   'backend':'slepc', 'tested_ranks':[1,2] if slepc['supported'] else [],
                   'reason':None if slepc['supported'] else 'Validated distributed image is not configured'},
            'gpu':probe_metal(),
            'numpy_version':np.__version__, 'python_version':platform.python_version(),
            'platform':platform.system(), 'machine':platform.machine(), 'cpu_count':os.cpu_count(),
            'cpu_model':platform.processor() or platform.machine(), 'physical_cpu_count':psutil.cpu_count(logical=False),
            'memory_total_bytes':psutil.virtual_memory().total,
            'threads':_threadpools(), 'limits':{'max_modes':_MAX_MODES, 'max_subspace':_MAX_SUBSPACE},
            'limitations':['General sparse exact certification is limited by explicit dimension/fill/fraction/time budgets', 'No exact ARPACK checkpoint continuation',
                           'Direct sparse LU fill-in is not predictable; worker must enforce process-tree budgets',
                           'ARPACK outer iteration count is unavailable; solve and factorization counts are observed']}


def _config(config):
    from emsolver.contracts import resolve_config
    return resolve_config(config)


def _is_banded_csr(a, half_bandwidth):
    # Bound temporary index arrays independently of nnz. Empty CSR rows need
    # right-side insertion so each stored entry maps to its actual row.
    for start in range(0, a.nnz, 65536):
        end = min(start+65536, a.nnz)
        positions = np.arange(start, end, dtype=np.int64)
        rows = np.searchsorted(a.indptr, positions, side='right')-1
        columns = a.indices[start:end]
        if np.any(columns < rows-half_bandwidth) or np.any(columns > rows+half_bandwidth):
            return False
    return True


def _is_diagonal(a):
    return _is_banded_csr(a, 0)


def _interval_mask(values, interval):
    # Values are never snapped into/out of the interval by the endpoint tolerance.
    return (values >= interval['lower']) & (values < interval['upper'])


def _cluster_tol(values):
    return 64*np.finfo(float).eps*max(float(np.max(np.abs(values), initial=0)), np.finfo(float).tiny)


def _diagonal_selection(diagonal, target, order=None):
    if target['mode'] == 'interval_all':
        indices = np.flatnonzero(_interval_mask(diagonal, target['interval']))
        indices = indices[np.argsort(diagonal[indices], kind='stable')]
        total = len(indices)
        return indices[:target['max_modes']], {'source':'structural_diagonal_full_spectrum', 'count':total,
                                             'boundary_cluster_cut':False, 'certified':total <= target['max_modes']}
    if order is None:
        order = _target_order(diagonal, target['sigma'])
    k = target['k']
    count = k
    cut = False
    if k < len(order):
        # Equal-distance different eigenvalues have a deterministic value tie-break.
        tol = _cluster_tol(diagonal)
        cut = bool(abs(diagonal[order[k]]-diagonal[order[k-1]]) <= tol)
        if cut and target['cluster_policy'] == 'cluster_complete':
            while count < len(order) and abs(diagonal[order[count]]-diagonal[order[k-1]]) <= tol:
                count += 1
            cut = False
    return order[:count], {'source':'structural_diagonal_full_spectrum', 'count':count,
                          'boundary_cluster_cut':cut, 'certified':not cut}


def _normalise_vectors(x):
    x = np.array(x, dtype=np.complex128 if np.iscomplexobj(x) else np.float64, copy=True)
    if x.ndim != 2 or not np.isfinite(x).all():
        _error('INVALID_EIGENVECTORS', 'finite two-dimensional array required')
    for i in range(x.shape[1]):
        scale = float(np.max(np.abs(x[:, i]), initial=0))
        if not scale or not math.isfinite(scale):
            _error('ZERO_EIGENVECTOR', 'each mode must be nonzero')
        if np.iscomplexobj(x):
            x[:, i].real[:] = x[:, i].real/scale
            x[:, i].imag[:] = x[:, i].imag/scale
        else:
            x[:, i] /= scale
        norm = float(np.linalg.norm(x[:, i]))
        x[:, i] /= norm
    return x


def _stable_norm2(vector):
    maximum = float(np.max(np.abs(vector), initial=0))
    if maximum == 0:
        return 0.
    if not math.isfinite(maximum):
        return float('inf')
    if np.iscomplexobj(vector):
        scaled = vector.real/maximum+1j*(vector.imag/maximum)
    else:
        scaled = vector/maximum
    return float(np.linalg.norm(scaled))*maximum


def _validate(a, values, vectors, config):
    values = np.asarray(values)
    if values.ndim != 1 or not np.isfinite(values).all() or vectors.shape != (a.shape[0], len(values)):
        _error('INVALID_EIGENPAIRS', 'shape or finite-value failure')
    if np.iscomplexobj(values) and np.any(values.imag != 0):
        _error('COMPLEX_EIGENVALUES', 'saved Hermitian eigenvalues must be real')
    x = _normalise_vectors(vectors)
    residuals, absolute = [], []
    scale_a = _scale(a)
    for value, vector in zip(values, x.T):
        scale = max(scale_a, abs(float(value)))
        if scale == 0:
            residuals.append(0.)
            absolute.append(0.)
            continue
        sa = _divide_sparse(a, scale)
        lv = float(value)/scale
        r = sa@vector-lv*vector
        numerator = float(np.max(np.abs(r), initial=0))
        denominator = (_norm_inf_scaled(sa)+abs(lv))*float(np.max(np.abs(vector), initial=0))
        residuals.append(numerator/denominator if denominator else (0. if numerator == 0 else float('inf')))
        # Compute this independently in original units: scaling A first can
        # erase tiny entries when its dynamic range exceeds float64's range.
        with np.errstate(over='ignore', invalid='ignore'):
            original_residual = a@vector-float(value)*vector
        absolute.append(_stable_norm2(original_residual))
    if x.shape[1]:
        gram = x.conjugate().T@x
        gram -= np.eye(x.shape[1])
        orthogonality = float(np.linalg.norm(gram, ord=2))
    else:
        orthogonality = 0.
    threshold = config['validation']['residual_scaled_max']
    absolute_max = config['validation'].get('absolute_residual_max')
    # An empty nearest-k candidate is a failed solve; an independently counted
    # empty interval can still be a valid answer to an interval-all task.
    has_answer = bool(len(values)) or config['target']['mode'] == 'interval_all'
    passed = bool(has_answer and all(math.isfinite(r) and r <= threshold for r in residuals) and
                  orthogonality <= config['validation']['orthogonality_max'] and
                  (absolute_max is None or all(r <= absolute_max for r in absolute)))
    return {'numerical_status':'PASSED' if passed else 'FAILED', 'residual_definition':'scaled_inf_v1',
            'residuals':residuals, 'absolute_residuals_2':absolute, 'orthogonality_error':orthogonality,
            'n_validated':len(values), 'normalization':'unit_2_norm'}, x


def _certify(a, values, vectors, config, *, check=None):
    if check is not None: check()
    target = config['target']
    result = {'target_certification':'UNVERIFIED', 'certification_source':None, 'boundary_cluster_cut':False,
              'expected_count':None, 'missing_count':None, 'endpoint_ambiguous':False}
    if not _is_diagonal(a):
        if not _is_banded_csr(a, 1):
            from .certification import certify_sparse
            exact = certify_sparse(a,values,vectors,config,check=check)
            if exact.get('certification_limit') in {'dimension_budget', 'input_nonzero_budget'}:
                from .independent_counts import certify_gapped
                started = time.monotonic()
                gapped = certify_gapped(a,values,vectors,config,check=check)
                uniform_seconds = time.monotonic()-started
                if gapped.get('certification_limit') in {
                        'too_many_disks_cover_target', 'no_separated_nearest_disk_union',
                        'interval_boundary_disk_overlap', 'disk_crosses_count_point'}:
                    from .independent_counts import certify_weighted
                    weighted = certify_weighted(a,values,vectors,config,check=check)
                    weighted['preceding_certification'] = {
                        'source': gapped['certification_source'], 'limit': gapped['certification_limit'],
                        'seconds': uniform_seconds}
                    gapped = weighted
                gapped['previous_certification_limit'] = exact['certification_limit']
                return gapped
            return exact
        return _certify_tridiagonal(a, values, vectors, config, result)
    diagonal = a.diagonal().real
    # The structural reference uses exact rational distances independently of
    # candidate sorting, so a ranking bug cannot validate itself.
    reference_order = _exact_target_order(diagonal, target['sigma']) if target['mode'] == 'nearest_k' else None
    selected, evidence = _diagonal_selection(diagonal, target, order=reference_order)
    expected = diagonal[selected]
    result.update(certification_source=evidence['source'], boundary_cluster_cut=evidence['boundary_cluster_cut'],
                  expected_count=evidence['count'])
    if target['mode'] == 'interval_all':
        interval = target['interval']
        atol = interval.get('boundary_atol', 1e-10)
        # Exact structural diagonal entries resolve endpoint membership analytically.
        result['endpoint_ambiguous'] = False
        result['boundary_atol'] = atol
    match = np.array_equal(values, expected)
    # Verify containment in each exact diagonal eigenspace, not merely value proximity.
    if match:
        for value, vector in zip(values, vectors.T):
            mismatch = diagonal != value
            if np.linalg.norm(vector[mismatch]) > config['validation']['orthogonality_max']:
                match = False
                break
    if match and evidence['certified']:
        result['target_certification'] = 'CERTIFIED'
        result['missing_count'] = 0
    else:
        result['missing_count'] = max(0, evidence['count']-len(values))
    return result


def _sturm_count(diagonal, off_squared, point):
    """Count eigenvalues <= point via guarded real tridiagonal LDL recurrence.

    This is only a resource preflight. LAPACK independently recomputes the
    selected eigenvalues and their multiplicities before any certification.
    """
    pivot_min = np.finfo(float).tiny
    pivot = float(diagonal[0]-point)
    if abs(pivot) < pivot_min:
        pivot = -pivot_min
    count = int(pivot <= 0)
    for index in range(1, len(diagonal)):
        pivot = float(diagonal[index]-point)-float(off_squared[index-1])/pivot
        if abs(pivot) < pivot_min:
            pivot = -pivot_min
        count += int(pivot <= 0)
    return count


def _target_order(values, sigma):
    values = np.asarray(values)
    if not len(values):
        return np.empty(0, dtype=np.intp)
    # Beyond the spectrum, exact distance order is monotonic in the value;
    # subtraction would erase small differences from a very remote target.
    if sigma >= np.max(values):
        return np.argsort(-values, kind='stable')
    if sigma <= np.min(values):
        return np.argsort(values, kind='stable')
    with np.errstate(over='ignore'):
        distances = np.abs(values-sigma)
    order = np.lexsort((values, distances))
    rounded = distances[order]
    starts = np.flatnonzero(np.r_[True, rounded[1:] != rounded[:-1]])
    ends = np.r_[starts[1:], len(order)]
    origin = Fraction.from_float(float(sigma))
    # IEEE subtraction rounds exact distances monotonically, so only equal
    # rounded distances (including overflow to infinity) need exact comparison.
    for start, end in zip(starts[ends-starts > 1], ends[ends-starts > 1]):
        order[start:end] = sorted(order[start:end], key=lambda i:(
            abs(Fraction.from_float(float(values[i]))-origin), float(values[i]), int(i)))
    return order


def _exact_target_order(values, sigma):
    """Independent reference ordering over exact binary64 rational values."""
    origin = Fraction.from_float(float(sigma))
    return np.asarray(sorted(range(len(values)), key=lambda i:(
        abs(Fraction.from_float(float(values[i]))-origin), float(values[i]), i)), dtype=np.intp)


def _certify_tridiagonal(a, values, vectors, config, result):
    n = a.shape[0]
    # Both CPU cost and auxiliary memory are bounded; larger inputs remain candidates.
    if n > 200000:
        return result
    if not _is_banded_csr(a, 1):
        return result
    scale = _scale(a)
    if not scale:
        return result
    scaled = _divide_sparse(a, scale)
    if np.any((a.data != 0) & (scaled.data == 0)):
        result['certification_limit'] = 'scaled_reference_underflow'
        return result
    diagonal = scaled.diagonal().real
    upper, lower = scaled.diagonal(1), scaled.diagonal(-1)
    if not np.array_equal(upper, lower.conjugate()) or np.any(scaled.diagonal().imag != 0):
        return result
    # A diagonal unitary gauge makes the Hermitian off-diagonal real nonnegative.
    off = np.abs(upper)
    squared = off*off
    target = config['target']
    precision = np.finfo(float).eps*max(1., _norm_inf_scaled(scaled))*64
    margin = max(precision*16, 1e-11)
    if target['mode'] == 'nearest_k':
        if not len(values):
            return result
        centre = target['sigma']/scale
        radius = float(np.max(np.abs(values/scale-centre)))
        low, high = centre-radius-margin, centre+radius+margin
    else:
        low = target['interval']['lower']/scale-margin
        high = target['interval']['upper']/scale+margin
    if not math.isfinite(low) or not math.isfinite(high) or low >= high:
        return result
    left = _sturm_count(diagonal, squared, low)
    right = _sturm_count(diagonal, squared, high)
    if _sturm_count(diagonal, squared, low-margin) != left or _sturm_count(diagonal, squared, high+margin) != right:
        result['certification_limit'] = 'spectral_window_boundary_unstable'
        return result
    count = right-left
    if not 0 <= count <= _MAX_MODES:
        result['certification_limit'] = 'spectral_window_exceeds_mode_budget'
        return result
    try:
        expected = eigvalsh_tridiagonal(diagonal, off, select='v', select_range=(low, high),
                                        lapack_driver='stebz', tol=precision/8)
    except (ValueError, np.linalg.LinAlgError):
        result['certification_limit'] = 'lapack_spectral_window_failed'
        return result
    if len(expected) != count:
        result['certification_limit'] = 'independent_count_disagreement'
        return result
    result['certification_source'] = 'tridiagonal_sturm_lapack_stebz'
    result['certification_window_count'] = count
    result['certification_absolute_tolerance'] = precision*scale
    full_window = expected.copy()
    if target['mode'] == 'interval_all':
        interval = target['interval']
        endpoint_tolerance = max(interval['boundary_atol']/scale, precision*8)
        ambiguous = bool(np.any(np.abs(expected-interval['lower']/scale) <= endpoint_tolerance) or
                         np.any(np.abs(expected-interval['upper']/scale) <= endpoint_tolerance))
        result['endpoint_ambiguous'] = ambiguous
        expected = expected[(expected >= interval['lower']/scale) & (expected < interval['upper']/scale)]
        result['expected_count'] = len(expected)
        if ambiguous or len(expected) > target['max_modes']:
            return result
    else:
        unscaled_expected = expected*scale
        if not np.isfinite(unscaled_expected).all():
            return result
        expected = expected[_exact_target_order(unscaled_expected, target['sigma'])]
        k = target['k']
        if len(expected) < k:
            result['expected_count'] = k
            return result
        if len(expected) > k:
            value_gap = abs(expected[k]-expected[k-1])
            distance_gap = abs(abs(expected[k]-centre)-abs(expected[k-1]-centre))
            if value_gap > precision*64 and distance_gap <= precision*32:
                result['certification_limit'] = 'uncertain_nearest_distance_boundary'
                return result
        end = k
        # Multiplicity is retained by LAPACK and independently checked against vectors.
        cut = len(expected) > k and abs(expected[k]-expected[k-1]) <= precision*8
        if cut and target['cluster_policy'] == 'cluster_complete':
            while end < len(expected) and abs(expected[end]-expected[k-1]) <= precision*8:
                end += 1
            cut = False
        result['boundary_cluster_cut'] = bool(cut)
        expected = expected[:end]
        result['expected_count'] = len(expected)
        if cut:
            return result
    match_tolerances, spectral_separations = [], []
    for root in expected:
        separations = np.abs(full_window-root)
        distinct_separations = separations[separations > 0]
        gap = float(np.min(distinct_separations, initial=float('inf')))
        base_tolerance = precision*16
        # A candidate tolerance must never encompass a different reference
        # mode. If finite-precision intervals overlap, withhold certification
        # even when the candidate happens to be the correct near neighbor.
        if gap <= 4*base_tolerance:
            result['certification_limit'] = 'unresolved_neighboring_modes'
            return result
        match_tolerances.append(min(base_tolerance, gap/4))
        spectral_separations.append(min(gap, root-low, high-root))
    matches = len(values) == len(expected) and np.all(np.abs(values/scale-expected) <= match_tolerances)
    if matches:
        vectors = _normalise_vectors(vectors)
        angle_bounds = []
        for value, root, separation, vector in zip(values, expected, spectral_separations, vectors.T):
            # All other eigenspaces are separated either by a known reference
            # root or by a counted spectral-window boundary. Reference and
            # candidate uncertainty must be subtracted from that lower bound.
            separation -= abs(value/scale-root)+precision*2
            if separation <= 0:
                result['certification_limit'] = 'unresolved_mode_subspace_separation'
                return result
            residual_norm = _stable_norm2(scaled@vector-(value/scale)*vector)
            angle_bounds.append(residual_norm/separation)
        result['mode_subspace_angle_bound_max'] = max(angle_bounds, default=0.)
        result['mode_subspace_angle_threshold'] = 1e-4
        if any(not math.isfinite(bound) or bound > 1e-4 for bound in angle_bounds):
            result['certification_limit'] = 'mode_subspace_not_verified'
            return result
        result['target_certification'] = 'CERTIFIED'
        result['missing_count'] = 0
    else:
        result['missing_count'] = max(0, len(expected)-len(values))
    return result


class _Runtime:
    def __init__(self, config, callback, cancel_check):
        self.start = time.monotonic()
        self.config = config
        self.callback = callback
        self.cancel = cancel_check
        self.process = psutil.Process()
        self.peak = 0
        self.events = deque()
        self.event_sizes = deque()
        self.retained_event_bytes = 0
        self.event_history_byte_limit = 8*1024**2
        self.events_emitted = 0
        self.events_suppressed = 0
        self.memory_sample_interval_seconds = 0.05
        self.next_memory_sample = 0.
        self.runtime_check_seconds = 0.
        self.event_emission_seconds = 0.

    def check(self):
        started = time.monotonic()
        if self.cancel and self.cancel():
            raise InterruptedError('CANCELLED: cancellation requested')
        if started-self.start > self.config['resources']['walltime_seconds']:
            raise RuntimeError('WALLTIME_EXCEEDED: time budget exhausted')
        if started >= self.next_memory_sample:
            rss = self.process.memory_info().rss
            for child in self.process.children(recursive=True):
                try:
                    rss += child.memory_info().rss
                except psutil.Error:
                    pass
            self.peak = max(rss, self.peak)
            self.next_memory_sample = started+self.memory_sample_interval_seconds
            from emsolver.resource_budgets import memory_budgets
            if rss > memory_budgets(self.config)['host_bytes']:
                raise RuntimeError('RESOURCE_EXHAUSTED: process-tree memory budget')
        self.runtime_check_seconds += time.monotonic()-started

    def emit(self, stage, event, **fields):
        started = time.monotonic()
        self.check()
        secondary = event in {'linear_progress', 'block_residual_checked', 'modes_soft_locked', 'block_restarted',
                              'inner_tolerance_updated', 'inner_progress'}
        if secondary and not self.config['observability']['enabled']:
            self.events_suppressed += 1
            self.event_emission_seconds += time.monotonic()-started
            return
        item = _clean({'stage':stage, 'event':event, 'elapsed_seconds':time.monotonic()-self.start,
                       'peak_memory_bytes':self.peak, **fields})
        item_bytes = len(json.dumps(item,ensure_ascii=False,allow_nan=False).encode('utf-8'))+1
        self.events.append(item)
        self.event_sizes.append(item_bytes)
        self.retained_event_bytes += item_bytes
        while len(self.events)>4096 or self.retained_event_bytes>self.event_history_byte_limit:
            self.events.popleft()
            self.retained_event_bytes -= self.event_sizes.popleft()
        self.events_emitted += 1
        if self.callback:
            self.callback(item)
        self.event_emission_seconds += time.monotonic()-started


def _orthogonalize_clusters(a, values, vectors):
    if not len(values):
        return values, vectors
    order = np.argsort(values.real, kind='stable')
    values, vectors = values[order], vectors[:, order]
    threshold = max(_cluster_tol(values.real), 1e-10*float(np.max(np.abs(values), initial=0)))
    start = 0
    while start < len(values):
        end = start+1
        while end < len(values) and abs(values[end]-values[start]) <= threshold:
            end += 1
        block = vectors[:, start:end]
        q, r = np.linalg.qr(block, mode='reduced')
        if np.min(np.abs(np.diag(r)), initial=1.) < 1e-10:
            _error('DEPENDENT_MODES', 'candidate cluster has deficient rank')
        projection = q.conjugate().T@(a@q)
        ev, rotation = np.linalg.eigh(projection)
        values[start:end] = ev
        vectors[:, start:end] = q@rotation
        start = end
    return values, vectors


def _solve_arpack(a, config, runtime):
    from .shift_control import create_shift_policy
    target, solver = config['target'], config['solver']
    n = a.shape[0]
    requested = target['k'] if target['mode'] == 'nearest_k' else target['max_modes']
    complex_path = np.iscomplexobj(a.data)
    max_candidates = n-2 if complex_path else n-1
    if target['mode'] == 'nearest_k' and requested > max_candidates:
        _error('BACKEND_K_LIMIT', 'ARPACK cannot compute requested internal block without dense fallback')
    subspace = min(n, solver['subspace_max'])
    if target['mode'] == 'interval_all':
        requested = min(requested, max_candidates, subspace-(2 if complex_path else 1))
        if requested < 1:
            _error('BACKEND_K_LIMIT', 'matrix too small for sparse ARPACK interval exploration')
    internal = min(max_candidates, requested+max(2, requested//3), subspace-(2 if complex_path else 1))
    if internal < requested or subspace <= internal+(1 if complex_path else 0):
        _error('SUBSPACE_LIMIT', 'increase subspace_max above candidate block size')
    budget = config['resources']['memory_limit_gib']*1024**3
    estimate = 3*(a.data.nbytes+a.indices.nbytes+a.indptr.nbytes)+n*subspace*(16 if complex_path else 8)*5
    if estimate > 0.7*budget:
        raise RuntimeError('RESOURCE_EXHAUSTED: estimated sparse/workspace memory exceeds safe budget')
    sigma = target['sigma'] if target['mode'] == 'nearest_k' else (target['interval']['lower']/2+target['interval']['upper']/2)
    matrix_scale = _scale(a)
    if matrix_scale == 0:
        matrix_scale = 1.
    scaled = _divide_sparse(a, matrix_scale)
    base = sigma/matrix_scale
    if not math.isfinite(base):
        _error('NUMERICAL_SCALE_LIMIT', 'target/matrix scale ratio exceeds float64 range')
    delta = max(1., abs(base), _norm_inf_scaled(scaled))*np.sqrt(np.finfo(float).eps)
    adaptive = solver['adaptive_shift']
    count = adaptive['max_attempts'] if adaptive['enabled'] else 1
    shift_policy=create_shift_policy(base,delta,count,adaptive['policy'])
    attempts, best = [], None
    counters = {'factorizations':0, 'linear_solves':0, 'matrix_vector_products':None, 'shifted_residual_matvecs':0, 'outer_iterations':None,
                'linear_iterations':None, 'max_linear_residual':0.}
    rng = np.random.default_rng(solver['random_seed'])
    for index in range(count):
        shift=shift_policy.next_shift()
        decision=dict(shift_policy.last_decision)
        work_sigma=shift*matrix_scale
        if not math.isfinite(work_sigma):
            attempts.append({'attempt':index+1,'sigma_target':sigma,'sigma_work':None,'status':'FAILED',
                             'error_code':'NUMERICAL_SCALE_LIMIT','shift_decision':decision,'seconds':0.})
            shift_policy.observe(error_code='NUMERICAL_SCALE_LIMIT')
            runtime.emit('PREPARING','shift_failed',attempt=index+1,error_code='NUMERICAL_SCALE_LIMIT',metrics={'shift_decision':decision})
            continue
        runtime.emit('PREPARING', 'shift_attempt', attempt=index+1, sigma_target=sigma, sigma_work=work_sigma,
                     metrics={'shift_decision':decision})
        attempt = {'attempt':index+1, 'sigma_target':sigma, 'sigma_work':work_sigma, 'status':'STARTED','shift_decision':decision}
        t0 = time.monotonic()
        shifted = (scaled-shift*sparse.eye(n, format='csr')).tocsc()
        try:
            counters['factorizations'] += 1
            lu = sla.splu(shifted)
            runtime.check()
            def solve(vector):
                runtime.check()
                answer = lu.solve(vector)
                counters['linear_solves'] += 1
                counters['shifted_residual_matvecs'] += 1
                r = shifted@answer-vector
                denominator = _norm_inf_scaled(shifted)*float(np.max(np.abs(answer), initial=0))+float(np.max(np.abs(vector), initial=0))
                relative = float(np.max(np.abs(r), initial=0))/denominator if denominator else 0.
                counters['max_linear_residual'] = max(counters['max_linear_residual'], relative)
                if not np.isfinite(answer).all() or not math.isfinite(relative) or relative > solver['linear']['rtol_initial']:
                    raise RuntimeError('LINEAR_RESIDUAL_FAILED: inaccurate sparse LU solve')
                if counters['linear_solves'] % 20 == 0:
                    runtime.emit('SOLVING', 'linear_progress', metrics=dict(counters), **counters)
                return answer
            inverse = sla.LinearOperator(a.shape, matvec=solve, dtype=scaled.dtype)
            initial = rng.standard_normal(n)
            if complex_path:
                initial = initial+1j*rng.standard_normal(n)
            kwargs = dict(k=internal, sigma=shift, which='LM', OPinv=inverse, ncv=subspace,
                          maxiter=solver['max_outer_iterations'], tol=min(config['validation']['residual_scaled_max']*0.1, 1e-11), v0=initial)
            runtime.emit('SOLVING', 'arpack_started', internal_k=internal, subspace=subspace)
            try:
                if complex_path:
                    values, vectors = sla.eigs(scaled, **kwargs)
                else:
                    values, vectors = sla.eigsh(scaled, **kwargs)
                attempt['status'] = 'CONVERGED'
            except sla.ArpackNoConvergence as exc:
                values, vectors = exc.eigenvalues, exc.eigenvectors
                attempt['status'] = 'PARTIAL_CONVERGENCE'
                if values is None or vectors is None or len(values) == 0:
                    raise RuntimeError('ARPACK_NO_CONVERGENCE: no usable candidates') from exc
            values = values*matrix_scale
            # Inspect imaginary components before conversion and before Rayleigh refinement.
            imag_limit = max(_cluster_tol(values.real), config['validation']['residual_scaled_max']*max(_scale(a), abs(sigma)))
            real_mask = np.abs(values.imag) <= imag_limit
            attempt['imaginary_rejected'] = int(np.sum(~real_mask))
            values, vectors = values.real[real_mask].copy(), vectors[:, real_mask].copy()
            values, vectors = _orthogonalize_clusters(a, values, vectors)
            shift_policy.observe(values/matrix_scale,linear_residual=counters['max_linear_residual'])
            report, vectors = _validate(a, values, vectors, config)
            valid = np.array(report['residuals']) <= config['validation']['residual_scaled_max']
            if config['validation'].get('absolute_residual_max') is not None:
                valid &= np.array(report['absolute_residuals_2']) <= config['validation']['absolute_residual_max']
            values, vectors = values[valid], vectors[:, valid]
            if target['mode'] == 'interval_all':
                mask = _interval_mask(values, target['interval'])
                values, vectors = values[mask], vectors[:, mask]
                order = np.argsort(values, kind='stable')[:requested]
            else:
                order = _target_order(values, sigma)
                limit = requested
                if target['cluster_policy'] == 'cluster_complete' and len(order) > requested:
                    while limit < len(order) and abs(values[order[limit]]-values[order[requested-1]]) <= _cluster_tol(values):
                        limit += 1
                order = order[:limit]
            values, vectors = values[order], vectors[:, order]
            attempt['n_accepted'] = len(values)
            if best is None or len(values) > len(best[0]):
                best = values, vectors
            runtime.emit('VALIDATING', 'attempt_validated', n_accepted=len(values), residual_max=max(report['residuals'], default=None))
            attempt['seconds'] = time.monotonic()-t0
            attempts.append(attempt)
            if len(values) >= requested:
                break
            shift_policy.observe(error_code='INSUFFICIENT_VALIDATED_CANDIDATES',linear_residual=counters['max_linear_residual'])
        except InterruptedError:
            raise
        except RuntimeError as exc:
            if str(exc).startswith(('RESOURCE_EXHAUSTED', 'WALLTIME_EXCEEDED')):
                raise
            attempt.update(status='FAILED', error_code=_exception_code(exc), seconds=time.monotonic()-t0)
            shift_policy.observe(error_code=attempt['error_code'],linear_residual=counters['max_linear_residual'])
            attempts.append(attempt)
            runtime.emit('SOLVING', 'shift_failed', attempt=index+1, error_code=attempt['error_code'])
        except (sla.ArpackError, np.linalg.LinAlgError) as exc:
            attempt.update(status='FAILED', error_code=type(exc).__name__, seconds=time.monotonic()-t0)
            shift_policy.observe(error_code=attempt['error_code'],linear_residual=counters['max_linear_residual'])
            attempts.append(attempt)
            runtime.emit('SOLVING', 'shift_failed', attempt=index+1, error_code=attempt['error_code'])
        finally:
            if 'lu' in locals():
                del lu
    if best is None:
        best = np.empty(0), np.empty((n, 0), dtype=a.dtype)
    return *best, attempts, counters


def _check_config_support(config):
    if config['problem']['type'] != 'standard_hermitian' or config['problem'].get('matrix_b_id'):
        _error('UNSUPPORTED_PROBLEM', 'generalized Hermitian mass matrix support is not validated')
    method = config['solver']['method']
    if not config['output']['eigenvectors']:
        _error('UNSUPPORTED_OUTPUT', 'independently verifiable runs require eigenvector artifacts')
    if config['solver']['subspace_max'] > _MAX_SUBSPACE:
        _error('SUBSPACE_LIMIT', f'subspace_max may not exceed {_MAX_SUBSPACE}')
    if config['solver']['backend']=='slepc':
        from emsolver.optional_backends import driver_config,probe_slepc
        try:driver_config(config)
        except ValueError:_error('BACKEND_UNAVAILABLE','Requested SLEPc combination is not supported')
        if (config['solver']['block']['checkpoint_interval'] or
                config['solver']['block']['checkpoint_kind']!='subspace' or config['problem'].get('matrix_b_id')):
            _error('BACKEND_UNAVAILABLE','SLEPc checkpoint/generalized combination is not supported')
        if not probe_slepc()['supported']:_error('BACKEND_UNAVAILABLE','Validated SLEPc image is not configured')
        return
    if config['solver']['backend'] != 'scipy' or method not in ('arpack', 'block_shift_invert', 'auto','jacobi_davidson'):
        _error('BACKEND_UNAVAILABLE', 'only the validated SciPy CPU adapters are supported')
    if method in ('block_shift_invert','jacobi_davidson') and config['target']['mode'] != 'nearest_k':
        _error('UNSUPPORTED_TARGET', 'block and correction adapters currently support nearest_k only')
    if method == 'jacobi_davidson' and config['solver']['linear']['preconditioner'] in ('ilu_budgeted_v1','ilu_equilibrated_compensated_v2'):
        if config['resources']['device'] != 'cpu':
            _error('UNSUPPORTED_PRECONDITIONER','ILU profiles are validated only for CPU correction equations')
        if config['solver']['linear']['precision'] != 'float64':
            _error('UNSUPPORTED_PRECISION','ILU profiles require float64 or complex128 correction arithmetic')
    if config['resources']['device'] == 'gpu':
        from .gpu import validate_gpu_profile
        try: validate_gpu_profile(config)
        except ValueError: _error('BACKEND_UNAVAILABLE', 'Requested GPU profile is not runnable')
    elif config['resources']['mpi_ranks'] != 1:
        _error('BACKEND_UNAVAILABLE', 'MPI requires a validated distributed backend')
    profiles = config['solver']['fallback_profiles']
    if profiles and (profiles != ['block_to_arpack_v1'] or config['solver']['method'] != 'block_shift_invert'):
        _error('BACKEND_UNAVAILABLE', 'only one block_to_arpack_v1 fallback from block_shift_invert is validated')
    linear = config['solver']['linear']
    if method == 'jacobi_davidson':
        if config['solver']['transform'] != 'precond':
            _error('UNSUPPORTED_TRANSFORM', 'Jacobi-Davidson requires the projected correction precond transform')
        if linear['method'] != 'gmres':
            _error('UNSUPPORTED_LINEAR_METHOD','Jacobi-Davidson requires GMRES correction solves')
        if linear['preconditioner'] not in ('none','jacobi','ilu_budgeted_v1','ilu_equilibrated_compensated_v2'):
            _error('UNSUPPORTED_PRECONDITIONER','Projected GMRES supports none, absolute-diagonal Jacobi, or declared CPU ILU profiles')
    else:
        if linear['precision'] != 'float64':
            _error('UNSUPPORTED_PRECISION','Mixed precision is only validated for projected correction equations')
        if linear['adaptive_accuracy']['enabled']:
            _error('UNSUPPORTED_INNER_ACCURACY','Exact inverse adapters cannot loosen inner accuracy adaptively')
        if config['solver']['transform'] != 'shift_invert':
            _error('UNSUPPORTED_TRANSFORM', 'the exact inverse adapters require shift_invert')
        if linear['method'] not in ('sparse_lu', 'backend_validated'):
            _error('UNSUPPORTED_LINEAR_METHOD', 'only sparse LU is implemented for exact inverse adapters')
        if linear['preconditioner'] not in ('auto_budgeted', 'none'):
            _error('UNSUPPORTED_PRECONDITIONER', 'only exact sparse LU is implemented for exact inverse adapters')
    if config['solver']['block']['checkpoint_kind']=='exact_block':
        from .exact_checkpoint import validate_profile
        validate_profile(config)
    if config['solver']['block']['checkpoint_interval'] and method not in ('block_shift_invert','jacobi_davidson'):
        _error('UNSUPPORTED_CHECKPOINT','Periodic subspace snapshots require a project-owned block or correction adapter')
    if not config['output']['eigenvectors']:
        _error('UNSUPPORTED_OUTPUT', 'independently verifiable runs require eigenvector artifacts')
    if config['solver']['subspace_max'] > _MAX_SUBSPACE:
        _error('SUBSPACE_LIMIT', f'subspace_max may not exceed {_MAX_SUBSPACE}')


def run_solver(input_path, config, output_dir, event_callback=None, cancel_check=None, *, initial_subspace_path=None,resume_checkpoint_path=None):
    from .backend import create_backend
    config = _config(config)
    if initial_subspace_path is not None and resume_checkpoint_path is not None:
        _error('INCOMPATIBLE_CHECKPOINT_REQUEST','warm start and exact resume are exclusive')
    _check_config_support(config)
    runtime = _Runtime(config, event_callback, cancel_check)
    runtime.output_dir=Path(output_dir)
    runtime.emit('VALIDATING_INPUT', 'input_validation_started')
    output_dir = Path(output_dir)
    if (output_dir/'manifest.json').exists():
        _error('OUTPUT_EXISTS', 'refuse to overwrite a published run')
    if output_dir.exists() and any(output_dir.iterdir()):
        _error('OUTPUT_EXISTS', 'output directory must initially be empty')
    from emsolver.resource_budgets import memory_budgets
    memory = memory_budgets(config)['host_bytes']
    t0 = time.monotonic()
    a = load_matrix(input_path, storage=config['problem']['storage'], hermitian_rtol=config['problem']['hermitian_rtol'],
                    max_bytes=int(min(1024**3, memory*0.3)))
    if config['problem']['scalar'] == 'float64' and np.iscomplexobj(a.data):
        _error('SCALAR_MISMATCH', 'complex input cannot be silently converted to float64')
    if config['problem']['scalar'] == 'complex128':
        a = a.astype(np.complex128)
    import_seconds = time.monotonic()-t0
    n = a.shape[0]
    target = config['target']
    requested = target['k'] if target['mode'] == 'nearest_k' else target['max_modes']
    if requested > _MAX_MODES or (target['mode'] == 'nearest_k' and requested >= n):
        _error('BACKEND_K_LIMIT', f'0 < k < n and at most {_MAX_MODES} requested modes required')
    t0 = time.monotonic()
    runtime.emit('PREPARING', 'matrix_ready', n=n, nnz=a.nnz, requested_modes=requested)
    with threadpool_limits(limits=config['resources']['threads']):
        effective_threadpools = _threadpools()
        input_digest = _sha(input_path)
        checkpoint_name='exact-block.checkpoint.npz' if config['solver']['block']['checkpoint_kind']=='exact_block' else 'subspace.checkpoint.npz'
        checkpoint_path = output_dir/checkpoint_name if config['solver']['block']['checkpoint_interval'] else None
        backend = create_backend(config, runtime=runtime, initial_subspace_path=initial_subspace_path, input_sha256=input_digest,
                                 checkpoint_path=checkpoint_path,resume_checkpoint_path=resume_checkpoint_path)
        backend.prepare(a)
        prepare_seconds = time.monotonic()-t0
        t0 = time.monotonic()
        backend.solve()
        candidate = backend.finalize()
        values, vectors, attempts, counters = candidate.values, candidate.vectors, candidate.attempts, candidate.counters
        solve_seconds = time.monotonic()-t0
        t0 = time.monotonic()
        validation, vectors = _validate(a, values, vectors, config)
        validation.update(_certify(a, values, vectors, config,check=runtime.check))
        validation_seconds = time.monotonic()-t0
    numerical = validation['numerical_status'] == 'PASSED'
    certified = validation['target_certification'] == 'CERTIFIED'
    enough = len(values) >= requested if target['mode'] == 'nearest_k' else certified
    status = 'SUCCEEDED' if numerical and certified and enough else ('PARTIAL' if numerical and (len(values) or certified) else 'FAILED')
    error_code = None if status == 'SUCCEEDED' else ('TARGET_UNVERIFIED' if numerical and len(values) else 'NO_VALID_MODES')
    runtime.emit('VALIDATING', 'independent_validation_completed', n_accepted=len(values), numerical_status=validation['numerical_status'], target_certification=validation['target_certification'])
    checkpoint_bytes = (backend.state_basis.nbytes+65536)*2 if backend.state_basis is not None else 0
    event_bytes = sum(len(json.dumps(event,ensure_ascii=False,allow_nan=False).encode('utf-8'))+1 for event in runtime.events)
    estimate_output = (vectors.nbytes*(3 if config['output']['format'] in ('scipy', 'both') else 1)+values.nbytes*8+
                       candidate.permutation.nbytes+checkpoint_bytes+event_bytes+1024**2)
    if estimate_output > config['resources']['disk_limit_gib']*1024**3:
        raise RuntimeError('RESOURCE_EXHAUSTED: output exceeds disk budget')
    if output_dir.exists() and any(path.name not in {'subspace.checkpoint.npz','exact-block.checkpoint.npz'} for path in output_dir.iterdir()):
        _error('OUTPUT_EXISTS', 'output directory must be empty')
    output_dir.mkdir(parents=True, exist_ok=True)
    # Publish the manifest last; without it the directory is an uncommitted run.
    t0 = time.monotonic()
    np.save(output_dir/'eigenvalues.npy', np.asarray(values, dtype=np.float64), allow_pickle=False)
    np.save(output_dir/'eigenvectors.npy', vectors, allow_pickle=False)
    np.save(output_dir/'residuals.npy', np.asarray(validation['residuals']), allow_pickle=False)
    if config['output']['format'] in ('scipy', 'both'):
        sparse.save_npz(output_dir/'eigenvalues.scipy.npz', sparse.csr_matrix(np.asarray(values).reshape(1, -1)))
        sparse.save_npz(output_dir/'eigenvectors.scipy.npz', sparse.csr_matrix(vectors))
    (output_dir/'config.resolved.yaml').write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=True), encoding='utf-8')
    _json(output_dir/'validation.json', validation)
    _json(output_dir/'environment.json', capabilities())
    np.save(output_dir/'permutation.npy', candidate.permutation, allow_pickle=False)
    _json(output_dir/'strategy.json', candidate.strategy)
    _json(output_dir/'memory-estimate.json', candidate.estimate)
    _json(output_dir/'diagnostics.json', candidate.diagnostics)
    if backend.state_basis is not None and backend.state_basis.shape[1]:
        from .checkpoint import save_checkpoint
        original_basis = np.empty_like(backend.state_basis)
        original_basis[candidate.permutation] = backend.state_basis
        save_checkpoint(output_dir/'subspace.checkpoint.npz', a, config, original_basis, input_sha256=input_digest)
    metrics = _clean({'n':n, 'nnz':a.nnz, 'backend':candidate.strategy['actual_backend'], 'method':candidate.strategy['actual_method'],
                      'prepare_seconds':prepare_seconds,
                      'import_seconds':import_seconds, 'solve_seconds':solve_seconds, 'validation_seconds':validation_seconds,
                      'export_seconds':time.monotonic()-t0, 'elapsed_seconds':time.monotonic()-runtime.start,
                      'peak_memory_bytes':runtime.peak, 'peak_memory_gib':runtime.peak/1024**3,
                      'memory_measurement':'sampled current process and descendants; worker enforces and samples full lifetime',
                      'event_history_limit':4096, 'events_emitted':runtime.events_emitted,
                      'event_history_byte_limit':runtime.event_history_byte_limit,'retained_event_bytes':runtime.retained_event_bytes,
                      'secondary_observation_enabled':config['observability']['enabled'], 'events_suppressed':runtime.events_suppressed,
                      'memory_sample_interval_seconds':runtime.memory_sample_interval_seconds,
                      'runtime_check_seconds':runtime.runtime_check_seconds, 'event_emission_seconds':runtime.event_emission_seconds,
                      'events_retained':len(runtime.events), 'events_dropped':max(0, runtime.events_emitted-len(runtime.events)),
                      'unavailable_metrics':{'outer_iterations':'SciPy ARPACK exposes no iteration counter' if counters['outer_iterations'] is None else None,
                                             'linear_iterations':'Direct LU does not use iterative inner solves' if counters['linear_iterations'] is None else None,
                                             'matrix_vector_products':'Backend internal operator products are not fully observable' if counters['matrix_vector_products'] is None else None},
                      'matrix_vector_products_scope':'Candidate iteration and candidate residual checks; final independent verifier excluded',
                      'matrix_vector_products_total':None,
                      'threads_requested':config['resources']['threads'], 'effective_threadpools':effective_threadpools, **counters})
    _json(output_dir/'metrics.json', metrics)
    runtime.emit('EXPORTING', 'artifacts_written', status=status)
    (output_dir/'events.jsonl').write_text(''.join(json.dumps(e, ensure_ascii=False, allow_nan=False)+'\n' for e in runtime.events), encoding='utf-8')
    # Re-read saved arrays and independently validate them before publishing.
    checked = verify_run(input_path, output_dir, config)
    if checked['numerical_status'] != validation['numerical_status'] or checked['target_certification'] != validation['target_certification']:
        raise RuntimeError('ARTIFACT_VERIFICATION_FAILED: saved results failed independent validation')
    runtime.check()
    if input_digest != _sha(input_path):
        raise RuntimeError('INPUT_CHANGED: original input changed while the solver was running')
    metrics.update(export_seconds=time.monotonic()-t0, elapsed_seconds=time.monotonic()-runtime.start,
                   peak_memory_bytes=runtime.peak, peak_memory_gib=runtime.peak/1024**3,
                   events_emitted=runtime.events_emitted, events_retained=len(runtime.events),
                   events_dropped=max(0, runtime.events_emitted-len(runtime.events)),
                   runtime_check_seconds=runtime.runtime_check_seconds, event_emission_seconds=runtime.event_emission_seconds)
    metrics['events_suppressed'] = runtime.events_suppressed
    metrics['retained_event_bytes'] = runtime.retained_event_bytes
    _json(output_dir/'metrics.json', metrics)
    _check_disk_budget(output_dir,config,reserve_bytes=65536)
    hashes = {p.name:_sha(p) for p in output_dir.iterdir() if p.is_file() and p.name != 'manifest.json'}
    manifest = {'schema_version':'1.0', 'status':status, 'error_code':error_code, 'input_sha256':input_digest,
                'config_sha256':_sha(output_dir/'config.resolved.yaml'), 'artifacts':hashes, 'n':n, 'n_accepted':len(values),
                'requested_k':target.get('k'), 'numerical_status':validation['numerical_status'],
                'target_certification':validation['target_certification'], 'attempts':attempts}
    temporary = output_dir/'manifest.json.tmp'
    _json(temporary, manifest)
    _check_disk_budget(output_dir,config)
    os.replace(temporary, output_dir/'manifest.json')
    return _clean({'status':status, 'error_code':error_code, 'eigenvalues':values.tolist(), 'residuals':validation['residuals'],
                   'validation':validation, 'metrics':metrics, 'attempts':attempts, 'n_accepted':len(values),
                   'strategy':candidate.strategy, 'memory_estimate':candidate.estimate, 'diagnostics':candidate.diagnostics})


def verify_run(input_path, output_dir, config=None):
    """Re-read original input and result arrays; never trust the saved validation."""
    output_dir = Path(output_dir)
    try:
        if config is None:
            config = yaml.safe_load((output_dir/'config.resolved.yaml').read_text(encoding='utf-8'))
        config = _config(config)
        a = load_matrix(input_path, storage=config['problem']['storage'], hermitian_rtol=config['problem']['hermitian_rtol'])
        values = np.load(output_dir/'eigenvalues.npy', allow_pickle=False)
        vectors = np.load(output_dir/'eigenvectors.npy', allow_pickle=False)
        report, normalised = _validate(a, values, vectors, config)
        report.update(_certify(a, values, normalised, config))
        manifest_path = output_dir/'manifest.json'
        if manifest_path.is_file():
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
            integrity = manifest.get('input_sha256') == _sha(input_path)
            for name, digest in manifest.get('artifacts', {}).items():
                if Path(name).name != name or not (output_dir/name).is_file() or _sha(output_dir/name) != digest:
                    integrity = False
            report['artifact_integrity'] = 'PASSED' if integrity else 'FAILED'
            if not integrity:
                report['numerical_status'] = 'FAILED'
                report['target_certification'] = 'UNVERIFIED'
                report['error_code'] = 'ARTIFACT_INTEGRITY_FAILED'
        return _clean(report)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        return {'numerical_status':'FAILED', 'target_certification':'UNVERIFIED', 'error_code':str(exc).split(':')[0],
                'n_validated':0}
