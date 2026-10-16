#!/usr/bin/env python3
"""Check a source delivery without treating optional capabilities as completed."""
from __future__ import annotations

import argparse
import ast
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import tomllib
from urllib.parse import urlsplit

import yaml

from emsolver.contracts import resolve_config


REQUIRED = {
    'license': ['LICENSE', 'NOTICE.md'],
    'installation': ['README.md', 'pyproject.toml', 'requirements.lock', 'scripts/start.sh', 'scripts/prepare_offline.sh'],
    'core': ['emsolver/numerics/core.py', 'emsolver/contracts.py', 'emsolver/execution.py', 'emsolver/cli.py', 'emsolver/jobs.py', 'emsolver/events.py', 'emsolver/backup.py', 'emsolver/experiments.py', 'emsolver/advice.py', 'emsolver/reporting.py', 'emsolver/protocols.py'],
    'application': ['emsolver/api.py', 'emsolver/service.py', 'emsolver/store.py', 'web/package-lock.json', 'web/src/App.tsx'],
    'example': ['examples/default.yaml', 'examples/interval.yaml', 'benchmarks/required.yaml', 'scripts/demo.sh', 'scripts/run_protocol.py'],
    'verification': ['tests/test_numerics.py', 'tests/test_service_core.py', 'tests/test_api_integration.py', 'tests/test_cli_integration.py', 'tests/test_store.py', 'tests/test_architecture_service.py', 'tests/test_experiments.py', 'tests/test_experiment_integration.py'],
    'traceability': ['docs/feature-coverage.json', 'docs/numerical-implementation.md', 'docs/backend-implementation.md', 'docs/dependency-inventory.json', 'docs/datasets.md'],
}


def _regular(root, name):
    path = root / name
    try:
        return path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(root.resolve()) and path.stat().st_size > 0
    except OSError:
        return False


def _tree(root, name):
    if not _regular(root, name): raise ValueError('Missing source')
    text = (root / name).read_text(encoding='utf-8')
    if text.startswith('\ufeff'): raise ValueError('UTF-8 BOM is not accepted')
    return ast.parse(text, filename=name)


