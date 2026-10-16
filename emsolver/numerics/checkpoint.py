"""Safe subspace snapshots for new tasks, never exact ARPACK continuation."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import zipfile

import numpy as np
import scipy

from emsolver.contracts import content_hash


ADAPTER_VERSION = 'block_inverse_v1'


def compatible_warm_config_hashes(config):
    hashes={content_hash(config)}
    if config['solver']['block'].get('checkpoint_kind')=='subspace':
        legacy={**config,'solver':{**config['solver'],'block':dict(config['solver']['block'])}}
        legacy['solver']['block'].pop('checkpoint_kind')
        hashes.add(content_hash(legacy))
    return hashes


def matrix_fingerprint(matrix):
    digest = hashlib.sha256()
    # Canonical index widths and little-endian values make the fingerprint
    # independent of CSR's chosen 32/64-bit index representation.
    for array in (np.asarray(matrix.shape, dtype='<i8'), matrix.indptr.astype('<i8'),
                  matrix.indices.astype('<i8'), matrix.data.astype('<c16' if np.iscomplexobj(matrix.data) else '<f8')):
        digest.update(array.tobytes())
    return digest.hexdigest()


def checkpoint_metadata(matrix, config, input_sha256=None):
    return {'schema_version':'1.0', 'kind':'subspace_warm_start', 'exact_resume':False,
            'adapter_version':ADAPTER_VERSION, 'numpy_version':np.__version__, 'scipy_version':scipy.__version__,
            'input_sha256':input_sha256, 'normalized_matrix_sha256':matrix_fingerprint(matrix),
            'config_sha256':content_hash(config), 'vector_order':'original_input'}


def save_checkpoint(path, matrix, config, vectors, *, input_sha256=None, progress=None):
    from . import core
    path = Path(path)
    basis = core._normalise_vectors(vectors)
    if basis.shape[0] != matrix.shape[0] or basis.shape[1] > min(matrix.shape[0]-1, core._MAX_SUBSPACE):
        raise ValueError('INVALID_CHECKPOINT: subspace dimensions exceed bounds')
    metadata = checkpoint_metadata(matrix, config, input_sha256)
    if progress is not None:
        metadata['progress'] = progress
    temporary = path.with_name(path.name+'.tmp')
    # Atomic replacement temporarily retains both the previous snapshot and
    # the new ZIP. DEFLATE can grow incompressible arrays slightly.
    core._check_disk_budget(path.parent,config,reserve_bytes=int(basis.nbytes*1.01)+65536)
    with temporary.open('wb') as handle:
        np.savez_compressed(handle, basis=basis, metadata=np.asarray(json.dumps(metadata, sort_keys=True)))
    core._check_disk_budget(path.parent,config)
    os.replace(temporary, path)
    return metadata


def load_checkpoint(path, matrix, config, *, input_sha256=None):
    from . import core
    path = Path(path)
    budget = int(min(config['resources']['memory_limit_gib']*1024**3*0.25, 1024**3))
    if not path.is_file() or path.stat().st_size > budget:
        raise ValueError('INVALID_CHECKPOINT: missing or over-budget snapshot')
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) != 2 or {entry.filename for entry in members} != {'basis.npy', 'metadata.npy'}:
                raise ValueError('INVALID_CHECKPOINT: only numeric basis and metadata fields are accepted')
            if sum(entry.file_size for entry in members) > budget:
                raise ValueError('INVALID_CHECKPOINT: expanded snapshot exceeds budget')
            for entry in members:
                with archive.open(entry) as handle:
                    core._check_npy_header(handle, entry.file_size, budget)
        with np.load(path, allow_pickle=False) as archive:
            metadata_array = archive['metadata']
            if metadata_array.shape != () or metadata_array.dtype.kind != 'U' or metadata_array.nbytes > 65536:
                raise ValueError('INVALID_CHECKPOINT: malformed metadata')
            metadata = json.loads(str(metadata_array.item()))
            basis = archive['basis']
    except (OSError, zipfile.BadZipFile, TypeError, json.JSONDecodeError, EOFError) as exc:
        raise ValueError('INVALID_CHECKPOINT: invalid numeric snapshot') from exc
    if not isinstance(metadata, dict):
        raise ValueError('INVALID_CHECKPOINT: metadata must be an object')
    expected = checkpoint_metadata(matrix, config, input_sha256)
    if metadata.get('normalized_matrix_sha256') != expected['normalized_matrix_sha256'] or metadata.get('input_sha256') != expected['input_sha256']:
        raise ValueError('CHECKPOINT_INPUT_MISMATCH: snapshot belongs to a different original input')
    # v0.4.0 snapshots predate this one semantically inert default key.
    # No other strategy/default fields may be removed for compatibility.
    if metadata.get('config_sha256') not in compatible_warm_config_hashes(config):
        raise ValueError('CHECKPOINT_CONFIG_MISMATCH: resolved configuration differs')
    for name in ('schema_version', 'kind', 'exact_resume', 'adapter_version', 'numpy_version', 'scipy_version', 'vector_order'):
        if metadata.get(name) != expected[name]:
            raise ValueError('CHECKPOINT_VERSION_MISMATCH: snapshot schema or numerical backend differs')
    if basis.dtype.kind not in 'fc' or basis.ndim != 2 or basis.shape[0] != matrix.shape[0] or not 0 < basis.shape[1] < matrix.shape[0] or basis.shape[1] > core._MAX_SUBSPACE:
        raise ValueError('INVALID_CHECKPOINT: invalid subspace dimensions or dtype')
    normalized = core._normalise_vectors(basis)
    gram = normalized.conjugate().T@normalized
    if np.linalg.norm(gram-np.eye(normalized.shape[1]), ord=2) > config['validation']['orthogonality_max']:
        raise ValueError('INVALID_CHECKPOINT: checkpoint basis is not orthonormal')
    if not np.iscomplexobj(matrix.data) and np.iscomplexobj(normalized) and np.any(normalized.imag != 0):
        raise ValueError('INVALID_CHECKPOINT: complex basis cannot enter real adapter')
    return normalized, metadata
