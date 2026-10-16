#!/usr/bin/env python3
"""Measure paired local observation overhead using real, independently checked solves."""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import re
import statistics
import tempfile
import time

import numpy as np
from threadpoolctl import threadpool_limits

from emsolver.contracts import content_hash, resolve_config
from emsolver.integrity import file_hash
from emsolver.numerics import generate_sample, run_solver, verify_run
from emsolver.telemetry import SecondaryTelemetry


def run(matrix=None, config=None, *, repeats=7, n=1000):
    if type(repeats) is not int or not 1 <= repeats <= 30: raise ValueError('repeats must be 1..30')
    if type(n) is not int or not 20 <= n <= 200000: raise ValueError('sample n must be 20..200000')
    base = resolve_config(config or {'target': {'k': 4, 'sigma': .123},
                                     'resources': {'threads': 1, 'walltime_seconds': 120},
                                     'solver': {'subspace_max': 30, 'random_seed': 20261016}})
    if 'enabled' not in base['observability']:
        raise ValueError('This benchmark requires the observability.enabled configuration contract')
    numerical_config = copy.deepcopy(base)
    numerical_config['observability'].pop('enabled')
    runs, pairs = [], []
    with tempfile.TemporaryDirectory(prefix='emsolver-observability-') as folder:
        root = Path(folder)
        input_path = Path(matrix) if matrix is not None else root / 'sample.npz'
        if matrix is None:
            generate_sample(input_path, kind='indefinite', n=n, seed=base['solver']['random_seed'])
        matrix_hash = file_hash(input_path)
        for repeat in range(repeats):
            paired = {}
            # Alternate order to expose, rather than silently absorb, warm-cache
            # and scheduling effects. Every result directory is fresh.
            for enabled in ((False, True) if repeat % 2 == 0 else (True, False)):
                current = copy.deepcopy(base)
                current['observability']['enabled'] = enabled
                output = root / f'run-{repeat}-{int(enabled)}'
                journal = root / f'events-{repeat}-{int(enabled)}.jsonl'
                telemetry = SecondaryTelemetry(capacity=256, start_worker=False)
                event_count = 0
                def emit(event):
                    nonlocal event_count
                    event_count += 1
                    with journal.open('a', encoding='utf-8') as handle:
                        handle.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + '\n')
                    if enabled:
                        telemetry.emit(event)
                result = {}
                started = time.perf_counter()
                error_code = None
                with threadpool_limits(limits=current['resources']['threads']):
                    try:
                        result = run_solver(input_path, current, output, event_callback=emit)
                    except Exception as exc:
                        match = re.match(r'^([A-Z][A-Z_]{2,63})\b', str(exc))
                        error_code = match.group(1) if match else 'BENCHMARK_RUN_FAILED'
                elapsed = time.perf_counter() - started
                try:
                    validation = verify_run(input_path, output) if result else {}
                except Exception:
                    validation = {}
                residuals = result.get('residuals') or []
                snapshot = {'repeat': repeat + 1, 'order': len(paired) + 1, 'enabled': enabled,
                            'seconds': elapsed, 'status': result.get('status', 'FAILED'),
                            'error_code': error_code or result.get('error_code'),
                            'config_hash': content_hash(current), 'event_count': event_count,
                            'secondary': telemetry.counters(),
                            'correctness': {'numerical_status': (result.get('validation') or {}).get('numerical_status'),
                                            'target_certification': (result.get('validation') or {}).get('target_certification'),
                                            'n_accepted': result.get('n_accepted', 0),
                                            'residual_max': max(residuals) if residuals else None,
                                            'independent_numerical_status': validation.get('numerical_status'),
                                            'independent_target_certification': validation.get('target_certification')},
                            'observation_metrics': {key: value for key, value in (result.get('metrics') or {}).items()
                                                    if key in {'secondary_observation_enabled', 'events_suppressed', 'events_emitted', 'event_emission_seconds'}}}
                runs.append(snapshot)
                paired[enabled] = (snapshot, result)
                telemetry.close()
            before, after = paired[False], paired[True]
            left, right = before[1].get('eigenvalues') or [], after[1].get('eigenvalues') or []
            residual_left, residual_right = before[1].get('residuals') or [], after[1].get('residuals') or []
            equivalent = (bool(left) and len(left) == len(right) and len(residual_left) == len(residual_right) and
                          before[0]['status'] == after[0]['status'] and
                          all(before[0]['correctness'][key] == after[0]['correctness'][key] for key in
                              ('numerical_status', 'target_certification', 'n_accepted',
                               'independent_numerical_status', 'independent_target_certification')) and
                          bool(np.allclose(left, right, rtol=1e-12, atol=1e-14)) and
                          bool(np.allclose(residual_left, residual_right, rtol=1e-10, atol=1e-14)))
            pairs.append({'repeat': repeat + 1, 'numerically_equivalent': equivalent,
                          'enabled_to_disabled_ratio': after[0]['seconds'] / before[0]['seconds'],
                          'difference_seconds': after[0]['seconds'] - before[0]['seconds']})
        unchanged = file_hash(input_path) == matrix_hash
    enabled_times = [row['seconds'] for row in runs if row['enabled']]
    disabled_times = [row['seconds'] for row in runs if not row['enabled']]
    ratio = statistics.median(enabled_times) / statistics.median(disabled_times)
    return {'schema_version': '1.0', 'switch': 'observability.enabled', 'repeats': repeats,
            'input_sha256': matrix_hash, 'input_unchanged': unchanged,
            'numerical_config_hash': content_hash(numerical_config),
            'scope': 'Local paired real solver runs with alternating order, JSONL callbacks and a bounded secondary sample buffer; excludes HTTP, UI and external exporters. Not a performance guarantee.',
            'timing_scope': 'run_solver including local callback and numerical exports; independent verification is outside the timed region',
            'runs': runs, 'pairs': pairs,
            'summary': {'median_enabled_seconds': statistics.median(enabled_times),
                        'median_disabled_seconds': statistics.median(disabled_times),
                        'enabled_to_disabled_ratio': ratio, 'difference_percent': (ratio - 1) * 100,
                        'median_paired_ratio': statistics.median(row['enabled_to_disabled_ratio'] for row in pairs),
                        'all_pairs_numerically_equivalent': all(row['numerically_equivalent'] for row in pairs)}}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--matrix', type=Path)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--n', type=int, default=1000)
    parser.add_argument('--repeats', type=int, default=7)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    import yaml
    config = yaml.safe_load(args.config.read_text(encoding='utf-8')) if args.config else None
    result = run(args.matrix, config, repeats=args.repeats, n=args.n)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result['summary'], ensure_ascii=False, allow_nan=False))