def _implemented_function(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return any(isinstance(child, (ast.Call, ast.If, ast.For, ast.While, ast.With, ast.Try)) for child in ast.walk(node))
    return False


def _evidence(root, value):
    if not isinstance(value, str): return False
    parts = value.split('::')
    relative = Path(parts[0])
    if relative.is_absolute() or '..' in relative.parts or not _regular(root, parts[0]): return False
    if len(parts) > 1:
        try:
            tree = _tree(root, parts[0])
            symbols = {node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
            return all(symbol.split('[', 1)[0] in symbols for symbol in parts[1:])
        except (ValueError, SyntaxError, UnicodeError): return False
    return True


class _AssetReferences(HTMLParser):
    def __init__(self):
        super().__init__()
        self.references = []
    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if value and ((tag == 'script' and key == 'src') or (tag == 'link' and key == 'href') or
                          (tag in {'img', 'source'} and key == 'src')):
                self.references.append(value)


def _built_assets(root):
    dist = root / 'web' / 'dist'
    index = dist / 'index.html'
    if not _regular(root, 'web/dist/index.html'): return False
    parser = _AssetReferences()
    parser.feed(index.read_text(encoding='utf-8'))
    local = []
    for reference in parser.references:
        parsed = urlsplit(reference)
        if parsed.scheme or parsed.netloc: continue
        path = dist / parsed.path.lstrip('/')
        if not path.is_file() or path.stat().st_size == 0 or path.is_symlink() or not path.resolve().is_relative_to(dist.resolve()): return False
        local.append(path)
    if not any(path.suffix == '.js' for path in local): return False
    for path in local:
        if path.suffix != '.css': continue
        for reference in re.findall(r'url\(\s*[\'\"]?([^\)\'\"]+)', path.read_text(encoding='utf-8')):
            parsed = urlsplit(reference)
            if parsed.scheme or parsed.netloc: continue
            asset = dist / parsed.path.lstrip('/') if parsed.path.startswith('/') else path.parent / parsed.path
            if not asset.is_file() or not asset.resolve().is_relative_to(dist.resolve()): return False
    return True


def _sample_smoke(root):
    try:
        if not _implemented_function(_tree(root, 'emsolver/numerics/core.py'), 'generate_sample'): return False
        environment = os.environ.copy()
        environment['PYTHONPATH'] = str(root.resolve())
        with tempfile.TemporaryDirectory(prefix='emsolver-delivery-') as folder:
            sample = Path(folder) / 'sample.npz'
            for args in (['sample', '--kind', 'indefinite', '--n', '20', '--output', str(sample), '--json'],
                         ['inspect', '--matrix', str(sample), '--json'],
                         ['solve', '--matrix', str(sample), '--config', str((root / 'examples/default.yaml').resolve()),
                          '--output', str(Path(folder) / 'run'), '--json']):
                result = subprocess.run([sys.executable, '-m', 'emsolver.cli', *args], cwd=root,
                                        env=environment, capture_output=True, text=True, timeout=20)
                if result.returncode not in ({0, 4} if args[0] == 'solve' else {0}): return False
                value = json.loads(result.stdout)
                if not isinstance(value, dict): return False
                if args[0] == 'solve' and value.get('validation', {}).get('numerical_status') != 'PASSED': return False
            return sample.is_file() and sample.stat().st_size > 0
    except (OSError, ValueError, SyntaxError, subprocess.TimeoutExpired):
        return False


def _inventory(root, project):
    rows = json.loads((root / 'docs/dependency-inventory.json').read_text(encoding='utf-8'))
    if not isinstance(rows, list) or not rows: return False
    normalize = lambda value: re.sub(r'[-_.]+', '-', value).lower()
    index = {}
    for row in rows:
        name, version = row.get('name'), row.get('version')
        license_info = row.get('license_expression') or row.get('license_metadata')
        urls = [row.get('home_page')] + row.get('project_urls', [])
        valid_urls = [urlsplit(url.split(', ', 1)[-1]) for url in urls if isinstance(url, str)]
        if (not isinstance(name, str) or not name or normalize(name) in index or
                not isinstance(version, str) or not re.fullmatch(r'\d[^\s]*', version) or
                not isinstance(license_info, str) or not license_info.strip() or license_info.strip().lower() in {'unknown', 'todo', 'none'} or
                not any(url.scheme in {'https', 'http'} and url.netloc for url in valid_urls)):
            return False
        index[normalize(name)] = row
    locked = {}
    for line in (root / 'requirements.lock').read_text(encoding='utf-8').splitlines():
        match = re.fullmatch(r'([A-Za-z0-9_.-]+)==([^\s;]+)', line.strip())
        if match: locked[normalize(match.group(1))] = match.group(2)
    for requirement in project['project']['dependencies']:
        match = re.match(r'[A-Za-z0-9_.-]+', requirement)
        if not match: return False
        name = normalize(match.group())
        if name not in locked or name not in index or index[name]['version'] != locked[name]: return False
    frontend = json.loads((root / 'web/package.json').read_text(encoding='utf-8'))
    lock = json.loads((root / 'web/package-lock.json').read_text(encoding='utf-8'))
    for name in frontend.get('dependencies', {}):
        version = lock.get('packages', {}).get('node_modules/' + name, {}).get('version')
        if normalize(name) not in index or index[normalize(name)]['version'] != version: return False
    return True


def inspect(root: Path, require_build: bool = False) -> dict:
    root = Path(root)
    checks = []
    def add(category, path, passed, reason=None):
        checks.append({'category': category, 'path': path, 'passed': bool(passed), **({'reason': reason} if reason else {})})
    for category, names in REQUIRED.items():
        for name in names:
            add(category, name, _regular(root, name))
    cli_commands = set()
    for category, names in REQUIRED.items():
        for name in names:
            if not name.endswith('.py'): continue
            try:
                tree = _tree(root, name)
                valid = any(isinstance(node, ast.Call) for node in ast.walk(tree))
                if name == 'emsolver/cli.py':
                    cli_commands = {node.args[0].value for node in ast.walk(tree) if isinstance(node, ast.Call) and
                                    isinstance(node.func, ast.Name) and node.func.id == 'cmd' and node.args and
                                    isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)}
                add('source_syntax', name, valid)
            except (OSError, ValueError, SyntaxError, UnicodeError): add('source_syntax', name, False)
    essentials = {'emsolver/numerics/core.py': ('run_solver', 'generate_sample', 'load_matrix'),
                  'emsolver/contracts.py': ('resolve_config',), 'emsolver/execution.py': ('execute_local',),
                  'emsolver/cli.py': ('main', 'parser'), 'emsolver/reporting.py': ('build_report', 'build_benchmark_report'),
                  'scripts/run_protocol.py': ('main',)}
    for name, functions in essentials.items():
        try: valid = all(_implemented_function(_tree(root, name), function) for function in functions)
        except (ValueError, SyntaxError, UnicodeError, OSError): valid = False
        add('core_semantics', name, valid)
    expected = {'start.sh': ('serve',), 'demo.sh': ('doctor', 'sample', 'inspect', 'solve', 'validate', 'report'),
                'prepare_offline.sh': ()}
    for script, targets in expected.items():
        path = root / 'scripts' / script
        try:
            text = path.read_text(encoding='utf-8')
            syntax = subprocess.run(['bash', '-n', str(path)], capture_output=True, timeout=5).returncode == 0
            found = set(re.findall(r'(?:emsolver|[\"\']?\$bin[\"\']?)\s+([a-z][a-z_-]+)', text))
            valid = syntax and set(targets) <= found and found <= cli_commands
            if script == 'prepare_offline.sh':
                valid &= all(value in text for value in ('pip download', 'pip wheel', '--no-index', 'requirements.lock'))
            add('script_semantics', 'scripts/' + script, valid)
        except (OSError, UnicodeError, subprocess.TimeoutExpired): add('script_semantics', 'scripts/' + script, False)
    try:
        project = tomllib.loads((root / 'pyproject.toml').read_text(encoding='utf-8'))
        target = project['project']['scripts']['emsolver']
        valid = target == 'emsolver.cli:main' and bool(project['build-system']['requires']) and bool(project['project']['dependencies'])
        add('installation_semantics', 'pyproject.toml', valid)
    except (OSError, ValueError, TypeError, KeyError):
        project = {}
        add('installation_semantics', 'pyproject.toml', False)
    for name in ('examples/default.yaml', 'examples/interval.yaml'):
        try:
            resolve_config(yaml.safe_load((root / name).read_text(encoding='utf-8')))
            valid = True
        except (OSError, ValueError, TypeError, yaml.YAMLError): valid = False
        add('example_semantics', name, valid)
    try:
        suite = yaml.safe_load((root / 'benchmarks/required.yaml').read_text(encoding='utf-8'))
        valid = isinstance(suite, dict) and isinstance(suite.get('cases'), list) and bool(suite['cases'])
        for case in suite['cases']:
            valid &= case.get('kind') in {'indefinite', 'clustered', 'repeated', 'near_singular', 'complex'}
            valid &= type(case.get('n')) is int and 4 <= case['n'] <= 1000000
            valid &= type(case.get('k')) is int and 0 < case['k'] < case['n']
            resolve_config(case.get('config') or {})
        add('example_semantics', 'benchmarks/required.yaml', valid)
    except (OSError, ValueError, TypeError, KeyError, yaml.YAMLError): add('example_semantics', 'benchmarks/required.yaml', False)
    add('runnable_sample', 'delivered CLI sample + inspect + solve default example, n=20', _sample_smoke(root))
    for name, terms in {'LICENSE': ('permission', 'copyright'), 'NOTICE.md': ('依赖', '数据'),
                        'README.md': ('sample', 'solve', 'validate', 'pip'),
                        'docs/numerical-implementation.md': ('残差', '验证', '限制'),
                        'docs/backend-implementation.md': ('配置', '进程', '备份'),
                        'docs/datasets.md': ('来源', '许可证', '合成')}.items():
        try:
            text = (root / name).read_text(encoding='utf-8').lower()
            valid = len(text.strip()) >= 200 and all(term in text for term in terms)
        except (OSError, UnicodeError): valid = False
        add('document_semantics', name, valid)
    try: valid = _inventory(root, project)
    except (OSError, ValueError, TypeError, KeyError, AttributeError): valid = False
    add('license_inventory', 'docs/dependency-inventory.json', valid,
        'Direct Python/frontend dependencies need exact lock versions, license metadata and source URLs.')
    if require_build or (root / 'web/dist/index.html').exists():
        try: valid = _built_assets(root)
        except (OSError, ValueError, UnicodeError): valid = False
        add('built_assets', 'web/dist/index.html and referenced local assets', valid)
    registry = root / 'docs/feature-coverage.json'
    try:
        coverage = json.loads(registry.read_text(encoding='utf-8'))
        valid = isinstance(coverage, list) and {row['id'] for row in coverage} == {f'F{i:02}' for i in range(1, 67)} and len(coverage) == 66
    except (OSError, ValueError, KeyError, TypeError):
        coverage, valid = [], False
    add('coverage', '66 unique F01–F66 entries', valid)
    if not isinstance(coverage, list): coverage = []
    for row in coverage:
        evidence = row.get('evidence')
        valid = (row.get('status') in {'implemented', 'partial', 'conditional', 'blocked', 'not_verified'} and
                 bool(row.get('implemented_scope')) and bool(row.get('limitation')) and
                 isinstance(evidence, list) and bool(evidence) and all(_evidence(root, value) for value in evidence))
        add('coverage_evidence', str(row.get('id', 'invalid entry')), valid)
    missing = [row['path'] for row in checks if not row['passed']]
    return {'schema_version': '1.0', 'source_delivery_complete': not missing,
            'functional_acceptance': 'NOT_CERTIFIED',
            'meaning': 'Required source materials passed bounded semantic checks; this does not certify every PRD feature or competition submission.',
            'missing': missing, 'checks': checks,
            'feature_status_counts': {status: sum(row.get('status') == status for row in coverage)
                                      for status in ('implemented', 'partial', 'conditional')}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--require-build', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = inspect(args.root, args.require_build)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + '\n', encoding='utf-8')
    print(text)
    return 0 if result['source_delivery_complete'] else 2


if __name__ == '__main__':
    sys.exit(main())
