"""Private backups retain acceptance records without exposing trust keys."""
import json
from contextlib import closing
from pathlib import Path
import zipfile
import pytest
from test_industrial import accepted_input
from emsolver.industrial import IndustrialCases
from emsolver.service import Service
from emsolver.errors import ServiceError


def test_backup_restores_dossier_and_check_and_usage(accepted_input, tmp_path):
    service, project, _, _, manifest, ledger = accepted_input
    case = ledger.create(project['id'], manifest)
    result = ledger.check(case['id'])
    backup = service.backup()
    destination = tmp_path / 'restored'
    assert service.restore_backup(backup, destination)['restored'] is True
    with closing(Service(destination, start_runner=False)) as restored:
        other = IndustrialCases(restored)
        assert other.dossier(case['id']) == ledger.dossier(case['id'])
        assert other.get(case['id'])['latest_check'] == result
        assert other.check(case['id'])['industrial_accepted'] is False
        usage = restored.project_usage(project['id'])
        assert usage['input_bytes'] >= sum(item['bytes'] for kind in ['industrial_cases', 'industrial_checks'] for item in restored.store.select(kind))


def test_backup_skips_configured_authority_file_even_if_misplaced(accepted_input, monkeypatch):
    service, _, _, _, _, _ = accepted_input
    key = service.root / 'industrial' / 'authority.json'
    key.write_text('{"keys": [{"secret": "must-not-leave-in-backup"}]}')
    key.chmod(0o600)
    monkeypatch.setenv('EMSOLVER_INDUSTRIAL_ATTESTATION_KEYS_FILE', str(key))
    backup = service.backup()
    with zipfile.ZipFile(backup) as archive:
        assert 'industrial/authority.json' not in archive.namelist()
        assert b'must-not-leave-in-backup' not in b''.join(archive.read(name) for name in archive.namelist())


def test_missing_dossier_requires_explicit_degraded_restore(accepted_input, tmp_path):
    service, _, _, _, manifest, ledger = accepted_input
    project = service.get_job(manifest['candidate_job_id'])['project_id']
    case = ledger.create(project, manifest)
    ledger.check(case['id'])
    backup = service.backup()
    broken = tmp_path / 'missing.zip'
    with zipfile.ZipFile(backup) as source, zipfile.ZipFile(broken, 'w') as dest:
        for member in source.infolist():
            if member.filename != 'industrial/' + case['dossier_sha256'] + '.json':
                dest.writestr(member, source.read(member.filename))
    with pytest.raises(ServiceError): service.restore_backup(broken, tmp_path / 'rejected')
    restored = service.restore_backup(broken, tmp_path / 'degraded', allow_missing=True)
    assert restored['degraded'] is True
    with closing(Service(tmp_path / 'degraded', start_runner=False)) as gateway:
        with pytest.raises(ServiceError): IndustrialCases(gateway).dossier(case['id'])


def test_large_reference_asset_roundtrip_and_missing_restore(accepted_input, tmp_path):
    import numpy as np
    from emsolver.industrial_reference import IndustrialReferenceAssets
    service, project, _, _, _, _ = accepted_input
    source = service.root / 'inbox' / 'reference.npz'
    np.savez_compressed(source, eigenvalues=np.array([1.]), eigenvectors=np.ones((589446, 1)))
    asset = IndustrialReferenceAssets(service).create(project['id'], source)
    backup = service.backup()
    destination = tmp_path / 'restored-reference'
    service.restore_backup(backup, destination)
    with closing(Service(destination, start_runner=False)) as gateway:
        restored = IndustrialReferenceAssets(gateway)
        assert restored.get(asset['id']) == asset
        assert restored.path(asset['id']).parent == destination / 'industrial'
    broken = tmp_path / 'missing-reference.zip'
    name = 'industrial/' + asset['sha256'] + '.npz'
    with zipfile.ZipFile(backup) as archive, zipfile.ZipFile(broken, 'w') as output:
        for member in archive.infolist():
            if member.filename != name: output.writestr(member, archive.read(member.filename))
    service.restore_backup(broken, tmp_path / 'missing-reference', allow_missing=True)
    with closing(Service(tmp_path / 'missing-reference', start_runner=False)) as gateway:
        restored = IndustrialReferenceAssets(gateway)
        assert restored.get(asset['id'])['availability'] == 'MISSING'
        with pytest.raises(ServiceError): restored.path(asset['id'])
