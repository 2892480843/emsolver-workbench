"""Read-only original-problem verification for the bounded validation parent."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re

from .validation_process import MAX_JSON_BYTES, _pairs, _invalid_constant, _strict_value


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--spec', type=Path, required=True)
    parser.add_argument('--result', type=Path, required=True)
    args = parser.parse_args(argv)
    temporary = args.result.with_suffix(args.result.suffix + '.tmp')
    try:
        if (args.spec.is_symlink() or args.result.is_symlink() or args.spec.parent.resolve() != args.result.parent.resolve() or
                not re.fullmatch(r'\.verify-[0-9a-f]{32}\.spec\.json', args.spec.name) or
                args.result.name != args.spec.name.removesuffix('.spec.json') + '.result.json' or
                args.spec.stat().st_size > MAX_JSON_BYTES):
            return 1
        spec = json.loads(args.spec.read_bytes(), object_pairs_hook=_pairs, parse_constant=_invalid_constant)
        if not isinstance(spec, dict) or set(spec) != {'schema_version', 'input_path', 'run_dir', 'config'} or spec['schema_version'] != '1.0':
            return 1
        directory, source = Path(spec['run_dir']), Path(spec['input_path'])
        if directory.is_symlink() or source.is_symlink() or directory.parent.resolve() != args.spec.parent.resolve(): return 1
        from .contracts import resolve_config
        config = resolve_config(spec['config'])
        from threadpoolctl import threadpool_limits
        from .numerics import verify_run
        with threadpool_limits(limits=config['resources']['threads']):
            validation = verify_run(source, directory, config)
        if not isinstance(validation, dict): return 1
        if validation.get('error_code') and not re.fullmatch(r'[A-Z][A-Z_]{2,63}', str(validation['error_code'])):
            validation['error_code'] = 'VALIDATION_FAILED'
        _strict_value(validation)
        raw = json.dumps({'schema_version': '1.0', 'validation': validation}, ensure_ascii=False, allow_nan=False).encode('utf-8')
        if len(raw) > MAX_JSON_BYTES: return 1
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'wb') as output: output.write(raw)
        temporary.replace(args.result)
        return 0
    except BaseException:
        return 1
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == '__main__':
    raise SystemExit(main())
