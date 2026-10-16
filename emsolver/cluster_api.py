"""Authenticated control-plane and remote-node transport adapters."""
from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path
import uuid

from fastapi import APIRouter, File, Form, Header, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from .access import require_admin
from .errors import ServiceError
from .integrity import file_hash

router = APIRouter()


def upload_limit():
    try:
        limit = int(os.environ.get('EMSOLVER_CLUSTER_MAX_UPLOAD_BYTES', str(1024**3)))
    except ValueError:
        raise ServiceError('CLUSTER_CONFIGURATION_INVALID', status=503) from None
    if not 1024**2 <= limit <= 16 * 1024**3:
        raise ServiceError('CLUSTER_CONFIGURATION_INVALID', status=503)
    return limit


class ClusterRequestLimit:
    """Bound streamed bodies before multipart parsing can consume unbounded disk."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get('path', '')
        if scope['type'] != 'http' or not path.startswith('/internal/v1/cluster/'):
            return await self.app(scope, receive, send)
        limit = upload_limit() if path.endswith('/complete') else 65536
        received = 0

        async def bounded_receive():
            nonlocal received
            message = await receive()
            if message['type'] == 'http.request':
                received += len(message.get('body', b''))
                if received > limit:
                    raise HTTPException(413, 'Cluster request exceeds its budget')
            return message

        await self.app(scope, bounded_receive, send)


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class Enrollment(Strict):
    name: str = Field(min_length=1, max_length=200)
    project_ids: list[str] = Field(min_length=1, max_length=1000)
    resources: dict
    backends: list[str] = Field(default_factory=lambda: ['scipy'], max_length=16)


class Claim(Strict):
    capabilities: dict


class Lease(Strict):
    lease_token: str = Field(min_length=16, max_length=256)
    attempt: int = Field(ge=1, le=3)


class Heartbeat(Lease):
    metrics: dict | None = None


class Failure(Lease):
    code: str = Field(pattern=r'^[A-Z][A-Z_]{2,63}$')


def _service(request):
    return request.app.state.service


def _coordinator(request):
    service = _service(request)
    if getattr(service, 'execution_mode', 'local') != 'cluster':
        raise ServiceError('CLUSTER_MODE_REQUIRED', 'Cluster execution is not enabled', 409)
    return service.cluster


def _worker(request):
    worker = getattr(request.state, 'cluster_worker', None)
    if worker is None:
        raise ServiceError('WORKER_UNAUTHORIZED', status=401)
    return worker['id']


@router.get('/api/v1/cluster/status')
def status(request: Request):
    service = _service(request)
    mode = getattr(service, 'execution_mode', 'local')
    return {'enabled': mode == 'cluster', 'execution_mode': mode,
            'persistence_backend': getattr(service.store, 'dialect', 'sqlite'),
            'coordinator_available': mode == 'cluster' and not getattr(request.app.state, 'cluster_reaper_error', None)}


@router.get('/api/v1/cluster/workers')
def workers(request: Request):
    require_admin()
    return _coordinator(request).workers()


@router.post('/api/v1/cluster/workers', status_code=201)
def enroll(body: Enrollment, request: Request):
    require_admin()
    return _coordinator(request).enroll(**body.model_dump())


@router.post('/api/v1/cluster/workers/{worker_id}/revoke')
def revoke(worker_id: str, request: Request):
    require_admin()
    return _coordinator(request).revoke(worker_id)


@router.get('/api/v1/cluster/jobs/{job_id}/lease')
def lease_status(job_id: str, request: Request):
    _service(request).get_job(job_id)
    return _coordinator(request).lease_status(job_id)


@router.post('/internal/v1/cluster/claim')
def claim(body: Claim, request: Request):
    service, coordinator, worker_id = _service(request), _coordinator(request), _worker(request)
    assignment = coordinator.claim(worker_id, body.capabilities)
    if assignment is None:
        return {'job': None}
    job = assignment['job']
    matrix = service._get('matrices', job['matrix_id'])
    path = Path(matrix['_path'])
    if path.is_symlink() or not path.is_file():
        coordinator.finish(worker_id, job['id'], assignment['lease_token'], assignment['attempt'],
            hashlib.sha256(b'INPUT_UNAVAILABLE').hexdigest(),
            lambda: service._finish(job['id'], 'FAILED', 'INPUT_UNAVAILABLE'))
        raise ServiceError('INPUT_UNAVAILABLE', status=409)
    keys = ('id', 'name', 'project_id', 'matrix_id', 'matrix_hash', 'config',
            'status', 'stage', 'created_at', 'execution', 'resumption')
    result = {'job': {key: job[key] for key in keys if key in job},
              'lease': {'token': assignment['lease_token'], 'attempt': assignment['attempt'],
                        'expires_at': assignment['expires_at'],
                        'lease_seconds': max(0,assignment['expires_at']-coordinator._now())},
              'input': {'sha256': job['matrix_hash'], 'bytes': path.stat().st_size,
                        'filename': 'input' + path.suffix,
                        'url': f"/internal/v1/cluster/jobs/{job['id']}/input"}}
    if job.get('resumption'):
        resume = job['resumption']
        exact = resume.get('exact_resume') is True
        result['checkpoint'] = {'sha256': resume['checkpoint_sha256'],
                                'kind': 'exact_block' if exact else 'subspace',
                                'url': f"/internal/v1/cluster/jobs/{job['id']}/checkpoint"}
    return result


@router.post('/internal/v1/cluster/jobs/{job_id}/heartbeat')
def heartbeat(job_id: str, body: Heartbeat, request: Request):
    coordinator=_coordinator(request)
    result=coordinator.heartbeat(_worker(request), job_id, **body.model_dump())
    result['lease_seconds']=max(0,result['expires_at']-coordinator._now())
    return result


def _leased_job(request, job_id, lease_token, attempt):
    return _coordinator(request).require_lease(_worker(request), job_id, lease_token, attempt)


@router.get('/internal/v1/cluster/jobs/{job_id}/input')
def input_file(job_id: str, request: Request,
               lease_token: str = Header(alias='X-EMSolver-Lease'),
               attempt: int = Header(alias='X-EMSolver-Attempt', ge=1, le=3)):
    job = _leased_job(request, job_id, lease_token, attempt)
    path = Path(_service(request)._get('matrices', job['matrix_id'])['_path'])
    if path.is_symlink() or not path.is_file() or file_hash(path) != job['matrix_hash']:
        raise ServiceError('INPUT_HASH_MISMATCH', status=409)
    return FileResponse(path, media_type='application/octet-stream', filename='input' + path.suffix)


@router.get('/internal/v1/cluster/jobs/{job_id}/checkpoint')
def checkpoint_file(job_id: str, request: Request,
                    lease_token: str = Header(alias='X-EMSolver-Lease'),
                    attempt: int = Header(alias='X-EMSolver-Attempt', ge=1, le=3)):
    job = _leased_job(request, job_id, lease_token, attempt)
    resume = job.get('resumption')
    if not resume:
        raise ServiceError('CHECKPOINT_UNAVAILABLE', status=404)
    name = 'exact-block.checkpoint.npz' if resume.get('exact_resume') else 'subspace.checkpoint.npz'
    path = _service(request).download(resume['source_job_id'], name)
    if Path(path).is_symlink() or file_hash(path) != resume['checkpoint_sha256']:
        raise ServiceError('CHECKPOINT_INCOMPATIBLE', status=409)
    return FileResponse(path, media_type='application/octet-stream', filename=name)


def _copy_upload(source, path, maximum):
    count = 0
    try:
        with path.open('xb') as destination:
            path.chmod(0o600)
            while block := source.read(1024**2):
                count += len(block)
                if count > maximum:
                    raise ServiceError('INPUT_TOO_LARGE', status=413)
                destination.write(block)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


@router.post('/internal/v1/cluster/jobs/{job_id}/complete')
async def complete(job_id: str, request: Request, bundle: UploadFile = File(...),
                   lease_token: str = Form(..., min_length=16, max_length=256),
                   attempt: int = Form(..., ge=1, le=3)):
    from .cluster_results import ClusterResultReceiver
    service = _service(request)
    path = service.root / 'inbox' / ('cluster-' + uuid.uuid4().hex + '.zip')
    try:
        submission=getattr(request.state,'cluster_submission',None)
        if (submission is None or submission[0]!=job_id or submission[2]!=attempt
                or not hmac.compare_digest(submission[1],lease_token)):
            raise ServiceError('LEASE_MISMATCH',status=409)
        await run_in_threadpool(_coordinator(request).require_lease, _worker(request), job_id,
                                lease_token, attempt, allow_completed=True)
        await run_in_threadpool(_copy_upload, bundle.file, path, upload_limit())
        return await run_in_threadpool(ClusterResultReceiver(service).complete, _worker(request),
                                       job_id, lease_token, attempt, path)
    finally:
        await bundle.close()
        path.unlink(missing_ok=True)


@router.post('/internal/v1/cluster/jobs/{job_id}/failure')
def failure(job_id: str, body: Failure, request: Request):
    service = _service(request)
    digest = hashlib.sha256(('failure:' + body.code).encode()).hexdigest()
    return _coordinator(request).finish(_worker(request), job_id, body.lease_token,
        body.attempt, digest, lambda: service._finish(job_id, 'FAILED', body.code))
