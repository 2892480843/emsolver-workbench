#!/usr/bin/env python3
"""Migrate EMSolver metadata without modifying the SQLite source."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

# Support running the source checkout script without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from emsolver.postgres_store import migrate_sqlite_to_postgres


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path, help='Existing SQLite metadata file, opened read-only')
    parser.add_argument('--schema', default=os.environ.get('EMSOLVER_DATABASE_SCHEMA', 'emsolver'), help='Dedicated empty PostgreSQL schema')
    parser.add_argument('--dry-run', action='store_true', help='Validate source and destination without writing metadata')
    parser.add_argument('--report', type=Path, help='Write the JSON result report to this new file')
    arguments = parser.parse_args(argv)
    dsn = os.environ.get('EMSOLVER_DATABASE_URL', '').strip()
    if not dsn:
        parser.error('Set EMSOLVER_DATABASE_URL; credentials are accepted only through the environment')
    if arguments.report and arguments.report.exists():
        parser.error('Report path already exists; choose a new report file')
    migration_completed = False
    try:
        report = migrate_sqlite_to_postgres(arguments.source, dsn, schema=arguments.schema, dry_run=arguments.dry_run)
        migration_completed = True
        serialized = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
        if arguments.report:
            with arguments.report.open('x', encoding='utf-8') as output:
                output.write(serialized)
        print(serialized, end='')
    except Exception as exc:
        if migration_completed:
            result = 'Preflight completed' if arguments.dry_run else 'Migration completed'
            print(result + '; result report could not be written. Do not repeat the migration into this destination.', file=sys.stderr)
            return 1
        # Database diagnostics can contain connection details or row data.
        # Only validation messages written by this module are safe to expose.
        if isinstance(exc, (ValueError, RuntimeError)):
            print('Migration failed: ' + str(exc), file=sys.stderr)
        else:
            print('Migration failed; source preserved and destination transaction rolled back', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
