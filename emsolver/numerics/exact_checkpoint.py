"""Version-pinned next-iteration state for the fixed-shift block adapter.

Numeric NPZ members only; no pickle, code objects, LU factors, or implicit
controller reconstruction. A digest detects corruption, not authorship:
the service must additionally freeze the controlled checkpoint file's hash.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import platform
from types import CodeType
import zipfile

import numpy as np
import scipy

from emsolver.contracts import content_hash
from .checkpoint import matrix_fingerprint


_ARRAYS=('basis','best_values','best_vectors','locked')


def validate_profile(config):
    solver=config['solver']
    if (solver['block']['checkpoint_kind']!='exact_block' or solver['method']!='block_shift_invert' or
        solver['backend']!='scipy' or solver['adaptive_shift']['enabled'] or solver['fallback_profiles'] or
        not solver['block']['checkpoint_interval'] or config['target']['mode']!='nearest_k' or
        config['resources']['device']!='cpu' or config['resources']['mpi_ranks']!=1):
        raise ValueError('UNSUPPORTED_EXACT_CHECKPOINT: fixed-shift SciPy block with a periodic interval and no fallback required')


def _environment():
    from . import core
    keys=('internal_api','prefix','version','architecture','num_threads')
    libraries=[{key:item.get(key) for key in keys} for item in core._threadpools()]
    # Discovery order follows import order, not numerical identity. Preserve
    # multiplicity and every bound field while canonicalizing that order.
    libraries.sort(key=lambda item:json.dumps(item,sort_keys=True,separators=(',',':'),allow_nan=False))
    return {'python_version':platform.python_version(),'numpy_version':np.__version__,'scipy_version':scipy.__version__,
            'system':platform.system(),'machine':platform.machine(),
            'threadpools':libraries}


def _code_value(value):
    """Encode loaded semantics without marshal's intern/reference-layout flags."""
    if isinstance(value,CodeType):
        fields=('co_argcount','co_posonlyargcount','co_kwonlyargcount','co_nlocals',
                'co_stacksize','co_flags','co_code','co_consts','co_names','co_varnames',
                'co_freevars','co_cellvars','co_name','co_qualname','co_firstlineno',
                'co_linetable','co_exceptiontable')
        # Installation paths may differ; Python version and disk source are bound
        # separately. Nested code and all executable/exception metadata stay bound.
        return ['code',{field:_code_value(getattr(value,field)) for field in fields}]
    if value is None:return ['none']
    if value is Ellipsis:return ['ellipsis']
    if type(value) is bool:return ['bool',value]
    if type(value) is int:return ['int',str(value)]
    if type(value) is float:return ['float',value.hex()]
    if type(value) is complex:return ['complex',value.real.hex(),value.imag.hex()]
    if type(value) is str:return ['str',value]
    if type(value) is bytes:return ['bytes',value.hex()]
    if type(value) is tuple:return ['tuple',[_code_value(item) for item in value]]
    if type(value) is frozenset:
        encoded=[_code_value(item) for item in value]
        return ['frozenset',sorted(encoded,key=lambda item:json.dumps(item,sort_keys=True,separators=(',',':')))]
    raise ValueError('INVALID_EXACT_CHECKPOINT: unidentifiable loaded code constant')


