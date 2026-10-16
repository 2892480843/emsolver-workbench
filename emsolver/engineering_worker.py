"""Original-input sparse nullspace verification; no physical mode removal."""
from __future__ import annotations
import json
from pathlib import Path
import sys


def verify(matrix_path, manifest, storage='full'):
    import numpy as np
    from scipy import sparse
    from .numerics.core import load_matrix, _scale, _divide_sparse, _norm_inf_scaled
    from .integrity import file_hash
    if file_hash(matrix_path) != manifest['matrix_sha256']: raise ValueError('INPUT_CHANGED')
    matrix = load_matrix(Path(matrix_path), storage=storage, max_bytes=1024**3)
    if matrix.shape[0] != len(manifest['dof_nodes']): raise ValueError('INPUT_CHANGED')
    declaration = manifest['nullspace']
    columns = []
    for basis in declaration['basis']:
        values = np.asarray(basis['real']) + 1j * np.asarray(basis['imag'] or np.zeros(len(basis['real'])))
        maximum = np.max(np.abs(values))
        if not np.isfinite(maximum) or maximum == 0: raise ValueError('INVALID_NULLSPACE')
        column = sparse.csc_matrix((values / maximum, (basis['indices'], np.zeros(len(values), dtype=int))), shape=(matrix.shape[0], 1))
        norm = np.sqrt(float((column.conjugate().T @ column).real[0, 0]))
        columns.append(column / norm)
    basis = sparse.hstack(columns, format='csc')
    sparse_gram = (basis.conjugate().T @ basis).tocoo()
    gram = np.zeros((len(columns), len(columns)), dtype=np.complex128)
    gram[sparse_gram.row, sparse_gram.col] = sparse_gram.data  # at most 16 x 16
    if np.min(np.linalg.eigvalsh(gram)) < 1e-12: raise ValueError('DEPENDENT_NULLSPACE')
    scale = _scale(matrix)
    scaled = _divide_sparse(matrix, scale) if scale else matrix
    norm = _norm_inf_scaled(scaled)
    residuals = []
    for column in columns:
        product = scaled @ column
        denominator = norm * float(np.max(np.abs(column.data), initial=0))
        numerator = float(np.max(np.abs(product.data), initial=0))
        residuals.append(numerator / denominator if denominator else (0. if numerator == 0 else float('inf')))
    if file_hash(matrix_path) != manifest['matrix_sha256']: raise ValueError('INPUT_CHANGED')
    passed = all(np.isfinite(value) and value <= declaration['residual_tolerance'] for value in residuals)
    return {'status': 'VERIFIED' if passed else 'UNVERIFIED', 'basis_count': len(columns), 'residuals': residuals,
            'residual_tolerance': declaration['residual_tolerance'], 'interpretation': declaration['interpretation'],
            'verification_input': 'original matrix', 'automatic_removal': False,
            'physical_interpretation': 'user-declared only; matrix nullspace does not identify spurious physical modes'}


def main():
    specification = json.loads(Path(sys.argv[1]).read_text())
    result = verify(specification['matrix_path'], specification['manifest'], specification.get('matrix_storage','full'))
    path = Path(sys.argv[2])
    path.write_text(json.dumps(result, allow_nan=False), encoding='utf-8')


if __name__ == '__main__': main()
