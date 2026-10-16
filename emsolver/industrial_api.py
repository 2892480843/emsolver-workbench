"""Project-authorized, bounded industrial acceptance API."""
import json

from fastapi import APIRouter, Query, Request
from starlette.concurrency import run_in_threadpool

from .engineering_api import unique_object
from .errors import ServiceError
from .industrial import IndustrialCases
from .industrial_contracts import MAX_MANIFEST_BYTES

router = APIRouter(prefix='/api/v1')


async def _manifest(request):
    chunks = []; size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_MANIFEST_BYTES: raise ServiceError('INDUSTRIAL_EVIDENCE_TOO_LARGE', status=413)
        chunks.append(chunk)
    try:
        value = json.loads(b''.join(chunks), object_pairs_hook=unique_object,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite')))
        if not isinstance(value, dict) or set(value) != {'manifest'}: raise ValueError('Invalid fields')
        return value['manifest']
    except (ValueError, UnicodeError, RecursionError): raise ServiceError('INVALID_INDUSTRIAL_REQUEST') from None


@router.get('/projects/{project_id}/industrial-template')
def template(project_id: str, request: Request, job_id: str = Query(min_length=1, max_length=200)):
    return IndustrialCases(request.app.state.service).template(project_id, job_id)


@router.get('/projects/{project_id}/industrial-cases')
def cases(project_id: str, request: Request, limit: int = Query(20, ge=1, le=100), cursor: str | None = Query(None, max_length=1024)):
    return IndustrialCases(request.app.state.service).list(project_id, limit=limit, cursor=cursor)


@router.post('/projects/{project_id}/industrial-cases', status_code=201)
async def create(project_id: str, request: Request):
    ledger = IndustrialCases(request.app.state.service); ledger._project(project_id, 'admin')
    return await run_in_threadpool(ledger.create, project_id, await _manifest(request))


@router.get('/industrial-cases/{case_id}')
def case(case_id: str, request: Request): return IndustrialCases(request.app.state.service).get(case_id)


@router.get('/industrial-cases/{case_id}/dossier')
def dossier(case_id: str, request: Request): return IndustrialCases(request.app.state.service).dossier(case_id)


@router.post('/industrial-cases/{case_id}/revisions', status_code=201)
async def revise(case_id: str, request: Request):
    ledger = IndustrialCases(request.app.state.service)
    record = ledger._record('industrial_cases', case_id); ledger._project(record['project_id'], 'admin')
    return await run_in_threadpool(ledger.revise, case_id, await _manifest(request))


@router.post('/industrial-cases/{case_id}/checks', status_code=201)
def check(case_id: str, request: Request): return IndustrialCases(request.app.state.service).check(case_id)


@router.get('/industrial-cases/{case_id}/checks')
def checks(case_id: str, request: Request, limit: int = Query(20, ge=1, le=100)):
    return IndustrialCases(request.app.state.service).checks(case_id, limit=limit)


@router.get('/industrial-checks/{check_id}')
def check_record(check_id: str, request: Request): return IndustrialCases(request.app.state.service).check_record(check_id)


@router.get('/industrial-cases/{case_id}/report')
def report(case_id: str, request: Request): return IndustrialCases(request.app.state.service).export(case_id)
