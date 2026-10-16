"""Bounded inline reference comparison child; never imported by solvers."""
from __future__ import annotations

import json
from pathlib import Path
import sys

import numpy as np

from .engineering import canonical
from .evaluation import evaluate_results
from .industrial_contracts import MAX_REFERENCE_ENTRIES, MAX_REFERENCE_MODES


def main():
    spec_path, output_path = map(Path, sys.argv[1:])
    if spec_path.stat().st_size > 16384: raise ValueError('Spec limit')
    spec = json.loads(spec_path.read_text(encoding='utf-8'))
    candidate, reference = Path(spec['candidate']), Path(spec['reference'])
    for folder in (candidate, reference):
        for filename in ('eigenvalues.npy', 'eigenvectors.npy'):
            path = folder / filename
            if path.is_symlink() or path.stat().st_size > MAX_REFERENCE_ENTRIES * 16 + 4096: raise ValueError('Reference limit')
        values = np.load(folder / 'eigenvalues.npy', mmap_mode='r', allow_pickle=False)
        vectors = np.load(folder / 'eigenvectors.npy', mmap_mode='r', allow_pickle=False)
        if (values.ndim != 1 or vectors.ndim != 2 or values.size > MAX_REFERENCE_MODES or
            vectors.size > MAX_REFERENCE_ENTRIES or vectors.shape[1] != values.size or
            values.dtype.kind != 'f' or vectors.dtype.kind not in {'f', 'c'}): raise ValueError('Reference shape')
    criteria = spec['criteria']
    result = evaluate_results(candidate, reference, criteria['max_eigenvalue_absolute_error'],
        criteria['max_eigenvalue_relative_error'], criteria['max_subspace_sine'])
    result.update(reference_eigenvalues=np.load(reference / 'eigenvalues.npy', allow_pickle=False).tolist(),
        candidate_eigenvalues=np.load(candidate / 'eigenvalues.npy', allow_pickle=False).tolist())
    output_path.write_bytes(canonical(result))


if __name__ == '__main__': main()
