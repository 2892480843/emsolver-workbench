"""Engineering routes enforce a byte budget before decoding request JSON."""
import json

from fastapi import APIRouter, Query, Request

from .engineering import EngineeringAssets
from .engineering_contracts import MAX_MANIFEST_BYTES
from .errors import ServiceError


router = APIRouter(prefix='/api/v1')


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result: raise ValueError('Duplicate field')
        result[key] = value
    return result


async def body(request, keys, limit=MAX_MANIFEST_BYTES):
    chunks = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit: raise ServiceError('ENGINEERING_MANIFEST_TOO_LARGE', status=413)
        chunks.append(chunk)
    try:
        value = json.loads(b''.join(chunks), object_pairs_hook=unique_object, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite')))
        if not isinstance(value, dict) or set(value) != set(keys): raise ValueError('Invalid fields')
        return value
    except (ValueError, UnicodeError, RecursionError): raise ServiceError('INVALID_ENGINEERING_REQUEST') from None


@router.get('/matrices/{matrix_id}/engineering')
def list_models(matrix_id: str, request: Request): return EngineeringAssets(request.app.state.service).list(matrix_id)


@router.post('/matrices/{matrix_id}/engineering', status_code=201)
async def create_model(matrix_id: str, request: Request):
    EngineeringAssets(request.app.state.service)._matrix(matrix_id, True)
    values = await body(request, ['manifest'])
    from starlette.concurrency import run_in_threadpool
    return await run_in_threadpool(EngineeringAssets(request.app.state.service).create, matrix_id, values['manifest'])


@router.post('/matrices/{matrix_id}/engineering/select')
async def select_model(matrix_id: str, request: Request):
    EngineeringAssets(request.app.state.service)._matrix(matrix_id, True)
    values = await body(request, ['asset_id'], 4096)
    if not isinstance(values['asset_id'], str): raise ServiceError('INVALID_ENGINEERING_REQUEST')
    return EngineeringAssets(request.app.state.service).select(matrix_id, values['asset_id'])


@router.post('/matrices/{matrix_id}/engineering/demo', status_code=201)
async def demo_model(matrix_id: str, request: Request):
    EngineeringAssets(request.app.state.service)._matrix(matrix_id, True)
    values = await body(request, ['authorized'], 4096)
    if values['authorized'] is not True: raise ServiceError('INVALID_ENGINEERING_REQUEST')
    return EngineeringAssets(request.app.state.service).create_demo(matrix_id)


@router.get('/engineering/{asset_id}')
def model(asset_id: str, request: Request): return EngineeringAssets(request.app.state.service)._public(EngineeringAssets(request.app.state.service).get(asset_id))


@router.get('/engineering/{asset_id}/manifest')
def manifest(asset_id: str, request: Request): return EngineeringAssets(request.app.state.service).manifest(asset_id)


@router.get('/jobs/{job_id}/field-preview')
def field(job_id: str, request: Request, mode: int = Query(0, ge=0), component: str = Query('magnitude'), limit: int = Query(512, ge=1, le=512)):
    return EngineeringAssets(request.app.state.service).field_preview(job_id, mode, component, limit)
