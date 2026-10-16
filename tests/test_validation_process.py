import copy
import json
from pathlib import Path

import numpy as np
import pytest

from emsolver.contracts import resolve_config
from emsolver.integrity import file_hash
from emsolver.numerics import generate_sample, run_solver
from emsolver.validation_process import verify_bounded


@pytest.fixture
def published(tmp_path):
    matrix = tmp_path / 'matrix.npz'
    generate_sample(matrix, n=30)
    directory = tmp_path / 'private-run'
    directory.mkdir(mode=0o700)
    artifacts = directory / 'artifacts'
    config = resolve_config({'target': {'k': 3, 'sigma': .1}, 'resources': {'threads': 1}})
    run_solver(matrix, config, artifacts)
    return matrix, artifacts, config


def test_real_child_verifies_original_problem_without_mutating_artifacts(published):
    matrix, artifacts, config = published
    before = {path.name: file_hash(path) for path in artifacts.iterdir() if path.is_file()}
    parent_before = {path.name for path in artifacts.parent.iterdir()}
    result = verify_bounded(matrix, artifacts, config)
    assert result['numerical_status'] == 'PASSED'
    assert result['target_certification'] == 'CERTIFIED'
    assert result['artifact_integrity'] == 'PASSED'
    assert {path.name: file_hash(path) for path in artifacts.iterdir() if path.is_file()} == before
    assert {path.name for path in artifacts.parent.iterdir()} == parent_before


def test_zero_vector_and_rehashed_manifest_cannot_forge_verification(published):
    matrix, artifacts, config = published
    vectors = np.load(artifacts / 'eigenvectors.npy', allow_pickle=False)
    vectors[:, 0] = 0
    np.save(artifacts / 'eigenvectors.npy', vectors, allow_pickle=False)
    manifest = json.loads((artifacts / 'manifest.json').read_text())
    manifest['artifacts']['eigenvectors.npy'] = file_hash(artifacts / 'eigenvectors.npy')
    (artifacts / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    result = verify_bounded(matrix, artifacts, config)
    assert result is not None and result['numerical_status'] == 'FAILED'
    assert result['target_certification'] == 'UNVERIFIED'
    assert not any(path.name.startswith('.verify-') for path in artifacts.parent.iterdir())


@pytest.mark.parametrize('resource,value', [('walltime_seconds', .001), ('memory_limit_gib', 1e-8), ('disk_limit_gib', 1e-9)])
def test_real_child_resource_failures_return_none_and_clean_controls(published, resource, value):
    matrix, artifacts, config = published
    limited = copy.deepcopy(config)
    limited['resources'][resource] = value
    assert verify_bounded(matrix, artifacts, limited) is None
    assert not any(path.name.startswith('.verify-') for path in artifacts.parent.iterdir())


def test_verifier_environment_does_not_inherit_secrets(published, monkeypatch):
    import emsolver.validation_process as module
    matrix, artifacts, config = published
    monkeypatch.setenv('EMSOLVER_API_TOKEN', 'test-only-secret')
    monkeypatch.setenv('AWS_SECRET_ACCESS_KEY', 'test-only-secret')
    actual = module.subprocess.Popen
    seen = []
    def spawn(*args, **kwargs):
        seen.append(kwargs['env'])
        return actual(*args, **kwargs)
    monkeypatch.setattr(module.subprocess, 'Popen', spawn)
    assert verify_bounded(matrix, artifacts, config)['numerical_status'] == 'PASSED'
    assert seen and 'EMSOLVER_API_TOKEN' not in seen[0] and 'AWS_SECRET_ACCESS_KEY' not in seen[0]
    assert seen[0]['OMP_NUM_THREADS'] == str(config['resources']['threads'])


def test_symlinked_published_arrays_are_rejected(published, tmp_path):
    matrix, artifacts, config = published
    array = artifacts / 'eigenvectors.npy'
    target = tmp_path / 'outside.npy'
    array.replace(target)
    array.symlink_to(target)
    assert verify_bounded(matrix, artifacts, config) is None
    assert target.exists()


@pytest.mark.parametrize('raw', [b'not json', b'\xff', b' ' * (1024**2 + 1),
    b'{"schema_version":"1.0","schema_version":"1.0","validation":{}}',
    b'{"schema_version":"1.0","validation":{"numerical_status":"PASSED","target_certification":"CERTIFIED","value":NaN}}'])
def test_untrusted_result_format_is_bounded_and_rejected(published, monkeypatch, raw):
    import emsolver.validation_process as module
    matrix, artifacts, config = published
    class Completed:
        returncode = 0
        def poll(self): return 0
    def malformed(args, **_):
        result = Path(args[args.index('--result') + 1])
        result.write_bytes(raw)
        result.chmod(0o600)
        return Completed()
    monkeypatch.setattr(module.subprocess, 'Popen', malformed)
    assert verify_bounded(matrix, artifacts, config) is None
    assert not any(path.name.startswith('.verify-') for path in artifacts.parent.iterdir())
