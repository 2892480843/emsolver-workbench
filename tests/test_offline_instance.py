import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import socket
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'verify_offline_instance.py'


def load_script():
    spec = importlib.util.spec_from_file_location('offline_instance_verifier', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_standalone_offline_verifier_runs_real_http_solver_exports_restore_and_rerun(tmp_path):
    dist = ROOT / 'web' / 'dist'
    if not (dist / 'index.html').is_file():
        pytest.skip('Build the frontend before verifying its actual served assets')
    copied = tmp_path / 'verify.py'
    copied.write_bytes(SCRIPT.read_bytes())
    output = tmp_path / 'evidence.json'
    work = tmp_path / 'isolated-instance'
    environment = os.environ.copy()
    environment['EMSOLVER_WEB_DIST'] = str(dist)
    environment['EMSOLVER_API_TOKEN'] = 'preexisting-parent-token-must-not-be-used'
    result = subprocess.run([sys.executable, str(copied), '--output', str(output), '--work-dir', str(work)],
                            cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=65)
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads(output.read_text())
    assert evidence['passed'] is True
    assert evidence['version'] == '0.9.0'
    assert evidence['server_processes_stopped'] is True
    assert evidence['server_runs'] == 2
    assert evidence['external_http_requests'] == 0
    checks = evidence['checks']
    assert checks['frontend']['assets'] and all(item['sha256'] for item in checks['frontend']['assets'])
    assert checks['independent_validation']['numerical_status'] == 'PASSED'
    assert checks['independent_validation']['target_certification'] == 'CERTIFIED'
    assert checks['independent_validation']['artifact_integrity'] == 'PASSED'
    assert checks['public_bundle']['checksums_verified'] is True
    assert checks['public_bundle']['runtime_scan_passed'] is True
    assert checks['backup_restore']['empty_destination'] is True
    assert checks['restored_rerun']['status'] == 'SUCCEEDED'
    text = output.read_text()
    assert str(tmp_path) not in text
    assert 'preexisting-parent-token-must-not-be-used' not in text


def test_offline_verifier_refuses_an_existing_work_directory_without_touching_it(tmp_path):
    work = tmp_path / 'existing'
    work.mkdir()
    sentinel = work / 'keep.txt'
    sentinel.write_text('untouched')
    output = tmp_path / 'failed.json'
    result = subprocess.run([sys.executable, str(SCRIPT), '--output', str(output), '--work-dir', str(work)],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode != 0
    assert sentinel.read_text() == 'untouched'
    assert list(work.iterdir()) == [sentinel]
    evidence = json.loads(output.read_text())
    assert evidence['passed'] is False
    assert evidence['error']['code'] == 'WORK_DIRECTORY_EXISTS'
    assert str(tmp_path) not in output.read_text()


def test_public_archive_checks_hashes_and_refuses_unsafe_or_private_runtime_data(tmp_path):
    module = load_script()
    safe = tmp_path / 'safe.zip'
    payload = b'{"status":"SUCCEEDED"}'
    import hashlib
    with zipfile.ZipFile(safe, 'w') as archive:
        archive.writestr('job.json', payload)
        archive.writestr('checksums.json', json.dumps({'job.json': hashlib.sha256(payload).hexdigest()}))
    assert module.scan_public_bundle(safe, b'verifier-private-token', tmp_path)['checksums_verified'] is True
    for member, content in [('job.json', b'{"access_token":"leak"}'),
                            ('job.json', b'{"source_url":"https://user:password@example.org/file?token=private"}'),
                            ('../escape', b'unsafe')]:
        invalid = tmp_path / ('unsafe-' + str(len(member)) + '.zip')
        with zipfile.ZipFile(invalid, 'w') as archive:
            archive.writestr(member, content)
            archive.writestr('checksums.json', json.dumps({member: hashlib.sha256(content).hexdigest()}))
        with pytest.raises(module.VerificationError):
            module.scan_public_bundle(invalid, b'verifier-private-token', tmp_path)
    with zipfile.ZipFile(safe, 'w') as archive:
        archive.writestr('job.json', payload)
        archive.writestr('checksums.json', json.dumps({'job.json': '0' * 64}))
    with pytest.raises(module.VerificationError, match='PUBLIC_HASH_MISMATCH'):
        module.scan_public_bundle(safe, b'verifier-private-token', tmp_path)


def test_requested_occupied_port_is_rejected_before_any_http_operation(tmp_path):
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        output = tmp_path / 'occupied.json'
        result = subprocess.run([sys.executable, str(SCRIPT), '--output', str(output), '--work-dir', str(tmp_path / 'new'),
                                 '--port', str(listener.getsockname()[1])], capture_output=True, text=True, timeout=5)
    assert result.returncode != 0
    evidence = json.loads(output.read_text())
    assert evidence['error']['code'] == 'PORT_UNAVAILABLE'
    assert evidence['server_runs'] == 0
    assert evidence['checks'] == {}
