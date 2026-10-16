"""Validate one numeric matrix in an isolated, monitored process."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re

from .integrity import file_hash


MAX_INPUT_BYTES = 1024**3


def _write_result(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
            json.dump(value, output, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--spec', required=True, type=Path)
    parser.add_argument('--result', required=True, type=Path)
    args = parser.parse_args(argv)
    result = {'schema_version': '1.0', 'status': 'FAILED', 'error_code': 'INVALID_IMPORT_SPEC'}
    try:
        if args.spec.is_symlink() or args.spec.stat().st_size > 16384:
            raise ValueError('INVALID_IMPORT_SPEC')
        spec = json.loads(args.spec.read_text(encoding='utf-8'))
        if (not isinstance(spec, dict) or set(spec) != {'schema_version', 'input_path', 'storage', 'input_sha256'} or
                spec['schema_version'] != '1.0' or spec['storage'] not in {'full', 'upper', 'lower'} or
                not re.fullmatch(r'[0-9a-f]{64}', spec['input_sha256'])):
            raise ValueError('INVALID_IMPORT_SPEC')
        path = Path(spec['input_path'])
        if (path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_INPUT_BYTES or
                path.parent.resolve() != args.spec.parent.resolve() or path.suffix not in {'.npz', '.npy'}):
            raise ValueError('UNSAFE_IMPORT_INPUT')
        if file_hash(path) != spec['input_sha256']:
            raise ValueError('IMPORT_INPUT_CHANGED')
        from threadpoolctl import threadpool_limits
        from .numerics import load_matrix, inspect_matrix
        with threadpool_limits(limits=1):
            matrix = load_matrix(path, storage=spec['storage'], max_bytes=MAX_INPUT_BYTES)
            diagnostic = inspect_matrix(matrix)
        if file_hash(path) != spec['input_sha256']:
            raise ValueError('IMPORT_INPUT_CHANGED')
        result = {'schema_version': '1.0', 'status': 'SUCCEEDED', 'input_sha256': spec['input_sha256'],
                  'diagnostic': diagnostic}
    except BaseException as exc:
        found = re.match(r'^([A-Z][A-Z_]{2,63})\b', str(exc))
        result = {'schema_version': '1.0', 'status': 'FAILED',
                  'error_code': found.group(1) if found else 'INVALID_MATRIX'}
    try:
        if args.result.parent.resolve() != args.spec.parent.resolve() or args.result.is_symlink():
            return 1
        _write_result(args.result, result)
    except (OSError, ValueError):
        return 1
    return 0 if result['status'] == 'SUCCEEDED' else 1


if __name__ == '__main__':
    raise SystemExit(main())
