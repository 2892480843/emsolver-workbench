#!/usr/bin/env python3
"""Freeze or execute an explicit development/holdout benchmark protocol."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

from emsolver.protocols import freeze_protocol, execute_protocol


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    freeze = commands.add_parser('freeze')
    freeze.add_argument('--spec', required=True, type=Path)
    freeze.add_argument('--output', required=True, type=Path)
    freeze.add_argument('--source-root', type=Path)
    execute = commands.add_parser('execute')
    execute.add_argument('--plan', required=True, type=Path)
    execute.add_argument('--output', required=True, type=Path)
    execute.add_argument('--source-root', type=Path)
    execute.add_argument('--expected-hash')
    args = parser.parse_args(argv)
    if args.command == 'freeze':
        if args.spec.stat().st_size > 1024**2: parser.error('spec exceeds 1 MiB')
        result = freeze_protocol(yaml.safe_load(args.spec.read_text(encoding='utf-8')), args.output, source_root=args.source_root)
        print(json.dumps({'protocol_hash': result['hash'], 'prescribed_runs': result['prescribed_runs']}))
        return 0
    result = execute_protocol(args.plan, args.output, source_root=args.source_root, expected_plan_hash=args.expected_hash)
    print(json.dumps(result['summary'], ensure_ascii=False))
    return 0 if all(row.get('expectation_met') is True for row in result['runs']) else 2


if __name__ == '__main__':
    raise SystemExit(main())
