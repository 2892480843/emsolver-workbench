"""Bounded independent post-crash validation, separate from the web process."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

import psutil

from .contracts import resolve_config


MAX_JSON_BYTES = 1024**2
MAX_MEMORY_BYTES = 2 * 1024**3
MAX_WALLTIME_SECONDS = 30
POLL_SECONDS = .02


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result: raise ValueError('Duplicate validation field')
        result[key] = value
    return result


def _invalid_constant(_):
    raise ValueError('Nonfinite validation data')


def _strict_value(value, depth=0):
    if depth > 8: raise ValueError('Validation nesting limit')
    if isinstance(value, dict):
        if len(value) > 128 or any(not isinstance(key, str) or len(key) > 128 for key in value):
            raise ValueError('Validation object limit')
        for child in value.values(): _strict_value(child, depth + 1)
    elif isinstance(value, list):
        if len(value) > 4096: raise ValueError('Validation list limit')
        for child in value: _strict_value(child, depth + 1)
    elif type(value) in (float, int):
        if not math.isfinite(value): raise ValueError('Nonfinite validation number')
    elif isinstance(value, str):
        if len(value) > 4096: raise ValueError('Validation text limit')
    elif value is not None and type(value) is not bool:
        raise ValueError('Invalid validation value')


def _terminate(process):
    """Only handle the owned child and its current descendants."""
    try:
        parent = psutil.Process(process.pid)
        children = parent.children(recursive=True)
        for child in reversed(children):
            try: child.terminate()
            except psutil.NoSuchProcess: pass
        try: parent.terminate()
        except psutil.NoSuchProcess: pass
        _, alive = psutil.wait_procs(children + [parent], timeout=.3)
        for child in alive:
            try: child.kill()
            except psutil.NoSuchProcess: pass
    except psutil.Error:
        if process.poll() is None:
            process.kill()
    try: process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def verify_bounded(input_path, run_dir, config):
    """Return actual validation or None on infrastructure/budget/schema failure.

    Verification has its own conservative 2 GiB ceiling and at most 30 seconds,
    additionally limited by the original job's resource limits. Published run
    contents are read only; all UUID controls are siblings of the artifact dir.
    """
    process = None
    controls = []
    try:
        config = resolve_config(config)
        source, directory = Path(input_path), Path(run_dir)
        if source.is_symlink() or directory.is_symlink(): return None
        source, directory = source.resolve(), directory.resolve()
        if (not source.is_file() or source.suffix.lower() not in {'.npz', '.npy'} or source.stat().st_size > 1024**3 or
                not directory.is_dir() or any(path.is_symlink() for path in directory.iterdir())):
            return None
        parent = directory.parent
        token = '.verify-' + uuid.uuid4().hex
        spec_path, result_path, temporary = [parent / (token + suffix) for suffix in ('.spec.json', '.result.json', '.result.json.tmp')]
        controls = [spec_path, result_path, temporary]
        raw = json.dumps({'schema_version': '1.0', 'input_path': str(source), 'run_dir': str(directory),
                          'config': config}, ensure_ascii=False, allow_nan=False).encode('utf-8')
        control_limit = min(config['resources']['disk_limit_gib'] * 1024**3, 2 * MAX_JSON_BYTES + 4096)
        if len(raw) > MAX_JSON_BYTES or len(raw) > control_limit: return None
        descriptor = os.open(spec_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'wb') as output: output.write(raw)
        # An allowlist avoids inheriting service tokens, cloud credentials,
        # model settings, custom import hooks or reference-directory hints.
        environment = {key: os.environ[key] for key in ('PATH', 'LANG', 'LC_ALL', 'TMPDIR', 'SYSTEMROOT', 'WINDIR') if key in os.environ}
        environment.update(PYTHONPATH=str(Path(__file__).resolve().parent.parent), PYTHONNOUSERSITE='1')
        threads = str(config['resources']['threads'])
        for key in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS', 'VECLIB_MAXIMUM_THREADS'):
            environment[key] = threads
        deadline = time.monotonic() + min(config['resources']['walltime_seconds'], MAX_WALLTIME_SECONDS)
        memory_limit = min(config['resources']['memory_limit_gib'] * 1024**3, MAX_MEMORY_BYTES)
        process = subprocess.Popen([sys.executable, '-m', 'emsolver.validation_worker', '--spec', str(spec_path),
                                    '--result', str(result_path)], cwd=Path(__file__).resolve().parent.parent,
                                   env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   start_new_session=True)
        while process.poll() is None:
            if time.monotonic() >= deadline: return None
            try:
                parent_process = psutil.Process(process.pid)
                rss = 0
                for item in [parent_process, *parent_process.children(recursive=True)]:
                    try: rss += item.memory_info().rss
                    except psutil.NoSuchProcess: pass
                if rss > memory_limit: return None
            except psutil.NoSuchProcess:
                pass
            except psutil.Error:
                return None
            disk = sum(path.stat().st_size for path in controls if path.is_file())
            if disk > control_limit: return None
            time.sleep(min(POLL_SECONDS, max(0, deadline - time.monotonic())))
        if process.returncode != 0 or time.monotonic() >= deadline: return None
        if sum(path.stat().st_size for path in controls if path.is_file()) > control_limit: return None
        if result_path.is_symlink() or not result_path.is_file() or result_path.stat().st_size > MAX_JSON_BYTES: return None
        with result_path.open('rb') as input_file: raw = input_file.read(MAX_JSON_BYTES + 1)
        if len(raw) > MAX_JSON_BYTES: return None
        value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_invalid_constant)
        if not isinstance(value, dict) or set(value) != {'schema_version', 'validation'} or value['schema_version'] != '1.0': return None
        validation = value['validation']
        if not isinstance(validation, dict): return None
        _strict_value(validation)
        if validation.get('numerical_status') not in {'PASSED', 'FAILED'} or validation.get('target_certification') not in {'CERTIFIED', 'UNVERIFIED'}:
            return None
        return validation
    except (OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError, subprocess.SubprocessError):
        return None
    finally:
        try:
            if process is not None and process.poll() is None:
                _terminate(process)
        except (OSError, psutil.Error, subprocess.SubprocessError):
            pass
        finally:
            for path in controls:
                try: path.unlink(missing_ok=True)
                except OSError: pass
