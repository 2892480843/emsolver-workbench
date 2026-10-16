"""Freeze disjoint evaluation splits and execute every prescribed benchmark row."""
from __future__ import annotations

import copy
import html
import json
import os
from pathlib import Path
import re
import shutil
import time

from .contracts import content_hash, resolve_config
from .execution import execute_local
from .integrity import file_hash
from .provenance import capture_provenance
from .reporting import atomic_text, sanitize


BLOCKED_ABLATIONS = {'preconditioning': 'No independently switchable validated preconditioning ablation is implemented.',
                     'subspace_control': 'No independently switchable adaptive subspace-control ablation is implemented.',
                     'mixed_precision': 'No validated mixed-precision ablation is implemented.'}
TERMINAL = {'SUCCEEDED', 'PARTIAL', 'FAILED', 'TIMED_OUT', 'CANCELLED'}


def _merge(base, patch):
    if not isinstance(patch, dict): raise ValueError('INVALID_PROTOCOL_CONFIG')
    result = copy.deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _id(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 200 or any(ord(char) < 32 for char in value):
        raise ValueError('INVALID_PROTOCOL_LABEL')
    return value


def _repeats(value):
    if type(value) is not int or not 1 <= value <= 30: raise ValueError('INVALID_PROTOCOL_REPEATS')
    return value


def _identity(source_root):
    provenance = capture_provenance(source_root)
    environment = {key: provenance[key] for key in ('package', 'python', 'platform', 'dependencies', 'container')}
    package = Path(__file__).resolve().parent
    runtime_files = {path.relative_to(package).as_posix(): file_hash(path) for path in sorted(package.rglob('*.py'))
                     if path.is_file() and not path.is_symlink()}
    return {'source': provenance['source'], 'source_hash': content_hash(provenance['source']),
            'runtime_code_hash': content_hash(runtime_files), 'runtime_source_files': runtime_files,
            'environment': environment, 'environment_hash': content_hash(environment)}


def _fresh(directory):
    directory = Path(directory)
    if directory.exists() or directory.is_symlink(): raise ValueError('OUTPUT_EXISTS: immutable output directory required')
    directory.mkdir(parents=True, mode=0o700)
    return directory


def freeze_protocol(spec, output_dir, *, source_root=None):
    """Materialize inputs/configurations before evaluation; names are labels only."""
    if not isinstance(spec, dict) or set(spec) - {'schema_version', 'repeats', 'base_config', 'cases', 'variants'}:
        raise ValueError('INVALID_PROTOCOL_FIELDS')
    if spec.get('schema_version', '1.0') != '1.0': raise ValueError('INVALID_PROTOCOL_VERSION')
    cases = spec.get('cases')
    variants = spec.get('variants') or [{'id': 'baseline'}]
    repeats = _repeats(spec.get('repeats', 1))
    if not isinstance(cases, list) or not 2 <= len(cases) <= 100 or not isinstance(variants, list) or not 1 <= len(variants) <= 10:
        raise ValueError('INVALID_PROTOCOL_SIZE')
    if any(not isinstance(case, dict) for case in cases) or any(not isinstance(row, dict) for row in variants):
        raise ValueError('INVALID_PROTOCOL_ROW')
    if {case.get('split') for case in cases} != {'development', 'holdout'}:
        raise ValueError('EXPLICIT_DEVELOPMENT_AND_HOLDOUT_REQUIRED')
    if len({_id(case['id']) for case in cases}) != len(cases) or len({_id(row['id']) for row in variants}) != len(variants):
        raise ValueError('DUPLICATE_PROTOCOL_LABEL')
    from .numerics import capabilities, generate_sample, inspect_matrix, load_matrix
    cap = sanitize(capabilities())
    identity = _identity(source_root)
    directory = _fresh(output_dir)
    try:
        (directory / 'inputs').mkdir(mode=0o700)
        inputs, rows, split_hashes = [], [], {'development': set(), 'holdout': set()}
        for case_index, case in enumerate(cases):
            if set(case) - {'id', 'split', 'input', 'config', 'repeats', 'expected_status', 'expected_error_code'}:
                raise ValueError('INVALID_CASE_FIELDS')
            declaration = case.get('input')
            if not isinstance(declaration, dict): raise ValueError('INVALID_INPUT_DECLARATION')
            if set(declaration) == {'path'}:
                source = Path(declaration['path'])
                if source.is_symlink() or not source.is_file() or source.suffix.lower() not in {'.npz', '.npy'} or source.stat().st_size > 1024**3:
                    raise ValueError('UNSAFE_PROTOCOL_INPUT')
                path = directory / 'inputs' / f'{case_index:04d}{source.suffix.lower()}'
                shutil.copyfile(source, path)
                origin = {'type': 'provided', 'private_path_excluded': True}
            elif set(declaration) <= {'kind', 'n', 'seed'} and {'kind', 'n'} <= set(declaration):
                seed = declaration.get('seed', 20261016)
                if type(seed) is not int or not 0 <= seed <= 2**63 - 1: raise ValueError('INVALID_GENERATOR_SEED')
                path = directory / 'inputs' / f'{case_index:04d}.npz'
                metadata = generate_sample(path, kind=declaration['kind'], n=declaration['n'], seed=seed)
                origin = {'type': 'synthetic', 'license': 'CC0-1.0', **sanitize(metadata)}
            else:
                raise ValueError('INVALID_INPUT_DECLARATION')
            digest = file_hash(path)
            path.chmod(0o400)
            split_hashes[case['split']].add(digest)
            inputs.append({'case_id': case['id'], 'split': case['split'], 'path': str(path.relative_to(directory)),
                           'sha256': digest, 'bytes': path.stat().st_size, 'origin': origin})
            base = resolve_config(_merge(spec.get('base_config') or {}, case.get('config') or {}))
            structural_diagonal = False
            if any(variant.get('ablation') is not None for variant in variants):
                try:
                    structural_diagonal = inspect_matrix(load_matrix(
                        path, storage=base['problem']['storage'],
                        hermitian_rtol=base['problem']['hermitian_rtol']))['is_diagonal'] is True
                except ValueError:
                    # Invalid numeric inputs remain prescribed execution rows;
                    # they must fail visibly rather than leave the denominator.
                    pass
            case_repeats = _repeats(case.get('repeats', repeats))
            expected = case.get('expected_status', ['SUCCEEDED', 'PARTIAL'])
            if not isinstance(expected, list) or not expected or any(value not in TERMINAL for value in expected):
                raise ValueError('INVALID_EXPECTED_STATUS')
            for variant in variants:
                if set(variant) - {'id', 'config', 'ablation'}: raise ValueError('INVALID_VARIANT_FIELDS')
                ablation = variant.get('ablation')
                if ablation is not None and ablation not in {*BLOCKED_ABLATIONS, 'adaptive_shift'}:
                    raise ValueError('UNKNOWN_ABLATION')
                patch = variant.get('config') or {}
                meaning = None
                reason = BLOCKED_ABLATIONS.get(ablation)
                jd = cap.get('jacobi_davidson', {})
                is_jd = base['solver']['method'] == 'jacobi_davidson' and jd.get('supported') is True
                if ablation == 'adaptive_shift':
                    if base['solver']['method'] == 'jacobi_davidson':
                        reason = 'Not applicable: JD uses sigma as a Ritz target and has no adaptive working-shift retry.'
                    else:
                        isolated = {'solver': {'adaptive_shift': {'enabled': False}}}
                        patch = _merge(patch, isolated)
                        meaning = 'Disable bounded adaptive working-shift recovery; preserve the original target and hard budgets.'
                        if not base['solver']['adaptive_shift']['enabled']:
                            reason = 'Adaptive shift is already disabled in the baseline; no independent ablation remains.'
                elif ablation == 'subspace_control' and (
                        (base['solver']['method'] == 'block_shift_invert' and cap.get('block_shift_invert', {}).get('supported')) or
                        (is_jd and jd.get('bounded_restarts') is True)):
                    isolated = {'solver': {'block': {'max_restarts': 0}}}
                    patch = _merge(patch, isolated)
                    meaning = 'Disable bounded block restarts; preserve the subspace_max hard cap and all resource budgets.'
                    reason = None if base['solver']['block']['max_restarts'] > 0 else 'Block restarts are already disabled in the baseline.'
                elif ablation == 'preconditioning' and is_jd and {'none', 'jacobi'} <= set(jd.get('preconditioners', [])):
                    isolated = {'solver': {'linear': {'preconditioner': 'none'}}}
                    patch = _merge(patch, isolated)
                    meaning = 'Disable Jacobi preconditioning for the JD correction equation; preserve all acceptance and resource limits.'
                    reason = None if base['solver']['linear']['preconditioner'] == 'jacobi' else 'Jacobi preconditioning is not active in the baseline.'
                elif ablation == 'mixed_precision' and is_jd and jd.get('mixed_precision') is True:
                    isolated = {'solver': {'linear': {'precision': 'float64'}}}
                    patch = _merge(patch, isolated)
                    meaning = 'Disable FP32 approximate corrections; preserve FP64 original-problem verification and all hard budgets.'
                    reason = None if base['solver']['linear'].get('precision') == 'mixed_float32' else 'Mixed FP32 corrections are not active in the baseline.'
                config = resolve_config(_merge(base, patch))
                if meaning and config != resolve_config(_merge(base, isolated)):
                    raise ValueError('ABLATION_NOT_ISOLATED: a named ablation must change only its documented switch')
                if any(config[key] != base[key] for key in ('problem', 'target', 'validation', 'resources', 'output')):
                    raise ValueError('UNFAIR_VARIANT: input, target, accuracy, resources and output must match')
                if ablation is not None and structural_diagonal:
                    reason = ('Not applicable: structural_diagonal analytic selection does not use '
                              'adaptive shifts, preconditioning, mixed corrections or bounded iterative restarts.')
                backend = config['solver']['backend']
                if not reason and not cap.get(backend, {}).get('supported'):
                    reason = f'Requested backend {backend} is not validated by current capabilities.'
                methods = cap.get(backend, {}).get('methods', [])
                method = config['solver']['method']
                if not reason and method not in methods and method != 'auto':
                    reason = f'Requested method {method} is not validated by current capabilities.'
                if not reason and config['resources']['device'] != 'cpu':
                    reason = 'Requested device is not validated by the current CPU protocol runner.'
                for repeat in range(1, case_repeats + 1):
                    rows.append({'id': f'row-{len(rows):05d}', 'case_id': case['id'], 'split': case['split'],
                                 'variant_id': variant['id'], 'ablation': ablation, 'repeat': repeat,
                                 'ablation_meaning': meaning,
                                 'input_index': case_index, 'config': config, 'config_hash': content_hash(config),
                                 'status': 'BLOCKED' if reason else 'READY', 'blocked_reason': reason,
                                 'expected_status': expected, 'expected_error_code': case.get('expected_error_code')})
        if split_hashes['development'] & split_hashes['holdout']:
            raise ValueError('HOLDOUT_OVERLAP: identical input content occurs in both splits')
        if len(rows) > 500: raise ValueError('PROTOCOL_RUN_LIMIT: maximum 500 prescribed runs')
        plan = {'schema_version': '1.0', 'inputs': inputs, 'rows': rows, **identity,
                'capabilities': cap, 'prescribed_runs': len(rows),
                'split_policy': 'Explicit disjoint input content hashes; labels and paths do not select solver settings.'}
        plan['hash'] = content_hash(plan)
        path = atomic_text(directory / 'protocol.json', json.dumps(plan, ensure_ascii=False, allow_nan=False, indent=2))
        path.chmod(0o400)
        return plan
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise


def _load_plan(path, source_root, expected_hash):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 16 * 1024**2:
        raise ValueError('INVALID_PROTOCOL_FILE')
    plan = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(plan, dict) or plan.get('schema_version') != '1.0': raise ValueError('INVALID_PROTOCOL_SCHEMA')
    digest = plan.get('hash')
    if digest != content_hash({key: value for key, value in plan.items() if key != 'hash'}) or (expected_hash is not None and digest != expected_hash):
        raise ValueError('PROTOCOL_HASH_MISMATCH')
    identity = _identity(source_root)
    if identity['source_hash'] != plan.get('source_hash'): raise ValueError('PROTOCOL_SOURCE_CHANGED')
    if identity['runtime_code_hash'] != plan.get('runtime_code_hash'): raise ValueError('PROTOCOL_RUNTIME_CODE_CHANGED')
    if identity['environment_hash'] != plan.get('environment_hash'): raise ValueError('PROTOCOL_ENVIRONMENT_CHANGED')
    if plan.get('prescribed_runs') != len(plan.get('rows', [])) or not 1 <= len(plan['rows']) <= 500:
        raise ValueError('PROTOCOL_ROWS_MISMATCH')
    if len({row['id'] for row in plan['rows']}) != len(plan['rows']): raise ValueError('DUPLICATE_PROTOCOL_ROW')
    for item in plan['inputs']:
        relative = Path(item['path'])
        file = path.parent / relative
        if relative.is_absolute() or '..' in relative.parts or file.is_symlink() or not file.resolve().is_relative_to(path.parent.resolve()):
            raise ValueError('UNSAFE_PROTOCOL_INPUT')
        if not file.is_file() or file_hash(file) != item['sha256']: raise ValueError('PROTOCOL_INPUT_CHANGED')
    for row in plan['rows']:
        if (row['config_hash'] != content_hash(row['config']) or resolve_config(row['config']) != row['config'] or
                type(row['input_index']) is not int or not 0 <= row['input_index'] < len(plan['inputs'])):
            raise ValueError('PROTOCOL_CONFIG_CHANGED')
        item = plan['inputs'][row['input_index']]
        if row['case_id'] != item['case_id'] or row['split'] != item['split']: raise ValueError('PROTOCOL_ROWS_MISMATCH')
    return plan


def _summary(runs):
    counts = {status: sum(row['status'] == status for row in runs) for status in (*sorted(TERMINAL), 'BLOCKED', 'NOT_RUN')}
    prescribed = len(runs)
    executed = sum(counts[status] for status in TERMINAL)
    failures = counts['FAILED'] + counts['TIMED_OUT']
    return {'prescribed_runs': prescribed, 'executed_runs': executed, 'blocked_runs': counts['BLOCKED'],
            'failed_runs': counts['FAILED'], 'timeout_runs': counts['TIMED_OUT'], 'status_counts': counts,
            'failed_or_timeout_rate_prescribed': failures / prescribed if prescribed else None,
            'failed_or_timeout_rate_executed': failures / executed if executed else None,
            'expectations_met': sum(row.get('expectation_met') is True for row in runs)}


def execute_protocol(plan_path, output_dir, *, source_root=None, expected_plan_hash=None, executor=None):
    plan_path = Path(plan_path)
    plan = _load_plan(plan_path, source_root, expected_plan_hash)
    directory = _fresh(output_dir)
    runs = [{**row, 'status': 'BLOCKED' if row['status'] == 'BLOCKED' else 'NOT_RUN',
             'validation': {}, 'metrics': {}, 'reference_error': None, 'reference_certification': 'NOT_AVAILABLE'} for row in plan['rows']]
    report = {'schema_version': '1.0', 'protocol_hash': plan['hash'], 'source_hash': plan['source_hash'],
              'environment_hash': plan['environment_hash'], 'runs': runs, 'summary': _summary(runs)}
    def save():
        report['summary'] = _summary(runs)
        atomic_text(directory / 'results.json', json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2))
    atomic_text(directory / 'protocol.json', json.dumps(plan, ensure_ascii=False, allow_nan=False, indent=2))
    save()
    for index, row in enumerate(runs):
        if row['status'] == 'BLOCKED': continue
        matrix = plan_path.parent / plan['inputs'][row['input_index']]['path']
        started = time.monotonic()
        try:
            result = (executor or execute_local)(matrix, copy.deepcopy(row['config']), directory / f'run-{index:05d}')
            if not isinstance(result, dict) or result.get('status') not in TERMINAL:
                raise ValueError('INVALID_EXECUTOR_RESULT')
        except Exception as exc:
            found = re.match(r'^([A-Z][A-Z_]{2,63})\b', str(exc))
            result = {'status': 'FAILED', 'error_code': found.group(1) if found else 'PROTOCOL_EXECUTION_ERROR'}
        row.update(status=result['status'], error_code=result.get('error_code'),
                   validation=sanitize(result.get('validation') or {}), metrics=sanitize(result.get('metrics') or {}),
                   elapsed_seconds=time.monotonic() - started, n_accepted=result.get('n_accepted', 0),
                   residual_max=max(result.get('residuals') or [], default=None))
        row['expectation_met'] = row['status'] in row['expected_status']
        if row.get('expected_error_code'):
            row['expectation_met'] &= row.get('error_code') == row['expected_error_code']
        if row['status'] in {'SUCCEEDED', 'PARTIAL'}:
            row['expectation_met'] &= row['validation'].get('numerical_status') == 'PASSED'
        save()
    esc = lambda value: html.escape(str(value), quote=True)
    body = ''.join('<tr>' + ''.join(f'<td>{esc(value)}</td>' for value in
                   (row['id'], row['split'], row['case_id'], row['variant_id'], row['repeat'], row['status'],
                    row.get('residual_max'), row['metrics'].get('walltime_seconds', row.get('elapsed_seconds')),
                    row['metrics'].get('process_tree_peak_bytes'), row.get('blocked_reason') or row.get('error_code') or '')) + '</tr>' for row in runs)
    atomic_text(directory / 'report.html', '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>冻结评测报告</title>'
                '<h1>冻结评测报告</h1><p>全部规定运行均保留；BLOCKED 表示未实现的对照，不能作为成功样本。缺参考项未认证。</p>'
                '<pre>' + esc(json.dumps(report['summary'], ensure_ascii=False, indent=2)) + '</pre>'
                '<table><thead><tr><th>行</th><th>划分</th><th>标签</th><th>策略</th><th>重复</th><th>状态</th><th>残差</th><th>秒</th><th>峰值字节</th><th>说明</th></tr></thead><tbody>' + body + '</tbody></table></html>')
    return report
