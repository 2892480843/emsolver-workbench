import importlib.util
import json
from pathlib import Path
import shutil

import pytest


SOURCE = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('delivery_checker', SOURCE / 'scripts' / 'check_delivery.py')
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)


@pytest.fixture
def delivery(tmp_path):
    root = tmp_path / 'delivery'
    root.mkdir()
    for directory in ('emsolver', 'tests', 'examples', 'benchmarks', 'scripts', 'docs'):
        shutil.copytree(SOURCE / directory, root / directory, ignore=shutil.ignore_patterns('__pycache__'))
    for name in ('LICENSE', 'NOTICE.md', 'README.md', 'pyproject.toml', 'requirements.lock'):
        shutil.copyfile(SOURCE / name, root / name)
    (root / 'web').mkdir()
    shutil.copytree(SOURCE / 'web' / 'src', root / 'web' / 'src')
    for name in ('package.json', 'package-lock.json'):
        shutil.copyfile(SOURCE / 'web' / name, root / 'web' / name)
    for row in json.loads((SOURCE / 'docs/feature-coverage.json').read_text(encoding='utf-8')):
        for evidence in row.get('evidence', []):
            relative = Path(evidence.split('::', 1)[0])
            if relative.is_absolute() or '..' in relative.parts: continue
            origin, target = SOURCE / relative, root / relative
            if origin.is_file() and not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(origin, target)
    return root


@pytest.mark.parametrize('script', ['start.sh', 'demo.sh', 'prepare_offline.sh'])
def test_empty_or_semantic_stub_scripts_block_delivery(delivery, script):
    (delivery / 'scripts' / script).write_text('#!/usr/bin/env bash\n# TODO\necho placeholder\n', encoding='utf-8')
    result = checker.inspect(delivery)
    assert result['source_delivery_complete'] is False
    assert any(row['category'] == 'script_semantics' and not row['passed'] for row in result['checks'])


def test_malformed_example_and_missing_sample_generator_block_delivery(delivery):
    (delivery / 'examples' / 'default.yaml').write_text('target: {k: 0}\n', encoding='utf-8')
    result = checker.inspect(delivery)
    assert result['source_delivery_complete'] is False
    assert any(row['category'] == 'example_semantics' and not row['passed'] for row in result['checks'])
    (delivery / 'emsolver' / 'numerics' / 'core.py').write_text('def generate_sample(*args):\n    pass\n', encoding='utf-8')
    result = checker.inspect(delivery)
    assert any(row['category'] == 'runnable_sample' and not row['passed'] for row in result['checks'])


def test_fake_coverage_evidence_paths_are_not_accepted(delivery):
    path = delivery / 'docs' / 'feature-coverage.json'
    rows = json.loads(path.read_text())
    rows[0]['evidence'] = ['tests/nonexistent.py::test_fiction']
    path.write_text(json.dumps(rows), encoding='utf-8')
    result = checker.inspect(delivery)
    assert result['source_delivery_complete'] is False
    assert any(row['category'] == 'coverage_evidence' and not row['passed'] for row in result['checks'])


def test_invalid_license_inventory_and_missing_built_asset_block_delivery(delivery):
    (delivery / 'docs' / 'dependency-inventory.json').write_text('[{"name":"numpy","version":"unknown","license_expression":""}]', encoding='utf-8')
    dist = delivery / 'web' / 'dist'
    dist.mkdir()
    (dist / 'index.html').write_text('<script type="module" src="/assets/missing.js"></script>', encoding='utf-8')
    result = checker.inspect(delivery, require_build=True)
    assert result['source_delivery_complete'] is False
    assert any(row['category'] == 'license_inventory' and not row['passed'] for row in result['checks'])
    assert any(row['category'] == 'built_assets' and not row['passed'] for row in result['checks'])


def test_material_presence_never_certifies_functional_acceptance(delivery):
    result = checker.inspect(delivery)
    assert result['source_delivery_complete'] is True, [row for row in result['checks'] if not row['passed']]
    assert result['functional_acceptance'] == 'NOT_CERTIFIED'
    assert 'does not certify' in result['meaning']


def test_schema_valid_but_unrunnable_example_is_blocked(delivery):
    (delivery / 'examples/default.yaml').write_text('solver: {backend: slepc}\n', encoding='utf-8')
    result = checker.inspect(delivery)
    assert result['source_delivery_complete'] is False
    assert any(row['category'] == 'runnable_sample' and not row['passed'] for row in result['checks'])
