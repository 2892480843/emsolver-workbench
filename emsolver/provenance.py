"""Portable source/environment evidence without private host paths or names."""
from __future__ import annotations

import hashlib
from importlib import metadata
import os
from pathlib import Path
import platform
import re
import subprocess
import sys

from . import __version__


DEPENDENCIES = ('numpy', 'scipy', 'fastapi', 'uvicorn', 'pydantic', 'python-multipart', 'psutil', 'PyYAML', 'threadpoolctl')


def _version(name):
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def _git(root):
    def run(arguments):
        try:
            result = subprocess.run(['git', '-C', str(root), *arguments], capture_output=True,
                                    text=True, timeout=1, check=False)
        except (OSError, subprocess.TimeoutExpired, UnicodeError):
            return None
        return result.stdout.strip() if result.returncode == 0 else None
    commit = run(['rev-parse', '--verify', 'HEAD'])
    if not commit or not re.fullmatch(r'[a-fA-F0-9]{40,64}', commit):
        return None, None
    status = run(['status', '--porcelain', '--untracked-files=no'])
    return commit, bool(status) if status is not None else None


def _source(root):
    files = []
    for folder, patterns in (('emsolver', ('*.py',)), ('web/src', ('*.ts', '*.tsx', '*.css'))):
        directory = root / folder
        if directory.is_symlink() or not directory.is_dir():
            continue
        for pattern in patterns:
            for path in directory.rglob(pattern):
                if path.is_file() and not path.is_symlink() and not any(parent.is_symlink() for parent in path.parents if parent != root):
                    files.append(path)
    for name in ('pyproject.toml', 'requirements.lock', 'web/package.json', 'web/package-lock.json'):
        path = root / name
        if path.is_file() and not path.is_symlink():
            files.append(path)
    digest = hashlib.sha256()
    count, total, truncated = 0, 0, False
    for path in sorted(set(files)):
        try:
            size = path.stat().st_size
            if size > 8 * 1024**2 or total + size > 32 * 1024**2:
                truncated = True
                continue
            raw = path.read_bytes()
        except OSError:
            truncated = True
            continue
        # Include a length-delimited relative filename and the file content hash.
        relative = path.relative_to(root).as_posix().encode('utf-8')
        digest.update(len(relative).to_bytes(4, 'big') + relative + hashlib.sha256(raw).digest())
        count += 1
        total += len(raw)
    commit, dirty = _git(root)
    return {'tree_sha256': digest.hexdigest() if count else None, 'file_count': count,
            'scope': 'emsolver/*.py recursively; web/src TS/TSX/CSS; Python and web dependency manifests',
            'complete': not truncated, 'git_commit': commit, 'git_dirty': dirty}


def reproducible_commands():
    return {'solve': 'emsolver solve --matrix INPUT.npz --config run/config.resolved.yaml --output rerun',
            'validate': 'emsolver validate --matrix INPUT.npz --run rerun',
            'input_requirement': 'Supply the authorized input matching the frozen matrix hash; INPUT.npz is a portable placeholder.'}


def capture_provenance(source_root=None):
    """Unknown image/git facts remain null; no external service is contacted."""
    root = Path(source_root) if source_root is not None else Path(__file__).resolve().parent.parent
    root = root.resolve()
    image = os.environ.get('EMSOLVER_IMAGE_DIGEST', '')
    valid_image = bool(re.fullmatch(r'sha256:[a-fA-F0-9]{64}', image))
    return {'schema_version': '1.0', 'package': {'name': 'emsolver-workbench', 'version': __version__},
            'python': {'version': platform.python_version(), 'implementation': platform.python_implementation()},
            'platform': {'system': platform.system(), 'release': platform.release(), 'machine': platform.machine(),
                         'python_platform': sys.platform, 'cpu_count': os.cpu_count()},
            'dependencies': {name: _version(name) for name in DEPENDENCIES}, 'source': _source(root),
            'container': {'status': 'declared' if valid_image else 'unknown',
                          'image_digest': image if valid_image else None,
                          'evidence': 'EMSOLVER_IMAGE_DIGEST supplied by operator; not independently attested' if valid_image else 'No verifiable image digest supplied'},
            'reproducible_commands': reproducible_commands()}
