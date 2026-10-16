"""Resource-monitored independent-reference validation and comparison worker."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile

import numpy as np
from threadpoolctl import threadpool_limits

from .industrial_reference import (MAX_WORKER_BYTES, candidate_metadata, header_metadata)
from .integrity import file_hash


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2: return 1
    spec, output = map(Path, args)
    temporary = output.with_suffix('.tmp')
    try:
        if spec.is_symlink() or output.exists() or spec.stat().st_size > MAX_WORKER_BYTES or spec.parent.resolve() != output.parent.resolve(): return 1
        value = json.loads(spec.read_text(encoding='utf-8'))
        if set(value) != {'operation', 'source', 'sha256', 'candidate', 'criteria'}: return 1
        source = Path(value['source'])
        metadata = header_metadata(source)
        if file_hash(source) != value['sha256']: return 1
        with threadpool_limits(limits=1), np.load(source, allow_pickle=False) as arrays:
            eigenvalues, eigenvectors = arrays['eigenvalues'], arrays['eigenvectors']
            if not np.isfinite(eigenvalues).all() or not np.isfinite(eigenvectors).all(): return 1
            if value['operation'] == 'validate':
                result = metadata
            elif value['operation'] == 'compare':
                from .evaluation import evaluate_results
                candidate = Path(value['candidate'])
                candidate_metadata(candidate)
                with tempfile.TemporaryDirectory(prefix='arrays-', dir=spec.parent) as target:
                    directory = Path(target)
                    np.save(directory / 'eigenvalues.npy', np.asarray(eigenvalues, dtype=np.float64))
                    scalar = np.complex128 if np.iscomplexobj(eigenvectors) else np.float64
                    np.save(directory / 'eigenvectors.npy', np.asarray(eigenvectors, dtype=scalar))
                    criteria = value['criteria']
                    result = evaluate_results(candidate, directory, criteria['max_eigenvalue_absolute_error'],
                        criteria['max_eigenvalue_relative_error'], criteria['max_subspace_sine'])
                    actual = np.load(candidate / 'eigenvalues.npy', allow_pickle=False)
                    result.update(reference_eigenvalues=np.asarray(eigenvalues, dtype=np.float64).tolist(),
                                  candidate_eigenvalues=np.asarray(actual, dtype=np.float64).tolist())
            else: return 1
        if file_hash(source) != value['sha256']: return 1
        raw = json.dumps({'schema_version': '1.0', 'sha256': value['sha256'], 'operation': value['operation'],
                          'result': result}, allow_nan=False).encode('utf-8')
        if len(raw) > MAX_WORKER_BYTES: return 1
        with temporary.open('xb') as handle: handle.write(raw)
        temporary.replace(output)
        return 0
    except BaseException:
        return 1
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == '__main__':
    raise SystemExit(main())
