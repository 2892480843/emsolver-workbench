"""Portable numerical execution contract shared by server and remote nodes."""
from __future__ import annotations

import hashlib
from pathlib import Path

PROTOCOL = 'emsolver.remote.v1'
ARTIFACT_NAMES = frozenset({
    'manifest.json', 'config.resolved.yaml', 'eigenvalues.npy', 'eigenvectors.npy',
    'residuals.npy', 'validation.json', 'metrics.json', 'eigenvalues.csv',
    'eigenvectors.npz', 'events.jsonl', 'eigenvalues.scipy.npz',
    'eigenvectors.scipy.npz', 'environment.json', 'strategy.json',
    'memory-estimate.json', 'diagnostics.json', 'permutation.npy',
    'subspace.checkpoint.npz', 'exact-block.checkpoint.npz',
})
BUNDLE_NAMES = ARTIFACT_NAMES | {'worker.result.json'}


def execution_fingerprint():
    """Hash code that defines numerical execution, excluding UI and host paths."""
    root = Path(__file__).resolve().parent
    names = ['contracts.py', 'worker.py', 'optional_backends.py',
             'resource_budgets.py', 'process_environment.py',
             'validation_process.py', 'validation_worker.py', 'cluster_protocol.py']
    names.extend(path.relative_to(root).as_posix() for path in (root / 'numerics').rglob('*.py'))
    digest = hashlib.sha256()
    for name in sorted(set(names)):
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise RuntimeError('EXECUTION_SOURCE_UNAVAILABLE')
        label = name.encode('utf-8')
        digest.update(len(label).to_bytes(4, 'big') + label + hashlib.sha256(path.read_bytes()).digest())
    return {'protocol': PROTOCOL, 'source_sha256': digest.hexdigest()}
