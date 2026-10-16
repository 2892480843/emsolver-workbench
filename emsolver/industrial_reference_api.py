"""Project-admin, streaming and bounded independent-reference asset upload."""
from pathlib import Path
import tempfile

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from .errors import ServiceError
from .industrial_reference import IndustrialReferenceAssets, MAX_UPLOAD_BYTES


router = APIRouter(prefix='/api/v1')


@router.post('/projects/{project_id}/industrial-reference-assets', status_code=201)
async def create(project_id: str, request: Request):
    assets = IndustrialReferenceAssets(request.app.state.service)
    assets._project(project_id, True)
    if assets.inbox.is_symlink(): raise ServiceError('INDUSTRIAL_REFERENCE_INVALID')
    size = 0

    async def bounded_stream():
        nonlocal size
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES + 65536: raise MultiPartException('Reference request size exceeded')
            yield chunk

    form = None
    temporary = None
    try:
        parser = MultiPartParser(request.headers, bounded_stream(), max_files=1, max_fields=0)
        form = await parser.parse()
        if len(form.multi_items()) != 1 or 'file' not in form or not isinstance(form['file'], UploadFile):
            raise ServiceError('INDUSTRIAL_REFERENCE_INVALID')
        upload = form['file']
        if not upload.filename or Path(upload.filename).suffix.lower() != '.npz': raise ServiceError('INDUSTRIAL_REFERENCE_INVALID')
        with tempfile.NamedTemporaryFile(prefix='reference-', suffix='.npz', dir=assets.inbox, delete=False) as output:
            temporary = Path(output.name)
            count = 0
            while chunk := await upload.read(1024**2):
                count += len(chunk)
                if count > MAX_UPLOAD_BYTES: raise ServiceError('INDUSTRIAL_REFERENCE_TOO_LARGE', status=413)
                output.write(chunk)
        return await run_in_threadpool(assets.create, project_id, temporary)
    except MultiPartException as exc:
        raise ServiceError('INDUSTRIAL_REFERENCE_INVALID', 'Invalid or oversized multipart reference upload', status=413) from exc
    finally:
        if form is not None: await form.close()
        if temporary is not None: temporary.unlink(missing_ok=True)


@router.get('/industrial-reference-assets/{asset_id}')
def get(asset_id: str, request: Request):
    return IndustrialReferenceAssets(request.app.state.service).get(asset_id)


@router.get('/industrial-reference-assets/{asset_id}/download')
def download(asset_id: str, request: Request):
    assets = IndustrialReferenceAssets(request.app.state.service)
    return FileResponse(assets.path(asset_id), media_type='application/octet-stream', filename=asset_id + '.npz')
