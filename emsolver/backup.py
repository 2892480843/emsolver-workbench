"""Reconstruct trusted metadata from an immutable, validated backup snapshot."""
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
import re
import sqlite3

from .errors import ServiceError
from .integrity import file_hash
from .contracts import content_hash
from .experiments import validate_frozen_plan
from .store import Store, SCHEMA_VERSION, encode, utcnow


KINDS=frozenset(('projects','matrices','configs','plans','jobs','benchmarks','reports','experiment_plans','project_storage','advice_effects','imports','engineering_assets','industrial_cases','industrial_checks','industrial_reference_assets'))


def _engineering_asset(item, matrix, data_root, staging_root, *, allow_missing, missing_files):
    """Rebind only an authenticated, canonical model payload; never execute it."""
    from .engineering_contracts import EngineeringMetadata, MAX_DOFS, MAX_MANIFEST_BYTES, normalize_manifest
    metadata=('model_version','model_kind','coordinate_units','field_units','source','license','authorized','public','frequency_mapping')
    required={'id','schema_version','project_id','matrix_id','matrix_hash','sidecar_sha256','bytes','created_at','_path',
              'n_nodes','n_dofs','nullspace_report','authorization_basis',*metadata}
    if (not required <= set(item) or set(item)-required-{'components','availability'} or
        item.get('schema_version')!='1.0' or not re.fullmatch(r'eng_[0-9a-f]{32}',item.get('id','')) or
        item.get('project_id')!=matrix.get('project_id') or item.get('matrix_id')!=matrix.get('id') or item.get('matrix_hash')!=matrix.get('hash') or
        type(item.get('bytes')) is not int or not 0<item['bytes']<=MAX_MANIFEST_BYTES or
        type(item.get('n_nodes')) is not int or not 1<=item['n_nodes']<=100000 or
        item.get('n_dofs')!=matrix.get('n') or type(item.get('n_dofs')) is not int or not 1<=item['n_dofs']<=MAX_DOFS or
        not isinstance(item.get('created_at'),str) or len(item['created_at'])>64 or
        item.get('availability','READY') not in {'READY','MISSING'} or
        item.get('authorization_basis')!='explicit project administrator declaration; no independent legal or physical certification'):
        raise ServiceError('INVALID_BACKUP_ENGINEERING')
    try:
        EngineeringMetadata.model_validate({**{key:item[key] for key in metadata},'schema_version':item['schema_version'],'matrix_sha256':item['matrix_hash']})
        if datetime.fromisoformat(item['created_at']).tzinfo is None:raise ValueError('Missing creation timezone')
        components=item.get('components')
        if components is not None and (not isinstance(components,list) or not components or any(value not in {'scalar','x','y','z'} for value in components) or
            components!=sorted(set(components)) or ('scalar' in components and components!=['scalar'])):
            raise ValueError('Invalid component declaration')
        report=item['nullspace_report']
        if report is not None:
            if (not isinstance(report,dict) or set(report)!={'status','basis_count','residuals','residual_tolerance','interpretation','verification_input','automatic_removal','physical_interpretation'} or
                type(report.get('basis_count')) is not int or not 1<=report['basis_count']<=16 or
                not isinstance(report.get('residuals'),list) or len(report['residuals'])!=report['basis_count'] or
                any(type(value) not in (int,float) or not math.isfinite(value) or value<0 for value in report['residuals']) or
                type(report.get('residual_tolerance')) not in (int,float) or not math.isfinite(report['residual_tolerance']) or not 0<report['residual_tolerance']<=1e-8 or
                report.get('interpretation') not in {'unspecified','expected_nullspace'} or report.get('verification_input')!='original matrix' or report.get('automatic_removal') is not False or
                report.get('physical_interpretation')!='user-declared only; matrix nullspace does not identify spurious physical modes' or
                report.get('status')!=('VERIFIED' if all(value<=report['residual_tolerance'] for value in report['residuals']) else 'UNVERIFIED')):
                raise ValueError('Invalid historical nullspace report')
    except (ValueError,TypeError,KeyError):raise ServiceError('INVALID_BACKUP_ENGINEERING') from None
    sha=item.get('sidecar_sha256')
    if not isinstance(sha,str) or not re.fullmatch(r'[0-9a-f]{64}',sha):raise ServiceError('INVALID_BACKUP_ENGINEERING')
    filename=sha+'.json'
    if not isinstance(item.get('_path'),str) or Path(item['_path']).name!=filename:raise ServiceError('INVALID_BACKUP_ENGINEERING')
    local=staging_root/'engineering'/filename
    if local.is_symlink() or local.parent.is_symlink():raise ServiceError('INVALID_BACKUP_ENGINEERING')
    if not local.is_file():
        if not allow_missing:raise ServiceError('BACKUP_HASH_MISMATCH')
        item['availability']='MISSING'
        if missing_files is not None:missing_files.add('engineering/'+filename)
    else:
        if local.stat().st_size!=item['bytes'] or file_hash(local)!=sha:raise ServiceError('BACKUP_HASH_MISMATCH')
        try:
            raw=local.read_bytes()
            manifest=normalize_manifest(json.loads(raw),matrix)
            canonical=json.dumps(manifest,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')
            if raw!=canonical or any(item[key]!=manifest[key] for key in metadata):raise ValueError('Changed summary')
            if (item['n_nodes']!=len(manifest['coordinates']) or item['n_dofs']!=len(manifest['dof_nodes']) or
                ('components' in item and item['components']!=sorted(set(manifest['dof_components'])))):
                raise ValueError('Changed shape')
            declaration=manifest['nullspace'];report=item['nullspace_report']
            if declaration is None:
                if report is not None:raise ValueError('Unexpected nullspace report')
            else:
                if (report is None or report['basis_count']!=len(declaration['basis']) or
                    report['residual_tolerance']!=declaration['residual_tolerance'] or report['interpretation']!=declaration['interpretation']):
                    raise ValueError('Changed nullspace report')
        except (ValueError,TypeError,KeyError,UnicodeError,RecursionError,ServiceError):
            raise ServiceError('INVALID_BACKUP_ENGINEERING') from None
        local.chmod(0o400)
        item['availability']='READY'
    item['_path']=str(data_root/'engineering'/filename)
    return item


def _rows(connection,query):
    cursor=connection.execute(query)
    try:
        while batch:=cursor.fetchmany(500):
            yield from batch
    finally:
        cursor.close()


def _object(raw,max_bytes=1024**2):
    if not isinstance(raw,str) or len(raw.encode('utf-8'))>max_bytes:
        raise ServiceError('INVALID_BACKUP_DATABASE')
    value=json.loads(raw)
    if not isinstance(value,dict):raise ServiceError('INVALID_BACKUP_DATABASE')
    # Encoding also rejects nonfinite numbers, including exponent overflow.
    encode(value)
    return value


def rebuild_metadata(source_path: Path,target_path: Path,data_root: Path,staging_root: Path,*,allow_missing=False,missing_files=None) -> int:
    """Copy bounded batches in original order under one destination transaction.

    Index projections and cursor identity are rebuilt by Store. No schema,
    trigger, view or index from the source database is installed in the target.
    """
    source=sqlite3.connect(Path(source_path).as_uri()+'?mode=ro&immutable=1',uri=True)
    target=None
    try:
        source.execute('PRAGMA trusted_schema=OFF')
        if source.execute('PRAGMA user_version').fetchone()[0] not in (0,SCHEMA_VERSION):
            raise ServiceError('UNSUPPORTED_BACKUP_SCHEMA')
        if source.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
            raise ServiceError('INVALID_BACKUP_DATABASE')
        count=source.execute('SELECT count(*) FROM entities').fetchone()[0]
        if count>100000:raise ServiceError('BACKUP_TOO_LARGE')
        if source.execute('SELECT 1 FROM entities GROUP BY kind,id HAVING count(*)>1 LIMIT 1').fetchone():
            raise ServiceError('INVALID_BACKUP_DATABASE')
        def reference(kind,identity):
            if not isinstance(identity,str) or not source.execute('SELECT 1 FROM entities WHERE kind=? AND id=?',(kind,identity)).fetchone():
                raise ServiceError('BACKUP_REFERENCE_MISSING')
        def entity(kind,identity):
            reference(kind,identity)
            return _object(source.execute('SELECT body FROM entities WHERE kind=? AND id=?',(kind,identity)).fetchone()[0],16*1024**2)
        industrial_payloads={}
        def industrial_payload(kind, identity):
            key=(kind,identity)
            if key in industrial_payloads:return industrial_payloads[key]
            item=entity(kind,identity)
            digest=item.get('dossier_sha256') if kind=='industrial_cases' else item.get('check_sha256')
            if not isinstance(digest,str) or not re.fullmatch(r'[0-9a-f]{64}',digest):raise ServiceError('INVALID_BACKUP_INDUSTRIAL')
            local=staging_root/'industrial'/(digest+'.json')
            if local.is_symlink() or local.parent.is_symlink():raise ServiceError('INVALID_BACKUP_INDUSTRIAL')
            if not local.is_file():
                if not allow_missing:raise ServiceError('BACKUP_HASH_MISMATCH')
                industrial_payloads[key]=None
                return None
            from .industrial_contracts import MAX_DOSSIER_BYTES,MAX_CHECK_BYTES
            maximum=MAX_DOSSIER_BYTES if kind=='industrial_cases' else MAX_CHECK_BYTES
            if local.stat().st_size>maximum or local.stat().st_size!=item.get('bytes') or file_hash(local)!=digest:raise ServiceError('BACKUP_HASH_MISMATCH')
            from .engineering_api import unique_object
            try:
                payload=json.loads(local.read_text(encoding='utf-8'),object_pairs_hook=unique_object)
                from .engineering import canonical
                if canonical(payload)!=local.read_bytes():raise ValueError('Noncanonical payload')
            except (ValueError,UnicodeError,RecursionError,TypeError):raise ServiceError('INVALID_BACKUP_INDUSTRIAL') from None
            industrial_payloads[key]=payload
            return payload
        def industrial_record(kind,item):
            payload=industrial_payload(kind,item['id'])
            try:
                from .industrial_backup import validate_backup_record
                validate_backup_record(kind,item,payload,entity,payload_lookup=industrial_payload)
            except (ValueError,TypeError,KeyError,RecursionError):raise ServiceError('INVALID_BACKUP_INDUSTRIAL') from None
            digest=item.get('dossier_sha256') if kind=='industrial_cases' else item.get('check_sha256')
            filename=digest+'.json';local=staging_root/'industrial'/filename
            if payload is None:
                item['availability']='MISSING'
                if missing_files is not None:missing_files.add('industrial/'+filename)
            else:
                local.chmod(0o400);item['availability']='READY'
            item['_path']=str(data_root/'industrial'/filename)
            return item
        checked_engineering={}
        def engineering_asset(identity):
            if identity not in checked_engineering:
                item=entity('engineering_assets',identity)
                matrix=entity('matrices',item['matrix_id'])
                reference('projects',item['project_id'])
                checked_engineering[identity]=_engineering_asset(item,matrix,data_root,staging_root,allow_missing=allow_missing,missing_files=missing_files)
            return checked_engineering[identity]
        def engineering_snapshot(item):
            snapshot=item.get('engineering_model')
            if snapshot is None:return
            if not isinstance(snapshot,dict):raise ServiceError('INVALID_BACKUP_ENGINEERING')
            if snapshot.get('availability','READY') not in {'READY','MISSING'}:raise ServiceError('INVALID_BACKUP_ENGINEERING')
            record=engineering_asset(snapshot.get('id'))
            original=entity('engineering_assets',record['id'])
            expected={key:value for key,value in original.items() if not key.startswith('_') and key!='availability'}
            actual={key:value for key,value in snapshot.items() if key!='availability'}
            expected['kind']='engineering_model_snapshot'
            if (content_hash(actual)!=content_hash(expected) or
                item.get('matrix_id')!=record['matrix_id'] or item.get('project_id')!=record['project_id'] or
                (item.get('matrix_hash') if 'matrix_hash' in item else item.get('input_hash'))!=record['matrix_hash']):
                raise ServiceError('INVALID_BACKUP_ENGINEERING')
        checked_batches=set()
        checked_plans=set()
        def validate_experiment_plan(plan):
            if plan['id'] not in checked_plans:
                validate_frozen_plan(plan,lambda kind,identity:entity(kind,identity))
                checked_plans.add(plan['id'])
        def validate_experiment_batch(batch):
            if batch['id'] in checked_batches:return
            plan=entity('experiment_plans',batch['experiment_plan_id'])
            validate_experiment_plan(plan)
            if (plan['project_id']!=batch['project_id'] or plan['hash']!=batch.get('experiment_hash') or
                any(content_hash(plan[field])!=content_hash(batch.get(field)) for field in ('cells','definition','budget')) or
                type(batch.get('repeats')) is not int or batch['repeats']!=plan['definition']['repeats'] or
                batch.get('matrix_ids')!=plan['definition']['matrix_ids'] or
                batch.get('plan_hashes')!=[cell['plan_hash'] for cell in plan['cells']]):
                raise ServiceError('BACKUP_REFERENCE_MISSING')
            cells={cell['id']:cell for cell in plan['cells']}
            repeats=plan['definition']['repeats']
            expected={(cell_id,repeat) for cell_id in cells for repeat in range(1,repeats+1)}
            members=batch.get('jobs')
            if not isinstance(members,list) or len(members)!=len(expected) or len(set(members))!=len(members):
                raise ServiceError('BACKUP_REFERENCE_MISSING')
            seen=set()
            for job_id in members:
                job=entity('jobs',job_id);link=job.get('experiment') or {}
                cell=cells.get(link.get('cell_id'));repeat=link.get('repeat')
                if (not cell or type(repeat) is not int or (cell['id'],repeat) not in expected or
                    link.get('batch_id')!=batch['id'] or job.get('project_id')!=batch['project_id'] or
                    job.get('plan_id')!=cell['plan_id'] or job.get('matrix_id')!=cell['matrix_id'] or
                    job.get('matrix_hash')!=cell['matrix_hash'] or content_hash(job.get('config'))!=content_hash(cell['config'])):
                    raise ServiceError('BACKUP_REFERENCE_MISSING')
                seen.add((cell['id'],repeat))
            if seen!=expected:raise ServiceError('BACKUP_REFERENCE_MISSING')
            checked_batches.add(batch['id'])
        target=Store(target_path)
        with target.transaction():
            for kind,identity,raw in _rows(source,'SELECT kind,id,body FROM entities ORDER BY rowid'):
                if kind not in KINDS or not isinstance(identity,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}',identity):
                    raise ServiceError('INVALID_BACKUP_DATABASE')
                item=_object(raw,16*1024**2)
                if item.get('id')!=identity:raise ServiceError('INVALID_BACKUP_DATABASE')
                if kind in {'matrices','jobs','plans','benchmarks','experiment_plans','project_storage','advice_effects','imports','engineering_assets'}:reference('projects',item['project_id'])
                if kind=='industrial_reference_assets':
                    reference('projects',item['project_id'])
                    from .industrial_reference import validate_record
                    filename=str(item.get('sha256',''))+'.npz'
                    local=staging_root/'industrial'/filename
                    if local.is_symlink() or local.parent.is_symlink():raise ServiceError('INVALID_BACKUP_INDUSTRIAL')
                    if not local.is_file():
                        if not allow_missing:raise ServiceError('BACKUP_HASH_MISMATCH')
                        validate_record(item,None)
                        item['availability']='MISSING'
                        if missing_files is not None:missing_files.add('industrial/'+filename)
                    else:
                        validate_record(item,local);local.chmod(0o400);item['availability']='READY'
                    item['_path']=str(data_root/'industrial'/filename)
                if kind in {'industrial_cases','industrial_checks'}:item=industrial_record(kind,item.copy())
                if kind=='engineering_assets':item=engineering_asset(identity).copy()
                if kind=='project_storage':
                    if any(type(item.get(key)) not in (int,float) or item[key]<0 for key in ('input_bytes','output_bytes')):
                        raise ServiceError('INVALID_BACKUP_DATABASE')
                    # Usage is a derived projection. Rebuild it from restored
                    # files instead of trusting counters from a different root.
                    continue
                if kind=='imports':
                    item={key:value for key,value in item.items() if not key.startswith('_')}
                    item.pop('worker_pid',None)
                    if item.get('status') in {'QUEUED','RUNNING','CANCELLING'}:
                        item.update(status='FAILED',stage='failed',finished_at=utcnow(),error={'code':'SOURCE_NOT_BACKED_UP'})
                if kind=='advice_effects':
                    reference('jobs',item['source_job_id']);reference('configs',item['derived_config_id'])
                    if item.get('new_job_id'):reference('jobs',item['new_job_id'])
                    if item.get('record_type')!='derived':reference('advice_effects',item['effect_id'])
                if kind=='matrices':
                    if item.get('engineering_asset_id'):
                        selected=engineering_asset(item['engineering_asset_id'])
                        if selected['matrix_id']!=identity or selected['matrix_hash']!=item.get('hash') or selected['project_id']!=item.get('project_id'):
                            raise ServiceError('INVALID_BACKUP_ENGINEERING')
                    filename=Path(item.get('_path','')).name
                    if not re.fullmatch(r'[0-9a-f]{64}\.(npz|npy)',filename):raise ServiceError('INVALID_BACKUP_MATRIX')
                    local=staging_root/'matrices'/filename
                    if not local.is_file():
                        if not allow_missing:raise ServiceError('BACKUP_HASH_MISMATCH')
                        item['availability']='MISSING'
                        if missing_files is not None:missing_files.add('matrices/'+filename)
                    elif file_hash(local)!=item['hash']:raise ServiceError('BACKUP_HASH_MISMATCH')
                    else:local.chmod(0o400);item.pop('availability',None)
                    item['_path']=str(data_root/'matrices'/filename)
                if kind in {'jobs','plans'}:reference('matrices',item['matrix_id'])
                if kind in {'jobs','plans'}:engineering_snapshot(item)
                if kind=='jobs' and item.get('plan_id'):
                    parent=entity('plans',item['plan_id'])
                    if content_hash(parent.get('engineering_model'))!=content_hash(item.get('engineering_model')):
                        raise ServiceError('INVALID_BACKUP_ENGINEERING')
                if kind=='configs':reference('matrices',item['config']['problem']['matrix_id'])
                if kind=='plans':reference('configs',item['config_id'])
                if kind=='jobs' and item.get('parent_job_id'):reference('jobs',item['parent_job_id'])
                if kind=='jobs' and item.get('resumption'):
                    reference('jobs',item['resumption']['source_job_id'])
                    if not re.fullmatch(r'[0-9a-f]{64}',item['resumption'].get('checkpoint_sha256','')):
                        raise ServiceError('INVALID_BACKUP_DATABASE')
                if kind=='jobs' and item.get('status') in {'SUCCEEDED','PARTIAL'} and item.get('result') and item.get('artifacts_state')!='EXPIRED':
                    run=staging_root/'runs'/identity/'artifacts'
                    manifest_path=run/'manifest.json'
                    absent=[]
                    if not manifest_path.is_file():absent.append(f'runs/{identity}/artifacts/manifest.json')
                    else:
                        if manifest_path.stat().st_size>1024**2:raise ServiceError('INVALID_BACKUP_DATABASE')
                        published=_object(manifest_path.read_text(encoding='utf-8'))
                        artifacts=published.get('artifacts')
                        if not isinstance(artifacts,dict) or len(artifacts)>64:raise ServiceError('INVALID_BACKUP_DATABASE')
                        for name,expected_hash in artifacts.items():
                            if not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}',name) or name in {'.','..'} or not isinstance(expected_hash,str) or not re.fullmatch(r'[0-9a-f]{64}',expected_hash):
                                raise ServiceError('INVALID_BACKUP_DATABASE')
                            local=run/name
                            if not local.is_file():absent.append(f'runs/{identity}/artifacts/{name}')
                            elif file_hash(local)!=expected_hash:raise ServiceError('BACKUP_HASH_MISMATCH')
                    if absent:
                        if not allow_missing:raise ServiceError('BACKUP_HASH_MISMATCH')
                        if missing_files is not None:missing_files.update(absent)
                if kind=='jobs' and missing_files:
                    absent=[name for name in missing_files if name.startswith('runs/'+identity+'/')]
                    if absent and item.get('artifacts_state')!='EXPIRED':
                        item.update(artifacts_state='MISSING',missing_artifacts=sorted(absent))
                if kind=='jobs' and item.get('experiment'):
                    linkage=item['experiment']
                    batch=entity('benchmarks',linkage['batch_id'])
                    if identity not in batch['jobs']:raise ServiceError('BACKUP_REFERENCE_MISSING')
                    if not any(cell['id']==linkage['cell_id'] for cell in batch.get('cells',[])):
                        raise ServiceError('BACKUP_REFERENCE_MISSING')
                    validate_experiment_batch(batch)
                if kind=='configs' and (item.get('derivation') or {}).get('source_job_id'):
                    reference('jobs',item['derivation']['source_job_id'])
                if kind=='experiment_plans':
                    validate_experiment_plan(item)
                if kind=='reports':
                    if item.get('job_id'):reference('jobs',item['job_id'])
                    elif item.get('benchmark_id'):reference('benchmarks',item['benchmark_id'])
                    else:raise ServiceError('BACKUP_REFERENCE_MISSING')
                if kind=='benchmarks':
                    for job_id in item['jobs']:reference('jobs',job_id)
                    if item.get('experiment_plan_id'):
                        validate_experiment_batch(item)
                target.put(kind,identity,item)
            for job_id,seq,raw in _rows(source,'SELECT job_id,seq,body FROM events ORDER BY job_id,seq'):
                reference('jobs',job_id)
                event=_object(raw)
                if type(seq) is not int or seq<1 or type(event.get('seq')) is not int or event['seq']!=seq:
                    raise ServiceError('INVALID_BACKUP_EVENT')
                target.db.execute('INSERT INTO events(job_id,seq,body) VALUES(?,?,?)',(job_id,seq,raw))
            for seq,raw in _rows(source,'SELECT seq,body FROM audit ORDER BY seq'):
                _object(raw)
                target.db.execute('INSERT INTO audit(seq,body) VALUES(?,?)',(seq,raw))
            for key,digest,job_id in _rows(source,'SELECT key,digest,job_id FROM idempotency'):
                reference('jobs',job_id)
                target.db.execute('INSERT INTO idempotency(key,digest,job_id) VALUES(?,?,?)',(key,digest,job_id))
        return count
    finally:
        if target is not None:target.close()
        source.close()