def _loaded_algorithm_hash():
    from . import block,core,shift_control
    functions={
        **{'block.'+name:getattr(block,name) for name in ('_basis','_reorthogonalize','solve_block')},
        **{'core.'+name:getattr(core,name) for name in ('_validate','_normalise_vectors','_scale','_divide_sparse',
                                                       '_norm_inf_scaled','_target_order','_cluster_tol','_stable_norm2')},
        'shift_control.create_shift_policy':shift_control.create_shift_policy,
    }
    for cls in (shift_control._LegacyScalePolicy,shift_control.FeedbackShiftPolicy):
        for name,item in vars(cls).items():
            if hasattr(item,'__code__'):functions[cls.__name__+'.'+name]=item
    functions_encoded={}
    for name,function in sorted(functions.items()):
        if not hasattr(function,'__code__'):raise ValueError('INVALID_EXACT_CHECKPOINT: unidentifiable loaded algorithm')
        functions_encoded[name]=_code_value(function.__code__)
    encoded=json.dumps({'encoding':'loaded_code_semantics_v1','functions':functions_encoded},
                       sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _base(matrix,config,input_sha256,permutation):
    source=Path(__file__).parent
    return {'schema_version':'1.0','kind':'exact_block','exact_resume':True,
            'adapter_version':'fixed_shift_block_state_v1','phase':'next_iteration_ready',
            'vector_order':'original_input','input_sha256':input_sha256,
            'normalized_matrix_sha256':matrix_fingerprint(matrix),'config_sha256':content_hash(config),
            'permutation_sha256':hashlib.sha256(np.asarray(permutation,dtype='<i8').tobytes()).hexdigest(),
            'implementation_sha256':{name:hashlib.sha256((source/name).read_bytes()).hexdigest()
                                      for name in ('block.py','core.py','exact_checkpoint.py','shift_control.py','shift_policy.py')},
            'loaded_algorithm_sha256':_loaded_algorithm_hash(),
            'environment':_environment()}


def _digest(arrays,metadata):
    digest=hashlib.sha256(json.dumps(metadata,sort_keys=True,separators=(',',':'),allow_nan=False).encode())
    for name in _ARRAYS:
        array=np.ascontiguousarray(arrays[name])
        digest.update(name.encode());digest.update(array.dtype.str.encode())
        digest.update(np.asarray(array.shape,dtype='<i8').tobytes());digest.update(array.tobytes())
    return digest.hexdigest()


def _pairs(items):
    result={}
    for key,value in items:
        if key in result:raise ValueError('INVALID_EXACT_CHECKPOINT: duplicate metadata field')
        result[key]=value
    return result


def _constant(_):raise ValueError('INVALID_EXACT_CHECKPOINT: nonfinite JSON constant')


def _validate_state(arrays,state,matrix,config):
    from . import core
    n=matrix.shape[0];solver=config['solver'];k=config['target']['k']
    internal=min(k+solver['block']['oversampling'],solver['subspace_max'],n-1)
    dtype=np.dtype(np.complex128 if np.iscomplexobj(matrix.data) else np.float64)
    basis,values,vectors,locked=(arrays[name] for name in _ARRAYS)
    if (basis.shape!=(n,internal) or basis.dtype!=dtype or values.ndim!=1 or values.dtype!=np.dtype('float64') or
        len(values)>internal or vectors.shape!=(n,len(values)) or vectors.dtype!=dtype or
        locked.shape!=(internal,) or locked.dtype!=np.dtype('bool') or
        not all(np.isfinite(array).all() for array in (basis,values,vectors))):
        raise ValueError('INVALID_EXACT_CHECKPOINT: bounded state array shape/dtype/finite failure')
    if np.linalg.norm(basis.conjugate().T@basis-np.eye(internal),ord=2)>config['validation']['orthogonality_max']:
        raise ValueError('INVALID_EXACT_CHECKPOINT: basis is not orthonormal')
    if values.size:
        report,_=core._validate(matrix,values,vectors,config)
        if report['numerical_status']!='PASSED':raise ValueError('INVALID_EXACT_CHECKPOINT: best candidates fail original problem validation')
    required={'iteration','counters','best_residual','stalled_checks','rng_state','shift_scaled','matrix_scale',
              'elapsed_seconds','attempt_seconds','checkpoint_sequence'}
    if not isinstance(state,dict) or set(state)!=required:raise ValueError('INVALID_EXACT_CHECKPOINT: controller fields differ')
    iteration=state['iteration'];counters=state['counters']
    if type(iteration) is not int or not 1<=iteration<solver['max_outer_iterations']:
        raise ValueError('INVALID_EXACT_CHECKPOINT: no remaining outer iteration budget')
    for key in ('best_residual','elapsed_seconds','attempt_seconds','shift_scaled','matrix_scale'):
        if type(state[key]) not in (int,float) or not math.isfinite(state[key]):raise ValueError('INVALID_EXACT_CHECKPOINT: nonfinite controller')
    if (state['best_residual']<0 or state['elapsed_seconds']<0 or state['attempt_seconds']<0 or
        type(state['stalled_checks']) is not int or not 0<=state['stalled_checks']<=iteration or
        type(state['checkpoint_sequence']) is not int or not 1<=state['checkpoint_sequence']<=iteration):
        raise ValueError('INVALID_EXACT_CHECKPOINT: invalid controller bounds')
    scale=core._scale(matrix) or 1.
    if state['matrix_scale']!=scale or state['shift_scaled']!=config['target']['sigma']/scale:
        raise ValueError('INVALID_EXACT_CHECKPOINT: fixed working shift changed')
    required_counters={'factorizations','linear_solves','linear_solve_batches','matrix_vector_products','shifted_residual_matvecs',
                       'outer_iterations','linear_iterations','max_linear_residual','residual_checks','restarts','soft_locked_modes',
                       'warm_start','residual_check_interval','residual_check_seconds'}
    optional={'exact_resume','restored_outer_iterations','outer_iterations_executed_this_process'}
    if not isinstance(counters,dict) or not required_counters<=counters.keys() or set(counters)-required_counters-optional:
        raise ValueError('INVALID_EXACT_CHECKPOINT: unexpected numerical counters')
    for key,value in counters.items():
        if key in ('warm_start','exact_resume'):
            if type(value) is not bool:raise ValueError('INVALID_EXACT_CHECKPOINT: invalid boolean counter')
        elif key=='linear_iterations':
            if value is not None:raise ValueError('INVALID_EXACT_CHECKPOINT: direct LU has no iterative counter')
        elif key in ('max_linear_residual','residual_check_seconds'):
            if type(value) not in (int,float) or not math.isfinite(value) or value<0:
                raise ValueError('INVALID_EXACT_CHECKPOINT: invalid numerical counter')
        elif type(value) is not int or value<0:raise ValueError('INVALID_EXACT_CHECKPOINT: noninteger work counter')
    if (counters['outer_iterations']!=iteration or counters['restarts']>solver['block']['max_restarts'] or
        counters['residual_check_interval']!=solver['block']['residual_check_interval'] or
        counters['soft_locked_modes']!=int(np.sum(locked)) or counters['linear_solves']>iteration*internal or
        counters['residual_checks']>iteration):
        raise ValueError('INVALID_EXACT_CHECKPOINT: counter/controller mismatch')
    rng=np.random.default_rng()
    try:rng.bit_generator.state=state['rng_state']
    except (ValueError,TypeError,KeyError) as error:raise ValueError('INVALID_EXACT_CHECKPOINT: invalid RNG state') from error


def save_exact_checkpoint(path,matrix,config,state,*,input_sha256=None,permutation=None):
    from . import core
    validate_profile(config)
    permutation=np.arange(matrix.shape[0]) if permutation is None else permutation
    arrays={name:np.ascontiguousarray(state[name]) for name in _ARRAYS}
    controller={key:value for key,value in state.items() if key not in _ARRAYS}
    _validate_state(arrays,controller,matrix,config)
    metadata={**_base(matrix,config,input_sha256,permutation),'state':controller}
    metadata['state_sha256']=_digest(arrays,metadata)
    encoded=json.dumps(metadata,sort_keys=True,allow_nan=False)
    if len(encoded.encode('utf-8'))>65536:raise ValueError('INVALID_EXACT_CHECKPOINT: metadata exceeds budget')
    path=Path(path);temporary=path.with_name(path.name+'.tmp')
    size=sum(array.nbytes for array in arrays.values())
    core._check_disk_budget(path.parent,config,reserve_bytes=int(size*1.01)+131072)
    with temporary.open('wb') as handle:np.savez_compressed(handle,**arrays,metadata=np.asarray(encoded))
    core._check_disk_budget(path.parent,config)
    os.replace(temporary,path)
    return metadata


def load_exact_checkpoint(path,matrix,config,*,input_sha256=None,permutation=None):
    from . import core
    validate_profile(config)
    path=Path(path);budget=int(min(config['resources']['memory_limit_gib']*1024**3*.25,1024**3))
    if path.is_symlink() or not path.is_file() or path.stat().st_size>budget:
        raise ValueError('INVALID_EXACT_CHECKPOINT: missing, linked, or over-budget state')
    try:
        with zipfile.ZipFile(path) as archive:
            members=archive.infolist()
            if len(members)!=5 or {entry.filename for entry in members}!={name+'.npy' for name in (*_ARRAYS,'metadata')}:
                raise ValueError('INVALID_EXACT_CHECKPOINT: unexpected archive members')
            if sum(entry.file_size for entry in members)>budget:raise ValueError('INVALID_EXACT_CHECKPOINT: expanded state exceeds budget')
            for entry in members:
                with archive.open(entry) as handle:core._check_npy_header(handle,entry.file_size,budget)
        with np.load(path,allow_pickle=False) as archive:
            raw=archive['metadata']
            if raw.shape!=() or raw.dtype.kind!='U' or raw.nbytes>262144:raise ValueError('INVALID_EXACT_CHECKPOINT: malformed metadata')
            metadata=json.loads(str(raw.item()),object_pairs_hook=_pairs,parse_constant=_constant)
            arrays={name:archive[name] for name in _ARRAYS}
    except (OSError,zipfile.BadZipFile,EOFError,TypeError,json.JSONDecodeError,RecursionError) as error:
        raise ValueError('INVALID_EXACT_CHECKPOINT: invalid numeric archive') from error
    if not isinstance(metadata,dict):raise ValueError('INVALID_EXACT_CHECKPOINT: metadata must be an object')
    checksum=metadata.pop('state_sha256',None)
    if checksum!=_digest(arrays,metadata):raise ValueError('EXACT_CHECKPOINT_INTEGRITY_FAILED: state digest differs')
    permutation=np.arange(matrix.shape[0]) if permutation is None else permutation
    expected=_base(matrix,config,input_sha256,permutation)
    if set(metadata)!=set(expected)|{'state'}:raise ValueError('INVALID_EXACT_CHECKPOINT: metadata fields differ')
    for key,value in expected.items():
        if metadata.get(key)!=value:raise ValueError(f'EXACT_CHECKPOINT_MISMATCH: {key} differs')
    state=metadata['state']
    _validate_state(arrays,state,matrix,config)
    return {**state,**arrays},metadata


def _header(handle):
    version=np.lib.format.read_magic(handle)
    if version==(1,0):return np.lib.format.read_array_header_1_0(handle)
    if version in ((2,0),(3,0)):return np.lib.format.read_array_header_2_0(handle)
    raise ValueError('INVALID_EXACT_CHECKPOINT: unsupported numeric header')


def preflight_exact_checkpoint(path,config,*,input_sha256,expected_n=None):
    """Stream-check archive identity/digest without allocating candidate arrays.

    Matrix fingerprint, numerical controller validity and environment matching
    are checked again by the worker under its configured thread limits.
    """
    from . import core
    validate_profile(config)
    path=Path(path);budget=int(min(config['resources']['memory_limit_gib']*1024**3*.25,1024**3))
    if path.is_symlink() or not path.is_file() or path.stat().st_size>budget:
        raise ValueError('INVALID_EXACT_CHECKPOINT: missing, linked, or over-budget state')
    try:
        with zipfile.ZipFile(path) as archive:
            members=archive.infolist()
            if len(members)!=5 or {entry.filename for entry in members}!={name+'.npy' for name in (*_ARRAYS,'metadata')}:
                raise ValueError('INVALID_EXACT_CHECKPOINT: unexpected archive members')
            if sum(entry.file_size for entry in members)>budget:raise ValueError('INVALID_EXACT_CHECKPOINT: expanded state exceeds budget')
            headers={}
            for entry in members:
                with archive.open(entry) as handle:core._check_npy_header(handle,entry.file_size,budget)
                with archive.open(entry) as handle:headers[entry.filename[:-4]]=_header(handle)
            shape,_,dtype=headers['metadata']
            if shape!=() or dtype.kind!='U' or dtype.itemsize>262144:
                raise ValueError('INVALID_EXACT_CHECKPOINT: malformed bounded metadata')
            with archive.open('metadata.npy') as handle:raw=np.lib.format.read_array(handle,allow_pickle=False)
            encoded=str(raw.item())
            if len(encoded.encode('utf-8'))>65536:raise ValueError('INVALID_EXACT_CHECKPOINT: metadata exceeds budget')
            metadata=json.loads(encoded,object_pairs_hook=_pairs,parse_constant=_constant)
            if not isinstance(metadata,dict):raise ValueError('INVALID_EXACT_CHECKPOINT: metadata must be an object')
            expected={'schema_version':'1.0','kind':'exact_block','exact_resume':True,
                      'adapter_version':'fixed_shift_block_state_v1','phase':'next_iteration_ready','vector_order':'original_input',
                      'input_sha256':input_sha256,'config_sha256':content_hash(config)}
            for key,value in expected.items():
                if type(metadata.get(key)) is not type(value) or metadata.get(key)!=value:
                    raise ValueError(f'EXACT_CHECKPOINT_MISMATCH: {key} differs')
            shape,fortran,dtype=headers['basis']
            if len(shape)!=2 or not 0<shape[1]<shape[0] or (expected_n is not None and shape[0]!=expected_n):
                raise ValueError('INVALID_EXACT_CHECKPOINT: invalid basis shape')
            n,internal=shape
            if internal!=min(config['target']['k']+config['solver']['block']['oversampling'],config['solver']['subspace_max'],n-1):
                raise ValueError('INVALID_EXACT_CHECKPOINT: incompatible bounded subspace')
            if dtype not in (np.dtype('float64'),np.dtype('complex128')):
                raise ValueError('INVALID_EXACT_CHECKPOINT: invalid basis dtype')
            value_shape,_,value_dtype=headers['best_values']
            if len(value_shape)!=1 or value_shape[0]>internal or value_dtype!=np.dtype('float64'):
                raise ValueError('INVALID_EXACT_CHECKPOINT: invalid candidate shape')
            if headers['best_vectors'][0]!=(n,value_shape[0]) or headers['best_vectors'][2]!=dtype:
                raise ValueError('INVALID_EXACT_CHECKPOINT: invalid candidate vector header')
            if headers['locked'][0]!=(internal,) or headers['locked'][2]!=np.dtype('bool'):
                raise ValueError('INVALID_EXACT_CHECKPOINT: invalid lock header')
            state=metadata.get('state')
            if (not isinstance(state,dict) or type(state.get('iteration')) is not int or
                not 1<=state['iteration']<config['solver']['max_outer_iterations']):
                raise ValueError('INVALID_EXACT_CHECKPOINT: no remaining outer budget')
            elapsed=state.get('elapsed_seconds')
            if type(elapsed) not in (int,float) or not math.isfinite(elapsed) or elapsed<0:
                raise ValueError('INVALID_EXACT_CHECKPOINT: invalid elapsed budget')
            if elapsed>=config['resources']['walltime_seconds']:
                raise ValueError('WALLTIME_EXCEEDED: no remaining checkpoint time budget')
            checksum=metadata.get('state_sha256')
            unsigned={key:value for key,value in metadata.items() if key!='state_sha256'}
            digest=hashlib.sha256(json.dumps(unsigned,sort_keys=True,separators=(',',':'),allow_nan=False).encode())
            for name in _ARRAYS:
                shape,fortran,dtype=headers[name]
                if fortran:raise ValueError('INVALID_EXACT_CHECKPOINT: noncanonical storage order')
                digest.update(name.encode());digest.update(dtype.str.encode());digest.update(np.asarray(shape,dtype='<i8').tobytes())
                with archive.open(name+'.npy') as handle:
                    _header(handle)
                    for block in iter(lambda:handle.read(1024**2),b''):digest.update(block)
            if checksum!=digest.hexdigest():raise ValueError('EXACT_CHECKPOINT_INTEGRITY_FAILED: state digest differs')
            return metadata
    except (OSError,zipfile.BadZipFile,EOFError,TypeError,json.JSONDecodeError,RecursionError) as error:
        raise ValueError('INVALID_EXACT_CHECKPOINT: invalid bounded archive') from error
