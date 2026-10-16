import hashlib
import json
from pathlib import Path
import zipfile

import numpy as np
import pytest
from scipy import sparse

from emsolver.numerics.slepc_driver import read_csr, read_spec


def specification(tmp_path):
    matrix = tmp_path / 'matrix.npz'
    sparse.save_npz(matrix, sparse.diags([np.ones(11) * .02, np.linspace(-2, 3, 12), np.ones(11) * .02], [-1, 0, 1], format='csr'))
    spec = {'schema_version': '1.0', 'input_sha256': hashlib.sha256(matrix.read_bytes()).hexdigest(),
            'config': {'target': {'mode': 'nearest_k', 'k': 2, 'sigma': .1},
                       'solver': {'method': 'krylovschur', 'subspace_max': 10, 'max_outer_iterations': 100, 'tolerance': 1e-11,
                                  'random_seed': 20261016, 'monitor_enabled': True},
                       'resources': {'mpi_ranks': 1, 'threads': 1},
                       'problem': {'hermitian_rtol': 1e-12}}}
    path = tmp_path / 'spec.json'
    path.write_text(json.dumps(spec))
    return path, matrix, spec


def test_driver_uses_fixed_csr_snapshot_and_exact_specification(tmp_path):
    path, matrix, expected = specification(tmp_path)
    assert read_spec(path) == expected
    actual = read_csr(matrix, expected['input_sha256'])
    assert actual.shape == (12, 12) and actual.nnz == 34
    assert np.allclose(actual.toarray(), actual.conjugate().T.toarray())


@pytest.mark.parametrize('change', ['reference_path', 'input_path', 'duplicate', 'nan', 'oversized', 'bad_hash', 'bad_rank', 'bad_method'])
def test_driver_rejects_extra_paths_ambiguous_or_unbounded_specs(tmp_path, change):
    path, _, spec = specification(tmp_path)
    if change in {'reference_path', 'input_path'}:
        spec[change] = '/outside/reference.npz'
    elif change == 'duplicate':
        path.write_text('{"schema_version":"1.0","schema_version":"1.0"}')
    elif change == 'nan':
        spec['config']['target']['sigma'] = float('nan')
    elif change == 'oversized':
        path.write_bytes(b' ' * (1024**2 + 1))
    elif change == 'bad_hash':
        spec['input_sha256'] = 'wrong'
    elif change == 'bad_rank':
        spec['config']['resources']['mpi_ranks'] = True
    elif change == 'bad_method':
        spec['config']['solver']['method'] = 'arpack'
    if change not in {'duplicate', 'oversized'}:
        path.write_text(json.dumps(spec))
    with pytest.raises(ValueError):
        read_spec(path)


def test_driver_rejects_changed_input_symlinks_and_unsafe_object_arrays(tmp_path):
    _, matrix, spec = specification(tmp_path)
    with pytest.raises(ValueError):
        read_csr(matrix, '0' * 64)
    alias = tmp_path / 'alias.npz'
    alias.symlink_to(matrix)
    with pytest.raises(ValueError):
        read_csr(alias, spec['input_sha256'])
    bad = tmp_path / 'object.npz'
    np.savez(bad, data=np.asarray([object()], dtype=object))
    with pytest.raises(ValueError):
        read_csr(bad, hashlib.sha256(bad.read_bytes()).hexdigest())


def test_driver_rejects_declared_array_bomb_before_numpy_loading(tmp_path):
    from io import BytesIO
    header = BytesIO()
    np.lib.format.write_array_header_1_0(header, {'descr': '<f8', 'fortran_order': False, 'shape': (10**12,)})
    bad = tmp_path / 'bomb.npz'
    with zipfile.ZipFile(bad, 'w') as archive:
        archive.writestr('data.npy', header.getvalue())
    with pytest.raises(ValueError):
        read_csr(bad, hashlib.sha256(bad.read_bytes()).hexdigest())
