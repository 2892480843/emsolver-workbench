"""Project policy validation, logical quotas and safe lifecycle planning."""
from __future__ import annotations
from pathlib import Path
from typing import Literal

from pydantic import BaseModel,ConfigDict,Field
from .errors import ServiceError
from .jobs import ACTIVE
from .store import utcnow


class ProjectPolicy(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,allow_inf_nan=False)
    storage_limit_gib:float=Field(default=4096,gt=0,le=65536)
    max_queued_jobs:int=Field(default=100,ge=1,le=100)
    priority:int=Field(default=0,ge=-10,le=10)
    retention_days:int=Field(default=30,ge=1,le=36500)


class ProjectMember(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    subject:str=Field(min_length=1,max_length=128,pattern=r'^[A-Za-z0-9_.@-]+$')
    role:Literal['viewer','analyst','admin']


def validated_policy(value=None):
    try:return ProjectPolicy.model_validate(value or {}).model_dump(mode='json')
    except (ValueError,TypeError):raise ServiceError('INVALID_PROJECT_POLICY') from None


def validated_members(value):
    if not isinstance(value,list) or len(value)>1000:raise ServiceError('INVALID_PROJECT_MEMBERS')
    try:members=[ProjectMember.model_validate(member).model_dump() for member in value]
    except (ValueError,TypeError):raise ServiceError('INVALID_PROJECT_MEMBERS') from None
    if len({member['subject'] for member in members})!=len(members):raise ServiceError('INVALID_PROJECT_MEMBERS')
    return members


def directory_bytes(path):
    return sum(file.stat().st_size for file in Path(path).rglob('*') if file.is_file() and not file.is_symlink()) if Path(path).is_dir() else 0


class ProjectGovernance:
    def __init__(self,gateway):self.gateway=gateway

    def _rows(self,kind,project_id):
        cursor=None
        while True:
            page=self.gateway.store.page(kind,project_id=project_id,limit=200,cursor=cursor)
            yield from page['items']
            cursor=page['next_cursor']
            if not cursor:break

    def _storage(self,project_id):
        service=self.gateway
        cached=service.store.get('project_storage',project_id)
        if cached is not None:return cached
        # Reconcile legacy data once. Later hot paths update this projection in
        # the same transaction as asset registration and job finalization.
        seen=set();inputs=0;outputs=0
        for matrix in self._rows('matrices',project_id):
            if matrix['hash'] in seen:continue
            seen.add(matrix['hash']);path=Path(matrix['_path'])
            inputs+=path.stat().st_size if path.is_file() else int(matrix.get('storage_bytes') or 0)
        for job in self._rows('jobs',project_id):
            if job['status'] not in ACTIVE:outputs+=directory_bytes(service.root/'runs'/job['id'])
        engineering_seen=set()
        for asset in self._rows('engineering_assets',project_id):
            sha=asset.get('sidecar_sha256')
            if sha in engineering_seen:continue
            engineering_seen.add(sha);path=Path(asset['_path'])
            if path.is_file():inputs+=path.stat().st_size
        industrial_seen=set()
        for kind, field in (('industrial_cases','dossier_sha256'),('industrial_checks','check_sha256'),('industrial_reference_assets','sha256')):
            for asset in self._rows(kind,project_id):
                sha=asset.get(field)
                if sha in industrial_seen:continue
                industrial_seen.add(sha);path=Path(asset['_path'])
                if path.is_file() and not path.is_symlink():inputs+=path.stat().st_size
        cached={'id':project_id,'project_id':project_id,'input_bytes':inputs,'output_bytes':outputs,'updated_at':utcnow()}
        service.store.put('project_storage',project_id,cached)
        return cached

    def add_bytes(self,project_id,*,inputs=0,outputs=0):
        item=self._storage(project_id)
        item.update(input_bytes=max(0,item['input_bytes']+inputs),output_bytes=max(0,item['output_bytes']+outputs),updated_at=utcnow())
        self.gateway.store.put('project_storage',project_id,item)

    def usage(self,project_id):
        service=self.gateway;project=service._project(project_id)
        return self._usage_for_project(project)

    def _usage_for_project(self,project):
        # Operations policy views authorize separately and expose only quotas.
        service=self.gateway;project_id=project['id']
        policy=validated_policy(project.get('policy'))
        # A first read may initialize the legacy storage projection. Acquire a
        # write transaction up front rather than upgrading a SQLite read lock.
        with service.store.transaction():
            cached=self._storage(project_id)
            jobs=service.store.select('jobs',project_id=project_id,status=ACTIVE,limit=1000)
            reserved=sum(job.get('config',{}).get('resources',{}).get('disk_limit_gib',0)*1024**3 for job in jobs)
            return {'project_id':project_id,'input_bytes':cached['input_bytes'],'output_bytes':cached['output_bytes'],
                'reserved_output_bytes':reserved,'total_bytes':cached['input_bytes']+cached['output_bytes'],
                'storage_limit_bytes':policy['storage_limit_gib']*1024**3,
                'queued_jobs':service.store.count('jobs',project_id=project_id,status='QUEUED'),'policy':policy}

    def reserve(self,project_id,*,input_bytes=0,output_gib=0,queue=False):
        usage=self.usage(project_id)
        if queue and usage['queued_jobs']>=usage['policy']['max_queued_jobs']:
            raise ServiceError('PROJECT_QUEUE_LIMIT','Project queued-job quota exceeded',429)
        if usage['total_bytes']+usage['reserved_output_bytes']+input_bytes+output_gib*1024**3>usage['storage_limit_bytes']:
            raise ServiceError('PROJECT_STORAGE_QUOTA','Project storage quota exceeded',409)
