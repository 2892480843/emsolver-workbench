#!/usr/bin/env python3
"""Compare bounded service-list changes using disposable metadata, never data/."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
from importlib import import_module
import json
from pathlib import Path
import platform
import sqlite3
import statistics
import sys
import tempfile
import time
from unittest.mock import patch

from emsolver.errors import ServiceError
from emsolver.service import Service


def legacy_matrices(service):
    return [service._public(item) for item in service.store.list('matrices') if service._visible(item)]


def legacy_configs(service):
    rows = []
    for item in service.store.list('configs'):
        matrix = service.store.get('matrices', item['config']['problem']['matrix_id'])
        if matrix and service._visible(matrix):
            rows.append(item)
    return rows


def legacy_imports(service, project_id):
    manager = service.import_manager
    with service._mutex, service.store.transaction():
        if project_id is not None:
            service._project(project_id)
        result = []
        for item in service.store.list('imports'):
            if project_id is not None and item['project_id'] != project_id:
                continue
            try:
                service._project(item['project_id'])
            except ServiceError as exc:
                if exc.code in {'FORBIDDEN', 'NOT_FOUND'}:
                    continue
                raise
            result.append(manager._public(item))
        return result


def run(matrices=2000, configs=4000, imports=2000, selected_imports=20, repeats=21):
    source_root = Path(__file__).resolve().parent.parent
    module_paths = {name: str(Path(import_module(name).__file__).resolve())
        for name in ('emsolver.api', 'emsolver.service', 'emsolver.imports', 'emsolver.store')}
    for name, path in module_paths.items():
        if Path(path) != source_root / (name.replace('.', '/') + '.py'):
            raise RuntimeError('Benchmark must load this workspace; run with PYTHONPATH=. ')
    with tempfile.TemporaryDirectory(prefix='emsolver-service-lists-') as directory:
        service = Service(directory, start_runner=False)
        try:
            with service.store.transaction():
                for identifier in ('p', 'other'):
                    service.store.put('projects', identifier, {'id': identifier, 'name': identifier,
                        'members': [], 'benchmark_kind': 'project'})
                for index in range(matrices):
                    service.store.put('matrices', f'm_{index}', {'id': f'm_{index}', 'project_id': 'p',
                        'name': str(index), 'benchmark_kind': 'matrix', '_path': '/unused/temporary-fixture',
                        'diagnostics': {'preview': [[point, point, 1.] for point in range(20)]}})
                references = min(matrices, 20)
                for index in range(configs):
                    service.store.put('configs', f'c_{index}', {'id': f'c_{index}', 'benchmark_kind': 'config',
                        'config': {'problem': {'matrix_id': f'm_{index % references}'}}})
                for index in range(imports):
                    service.store.put('imports', f'i_{index}', {'id': f'i_{index}', 'benchmark_kind': 'import',
                        'project_id': 'p' if index < selected_imports else 'other', 'status': 'SUCCEEDED',
                        'metadata': {'summary': 'x' * 1024}, '_identity': {'subject': 'local-user'}})
            functions = {
                'matrices_legacy': lambda: legacy_matrices(service),
                'matrices_current': service.list_matrices,
                'configs_legacy': lambda: legacy_configs(service),
                'configs_current': service.list_configs,
                'imports_legacy': lambda: legacy_imports(service, 'p'),
                'imports_current': lambda: service.import_manager.list('p'),
            }
            equality, result_hashes = {}, {}
            for group in ('matrices', 'configs', 'imports'):
                before, after = functions[group + '_legacy'](), functions[group + '_current']()
                assert before == after
                equality[group] = True
                result_hashes[group] = hashlib.sha256(json.dumps(after, sort_keys=True,
                    separators=(',', ':')).encode()).hexdigest()
            # Rotate all paths each round, retaining raw samples and GC's normal
            # operation. SQL/decoding instrumentation is outside timed runs.
            names = list(functions)
            for function in functions.values():
                function()
            samples = {name: [] for name in names}
            for iteration in range(repeats):
                order = names[iteration % len(names):] + names[:iteration % len(names)]
                for name in order:
                    start = time.perf_counter_ns()
                    functions[name]()
                    samples[name].append((time.perf_counter_ns() - start) / 1e6)
            decodes, statements, row_counts = {}, {}, {}
            original_loads = json.loads
            for name, function in functions.items():
                observed = Counter()

                def track(raw, *args, **kwargs):
                    value = original_loads(raw, *args, **kwargs)
                    observed['total'] += 1
                    if isinstance(value, dict):
                        observed[value.get('benchmark_kind', 'other')] += 1
                    return value

                with patch('emsolver.store.json.loads', track):
                    row_counts[name] = len(function())
                decodes[name] = dict(observed)
                traced = []
                service.store.db.set_trace_callback(traced.append)
                try:
                    function()
                finally:
                    service.store.db.set_trace_callback(None)
                statements[name] = sum(query.lstrip().upper().startswith('SELECT') for query in traced)
            plan = [row[3] for row in service.store.db.execute(
                'EXPLAIN QUERY PLAN SELECT body FROM entities WHERE kind=? AND project_id=? ORDER BY seq',
                ('imports', 'p'))]
            source_hashes = {name: hashlib.sha256((source_root / name).read_bytes()).hexdigest()
                for name in ('emsolver/api.py', 'emsolver/service.py', 'emsolver/imports.py', 'emsolver/store.py')}
            return {
                'scope': 'Local synthetic warmed-cache service metadata diagnostic; no numerical computation.',
                'protocol': 'One warmup per path; rotating order across interleaved rounds; normal GC; raw samples retained.',
                'fixtures': {'matrices': matrices, 'configs': configs, 'distinct_config_matrices': references,
                    'imports': imports, 'selected_imports': selected_imports, 'projects': 2},
                'repeats': repeats, 'outputs_equal': equality, 'result_sha256': result_hashes,
                'environment': {'platform': platform.platform(), 'machine': platform.machine(),
                    'python': sys.version.split()[0], 'sqlite': sqlite3.sqlite_version,
                    'loaded_module_paths': module_paths},
                'source_sha256': source_hashes, 'samples_ms': samples,
                'median_ms': {name: statistics.median(values) for name, values in samples.items()},
                'range_ms': {name: [min(values), max(values)] for name, values in samples.items()},
                'returned_rows': row_counts, 'decoded_json_records': decodes,
                'select_statements': statements, 'scoped_import_query_plan': plan,
                'limitations': ['All fixture data is disposable; no application database or release archive is opened.',
                    'Elapsed time varies with document size, references, project distribution, GC and competing machine load.',
                    'Query and JSON decode bounds are deterministic for these fixtures; no fixed production speedup is guaranteed.',
                    'List response sizes and full-list compatibility remain unchanged; this is not a pagination redesign.',
                    'Authorization memoization is local to one list call and current identity; membership edits are observed on the next call.']}
        finally:
            service.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('artifacts/performance-20261016/service-benchmark.json'))
    parser.add_argument('--matrices', type=int, default=2000)
    parser.add_argument('--configs', type=int, default=4000)
    parser.add_argument('--imports', type=int, default=2000)
    parser.add_argument('--selected-imports', type=int, default=20)
    parser.add_argument('--repeats', type=int, default=21)
    args = parser.parse_args()
    if (not all(1 <= value <= 100000 for value in (args.matrices, args.configs, args.imports)) or
            not 0 <= args.selected_imports <= args.imports or not 3 <= args.repeats <= 100):
        parser.error('Fixture counts must be 1..100000; selected imports 0..imports; repeats 3..100')
    result = run(args.matrices, args.configs, args.imports, args.selected_imports, args.repeats)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'output': str(args.output), 'median_ms': result['median_ms'],
        'select_statements': result['select_statements'], 'outputs_equal': result['outputs_equal']}, indent=2))


if __name__ == '__main__':
    main()
