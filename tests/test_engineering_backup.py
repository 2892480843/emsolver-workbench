"""Engineering assets survive private backup without widening their disclosure."""
import hashlib
import json
from pathlib import Path
import sqlite3
import zipfile

import pytest

from emsolver.engineering import EngineeringAssets
from emsolver.errors import ServiceError
from emsolver.service import Service


@pytest.fixture
def published(tmp_path):
    service = Service(tmp_path / 'source', start_runner=False)
    try:
        project = service.create_project('Private mapped model')
        matrix = service.generate_sample(project['id'],n=30)
        manifest = {'schema_version':'1.0','model_version':'private-mesh-v1','matrix_sha256':matrix['hash'],
            'authorized':True,'public':False,'source':{'title':'private-geometry-source','url':'https://example.org/private-mesh'},
            'license':'LicenseRef-Private','coordinate_units':'mm','field_units':'normalized',
            'coordinates':[[float(index),float(index%3),.125] for index in range(30)],
            'dof_nodes':list(range(30)),'dof_components':['scalar']*30,
            'frequency_mapping':{'kind':'omega_squared','eigenvalue_units':'s^-2','reviewed':True,'applicability':'private-equation-condition'}}
        assets = EngineeringAssets(service)
        asset = assets.create(matrix['id'],manifest)
        assets.select(matrix['id'],asset['id'])
        plan = service.create_plan(config={'problem':{'matrix_id':matrix['id']},'target':{'k':3,'sigma':.1},'resources':{'threads':1}})
        job = service.submit_job(plan['id'])
        service._execute(job['id'])
        assert service.get_job(job['id'])['status'] == 'SUCCEEDED'
        yield service,matrix,asset,job,manifest
    finally: service.close()


def rewrite(source,destination,*,omit=None,change_db=None):
    with zipfile.ZipFile(source) as archive:
        data = {entry.filename:archive.read(entry) for entry in archive.infolist()}
    manifest = json.loads(data['backup-manifest.json'])
    if omit:
        data.pop(omit)
        manifest['files'].pop(omit)
    if change_db:
        database = destination.with_suffix('.sqlite3')
        database.write_bytes(data['metadata.sqlite3'])
        db = sqlite3.connect(database)
        try:
            change_db(db)
            db.commit()
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        finally: db.close()
        data['metadata.sqlite3'] = database.read_bytes()
        database.unlink()
        manifest['files']['metadata.sqlite3'] = hashlib.sha256(data['metadata.sqlite3']).hexdigest()
    data['backup-manifest.json'] = json.dumps(manifest).encode()
    with zipfile.ZipFile(destination,'w') as archive:
        for name,content in data.items(): archive.writestr(name,content)


def test_backup_restores_immutable_geometry_freeze_paths_and_usage(published,tmp_path):
    service,matrix,asset,job,manifest = published
    source_usage = service.project_usage(matrix['project_id'])['input_bytes']
    archive = service.backup()
    destination = tmp_path / 'restored'
    result = service.restore_backup(archive,destination)
    assert result['degraded'] is False
    restored = Service(destination,start_runner=False)
    try:
        assets = EngineeringAssets(restored)
        record = restored.store.get('engineering_assets',asset['id'])
        assert record['_path'] == str(destination / 'engineering' / (asset['sidecar_sha256']+'.json'))
        assert Path(record['_path']).stat().st_mode & 0o222 == 0
        assert assets.manifest(asset['id'])['coordinates'] == manifest['coordinates']
        assert restored.get_job(job['id'])['engineering_model']['sidecar_sha256'] == asset['sidecar_sha256']
        assert assets.field_preview(job['id'],limit=3)['points'][0]['coordinate'] == manifest['coordinates'][0]
        assert restored.project_usage(matrix['project_id'])['input_bytes'] == source_usage
    finally: restored.close()


def test_missing_sidecar_is_only_restored_with_explicit_degraded_consent(published,tmp_path):
    service,_,asset,job,_ = published
    relative = 'engineering/'+asset['sidecar_sha256']+'.json'
    omitted = tmp_path/'omitted.zip'
    rewrite(service.backup(),omitted,omit=relative)
    with pytest.raises(ServiceError): service.restore_backup(omitted,tmp_path/'strict')
    assert not (tmp_path/'strict').exists()
    destination = tmp_path/'degraded'
    result = service.restore_backup(omitted,destination,allow_missing=True)
    assert result['degraded'] is True and relative in result['missing_files']
    restored = Service(destination,start_runner=False)
    try:
        assert restored.store.get('engineering_assets',asset['id'])['availability'] == 'MISSING'
        assert restored.get_job(job['id'])['status'] == 'SUCCEEDED'
        with pytest.raises(ServiceError): EngineeringAssets(restored).manifest(asset['id'])
        with pytest.raises(ServiceError): EngineeringAssets(restored).field_preview(job['id'])
    finally: restored.close()


@pytest.mark.parametrize('mutation',['frozen_hash','frozen_coordinates','frozen_removed','asset_hash','matrix_selection'])
def test_restore_rejects_semantic_engineering_tampering_even_when_archive_hash_matches(published,tmp_path,mutation):
    service,matrix,asset,job,_ = published
    archive = service.backup()
    def change(db):
        kind,identity = ('jobs',job['id']) if mutation.startswith('frozen') else ('engineering_assets',asset['id']) if mutation == 'asset_hash' else ('matrices',matrix['id'])
        body = json.loads(db.execute('SELECT body FROM entities WHERE kind=? AND id=?',(kind,identity)).fetchone()[0])
        if mutation == 'frozen_hash': body['engineering_model']['sidecar_sha256'] = '0'*64
        elif mutation == 'frozen_coordinates': body['engineering_model']['coordinates'] = [[1.,2.,3.]]
        elif mutation == 'frozen_removed': body.pop('engineering_model')
        elif mutation == 'asset_hash': body['matrix_hash'] = '0'*64
        else: body['engineering_asset_id'] = 'eng_missing'
        db.execute('UPDATE entities SET body=? WHERE kind=? AND id=?',(json.dumps(body),kind,identity))
    tampered = tmp_path/'tampered.zip'
    rewrite(archive,tampered,change_db=change)
    with pytest.raises(ServiceError): service.restore_backup(tampered,tmp_path/'refused')
    assert not (tmp_path/'refused').exists()


def test_public_bundle_omits_private_geometry_and_model_conditions(published):
    service,_,asset,job,manifest = published
    archive = service.create_bundle(job['id'])
    with zipfile.ZipFile(archive) as bundle:
        assert not any(name.startswith('engineering/') for name in bundle.namelist())
        for name in ['job.json','events.json','export-policy.json']:
            text = bundle.read(name).decode()
            assert 'engineering_model' not in text
            assert asset['sidecar_sha256'] not in text
            assert manifest['source']['title'] not in text
            assert 'private-equation-condition' not in text


def test_allow_missing_does_not_bypass_sidecar_authorization_metadata(published,tmp_path):
    service,_,asset,_,_ = published
    relative='engineering/'+asset['sidecar_sha256']+'.json'
    def change(db):
        body=json.loads(db.execute("SELECT body FROM entities WHERE kind='engineering_assets' AND id=?",(asset['id'],)).fetchone()[0])
        body['authorized']=False
        db.execute("UPDATE entities SET body=? WHERE kind='engineering_assets' AND id=?",(json.dumps(body),asset['id']))
    archive=tmp_path/'missing-unauthorized.zip'
    rewrite(service.backup(),archive,omit=relative,change_db=change)
    with pytest.raises(ServiceError):service.restore_backup(archive,tmp_path/'refused',allow_missing=True)
