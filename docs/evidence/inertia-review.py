"""Independent small exact-Hermitian inertia cross-check; no solver is used."""
from __future__ import annotations

import json
from pathlib import Path
import sys

import numpy as np
from scipy import sparse

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from emsolver.integrity import file_hash
from emsolver.numerics.certification import InertiaOracle

seed = 2026101601
rng = np.random.default_rng(seed)
records = []
for scalar in ('float64', 'complex128'):
    for n in range(2, 9):
        for repeat in range(8):
            upper = np.triu(rng.integers(-4, 5, (n, n)), 1) / 8.
            if scalar == 'complex128':
                upper = upper + 1j * np.triu(rng.integers(-4, 5, (n, n)), 1) / 8.
            diagonal = rng.integers(-8, 9, n) / 8. if repeat % 2 == 0 else np.zeros(n)
            matrix = sparse.csr_matrix(upper + upper.conjugate().T + np.diag(diagonal))
            oracle = InertiaOracle(matrix)
            reference = np.linalg.eigvalsh(matrix.toarray())
            for shift in (-2.125, 0., .375, 3.25):
                observed = oracle.inertia(shift)
                shifted = reference - shift
                expected = (int(np.count_nonzero(shifted < -1e-10)),
                            int(np.count_nonzero(abs(shifted) <= 1e-10)),
                            int(np.count_nonzero(shifted > 1e-10)))
                # A numerical reference cannot resolve arbitrary near-zero signs.
                ambiguous = bool(np.any((abs(shifted) > 1e-12) & (abs(shifted) < 1e-8)))
                assert not ambiguous, (scalar, n, repeat, shift, shifted)
                assert observed == expected, (scalar, n, repeat, shift, observed, expected)
                records.append({'scalar': scalar, 'n': n, 'repeat': repeat, 'shift': shift,
                                'exact_inertia': observed, 'reference_inertia': expected,
                                'reference_ambiguity': ambiguous, 'passed': True})
evidence = {'schema_version': '1.0', 'seed': seed, 'rng': 'numpy.random.default_rng/PCG64',
    'matrix_distribution': 'Strict upper real and optional imaginary integers in [-4,4]/8; conjugate transpose; even repeats diagonal integers in [-8,8]/8, odd repeats zero diagonal',
    'n_range': [2, 8], 'matrices_per_n_per_scalar': 8, 'matrix_count': 112,
    'shifts': [-2.125, 0., .375, 3.25], 'checks': len(records), 'passed': sum(r['passed'] for r in records),
    'reference': 'numpy.linalg.eigvalsh on dense matrix, sign tolerance 1e-10; explicit ambiguity rejection for gaps between 1e-12 and 1e-8',
    'source_sha256': file_hash(ROOT / 'emsolver/numerics/certification.py'),
    'scope': 'Independent bounded mathematical cross-check only; not proof of every certification budget or full application acceptance',
    'command': '.venv-emsolver/bin/python docs/evidence/inertia-review.py', 'records': records}
destination = Path(__file__).with_suffix('.json')
destination.write_text(json.dumps(evidence, indent=2, allow_nan=False) + '\n', encoding='utf-8')
print(json.dumps({'checks': evidence['checks'], 'passed': evidence['passed'], 'seed': seed}))
