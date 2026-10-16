"""Bounded preflight for server-owned subspace snapshots; workers verify the basis."""
from __future__ import annotations
import json
from pathlib import Path
import zipfile
import numpy as np
import scipy
from .contracts import content_hash
from .errors import ServiceError
from .integrity import file_hash
from .numerics.checkpoint import ADAPTER_VERSION,compatible_warm_config_hashes


def validate_snapshot(path: Path, config: dict, input_hash: str) -> str:
    budget=int(min(config['resources']['memory_limit_gib']*1024**3*.25,1024**3))
    if path.is_symlink() or not path.is_file():raise ServiceError('CHECKPOINT_NOT_AVAILABLE',status=409)
    if path.stat().st_size>budget:raise ServiceError('INVALID_CHECKPOINT')
    try:
        with zipfile.ZipFile(path) as archive:
            members=archive.infolist()
            if len(members)!=2 or {entry.filename for entry in members}!={'basis.npy','metadata.npy'} or sum(entry.file_size for entry in members)>budget:
                raise ValueError('Invalid snapshot members')
            if archive.getinfo('metadata.npy').file_size>65536:raise ValueError('Oversized snapshot metadata')
            from .numerics.core import _check_npy_header
            with archive.open('metadata.npy') as handle:
                _check_npy_header(handle,archive.getinfo('metadata.npy').file_size,65536)
            with archive.open('metadata.npy') as handle:
                value=np.lib.format.read_array(handle,allow_pickle=False)
            if value.ndim!=0 or value.dtype.kind!='U':raise ValueError('Invalid snapshot metadata')
            metadata=json.loads(value.item())
    except (OSError,ValueError,TypeError,KeyError,EOFError,zipfile.BadZipFile,RecursionError):
        raise ServiceError('INVALID_CHECKPOINT') from None
    expected={'schema_version':'1.0','kind':'subspace_warm_start','exact_resume':False,
              'adapter_version':ADAPTER_VERSION,'numpy_version':np.__version__,'scipy_version':scipy.__version__,
              'input_sha256':input_hash,'vector_order':'original_input'}
    if not isinstance(metadata,dict):raise ServiceError('INVALID_CHECKPOINT')
    for key,value in expected.items():
        if metadata.get(key)!=value:raise ServiceError('CHECKPOINT_INCOMPATIBLE',status=409)
    if metadata.get('config_sha256') not in compatible_warm_config_hashes(config):
        raise ServiceError('CHECKPOINT_INCOMPATIBLE',status=409)
    return file_hash(path)


def validate_exact_snapshot(path: Path,config: dict,input_hash: str) -> str:
    """Bounded, streaming service preflight; worker binds matrix/environment."""
    from .numerics.exact_checkpoint import preflight_exact_checkpoint
    path=Path(path)
    if path.is_symlink() or not path.is_file():raise ServiceError('CHECKPOINT_NOT_AVAILABLE',status=409)
    try:
        before=file_hash(path)
        preflight_exact_checkpoint(path,config,input_sha256=input_hash)
        after=file_hash(path)
        if before!=after:raise ValueError('Checkpoint changed during preflight')
        return after
    except (OSError,ValueError,TypeError,KeyError,EOFError,RecursionError):
        raise ServiceError('INVALID_EXACT_CHECKPOINT',status=409) from None
