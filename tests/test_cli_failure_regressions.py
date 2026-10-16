import argparse
import hashlib
import json
import zipfile

import pytest
import yaml

from emsolver.cli import main, _reproduce


def test_missing_input_keeps_config_and_generates_failure_report(tmp_path, capsys):
    output = tmp_path/'failed'
    assert main(['solve', '--matrix', str(tmp_path/'missing.npz'), '--output', str(output), '--k', '3', '--json']) == 2
    result = json.loads(capsys.readouterr().out)
    assert result['validation']['numerical_status'] == 'NOT_VALIDATED'
    assert result['validation']['target_certification'] == 'UNVERIFIED'
    assert yaml.safe_load((output/'config.resolved.yaml').read_text())['target']['k'] == 3
    assert main(['report', '--run', str(output), '--json']) == 0
    assert 'FAILED' in (output/'report.html').read_text()


def test_timeout_keeps_config_and_generates_failure_report(tmp_path, capsys):
    from scipy import sparse
    matrix = tmp_path/'matrix.npz'
    sparse.save_npz(matrix, sparse.eye(40, format='csr'))
    config = tmp_path/'config.yaml'
    config.write_text('target:\n  k: 3\nresources:\n  walltime_seconds: 0.001\n')
    output = tmp_path/'timeout'
    assert main(['solve', '--matrix', str(matrix), '--config', str(config), '--output', str(output), '--json']) == 10
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'TIMED_OUT'
    assert yaml.safe_load((output/'config.resolved.yaml').read_text())['resources']['walltime_seconds'] == 0.001
    assert main(['report', '--run', str(output), '--json']) == 0
    assert 'TIMED_OUT' in (output/'report.html').read_text()


def test_legacy_failure_report_does_not_invent_configuration(tmp_path):
    (tmp_path/'summary.json').write_text(json.dumps({'status': 'FAILED', 'error_code': 'WORKER_LOST'}))
    assert main(['report', '--run', str(tmp_path)]) == 0
    text = (tmp_path/'report.html').read_text()
    assert 'UNAVAILABLE' in text
    assert 'memory_limit_gib' not in text


@pytest.mark.parametrize('fault', ['unlisted', 'missing', 'duplicate', 'corrupt'])
def test_reproduce_rejects_incomplete_or_ambiguous_checksum_manifest(tmp_path, fault):
    payload = b'target:\n  k: 3\n'
    name = 'run/config.resolved.yaml'
    checks = {name: hashlib.sha256(payload).hexdigest()}
    if fault == 'unlisted': checks = {}
    if fault == 'missing': checks['absent.txt'] = hashlib.sha256(b'absent').hexdigest()
    path = tmp_path/'bundle.zip'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr(name, b'changed' if fault == 'corrupt' else payload)
        if fault == 'duplicate':
            with pytest.warns(UserWarning, match='Duplicate name'):
                archive.writestr(name, payload)
        archive.writestr('checksums.json', json.dumps(checks))
    args = argparse.Namespace(output=str(tmp_path/'output'), bundle=str(path), matrix='matrix.npz')
    with pytest.raises(ValueError, match='CHECKSUM|DUPLICATE'):
        _reproduce(args)
    assert not (tmp_path/'output/bundle').exists()


def test_reproduce_complete_archive_uses_installed_executor(tmp_path, monkeypatch):
    import emsolver.execution
    payloads = {'run/config.resolved.yaml': b'target:\n  k: 3\n',
                'emsolver/execution.py': b'raise AssertionError("must not execute archive code")'}
    path = tmp_path/'bundle.zip'
    with zipfile.ZipFile(path, 'w') as archive:
        for name, value in payloads.items(): archive.writestr(name, value)
        archive.writestr('checksums.json', json.dumps({name: hashlib.sha256(value).hexdigest() for name, value in payloads.items()}))
    calls = []
    def execute(matrix, config, output):
        calls.append(config)
        return {'status': 'SUCCEEDED'}
    monkeypatch.setattr(emsolver.execution, 'execute_local', execute)
    args = argparse.Namespace(output=str(tmp_path/'output'), bundle=str(path), matrix='matrix.npz')
    assert _reproduce(args)[1] == 0
    assert calls[0]['target']['k'] == 3
