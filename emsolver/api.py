"""FastAPI adapter. Importing this module does not start a worker or create data."""
from __future__ import annotations
from contextlib import asynccontextmanager, suppress
import asyncio
import hmac
import os
from pathlib import Path
import uuid
from typing import Any, Literal
from fastapi import FastAPI, File, Form, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field
from .service import Service, ServiceError
from .jobs import JobStatus
from .access import Identity,authenticate,identity_context,current_identity,configured_users
from .experiments import ExperimentSpec
from . import __version__


class Strict(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)


class ProjectCreate(Strict):
    name: str=Field(min_length=1,max_length=200)
    description: str=''


class ProjectPatch(Strict):
    expected_version: int|None=Field(default=None,ge=1)
    name: str|None=Field(default=None,min_length=1,max_length=200)
    description: str|None=None
    archived: bool|None=None
    classification: Literal['public','internal','confidential']|None=None
    members: list[dict[str,Any]]|None=Field(default=None,max_length=1000)
    policy: dict[str,Any]|None=None


class OperationsPolicyPatch(Strict):
    project_id:str=Field(min_length=1,max_length=200)
    policy:dict[str,Any]


class SampleCreate(Strict):
    project_id: str
    kind: Literal['indefinite','clustered','repeated','near_singular','complex']='indefinite'
    n: int=Field(default=500,ge=4,le=200000)
    name: str|None=None
    seed: int=Field(default=20261016,ge=0)


class MatrixImport(Strict):
    project_id: str
    path: str
    name: str|None=None
    storage: Literal['full','upper','lower']='full'
    metadata: dict[str,Any]|None=None


class MatrixPatch(Strict):
    expected_version: int|None=Field(default=None,ge=1)
    name: str|None=Field(default=None,min_length=1,max_length=200)
    metadata: dict[str,Any]|None=None


class SelectionExport(Strict):
    indices: list[int]=Field(min_length=1,max_length=256)
    format: Literal['numpy','scipy']='numpy'


class CleanupRequest(Strict):
    dry_run: bool=True


class ConfigCreate(Strict):
    config: dict[str,Any]


class AdviceCreate(Strict):
    job_id: str|None=None
    config: dict[str,Any]|None=None
    changes: dict[str,Any]


class ExperimentSubmit(Strict):
    experiment_plan_id: str=Field(min_length=1,max_length=200)


class PlanCreate(Strict):
    config: dict[str,Any]|None=None
    config_id: str|None=None
    project_id: str|None=None


class JobCreate(Strict):
    plan_id: str
    idempotency_key: str|None=Field(default=None,max_length=200)


class RestartRequest(Strict):
    use_checkpoint: bool=False
    resume_mode: Literal['subspace','exact_block']='subspace'


class RetentionRequest(Strict):
    retained: bool


class BenchmarkCreate(Strict):
    project_id: str
    matrix_ids: list[str]=Field(min_length=1,max_length=30)
    k: int=Field(default=6,ge=1)
    sigma: float=0.0
    repeats: int=Field(default=3,ge=1,le=10)


class DiagnoseCreate(Strict):
    job_id: str|None=None
    config: dict[str,Any]|None=None


class ReportCreate(Strict):
    job_id: str|None=None
    benchmark_id: str|None=None


class CompareCreate(Strict):
    job_ids: list[str]=Field(min_length=2,max_length=20)


class EffectLink(Strict):
    job_id:str


class MatrixDerive(Strict):
    triangle:Literal['upper','lower']
    name:str|None=Field(default=None,max_length=200)


class ModeCompare(Strict):
    candidate_job_id:str
    reference_job_id:str


def create_app(root=None,start_runner=True,execution_mode=None):
    @asynccontextmanager
    async def lifespan(app):
        app.state.service=Service(root,start_runner=start_runner,execution_mode=execution_mode)
        supervisor=None
        if app.state.service.execution_mode=='cluster':
            async def reap():
                while True:
                    try:
                        with identity_context(Identity('cluster-supervisor',True,'cluster-supervisor')):
                            await run_in_threadpool(app.state.service.cluster.reap_expired)
                        app.state.cluster_reaper_error=None
                    except Exception:
                        app.state.cluster_reaper_error='COORDINATOR_UNAVAILABLE'
                    await asyncio.sleep(min(5,app.state.service.cluster.lease_seconds/4))
            supervisor=asyncio.create_task(reap())
        try: yield
        finally:
            if supervisor is not None:
                supervisor.cancel()
                with suppress(asyncio.CancelledError):await supervisor
            app.state.service.close()

    app=FastAPI(title='EMSolver Workbench',version=__version__,lifespan=lifespan)
    # Keep the body limiter inside the authentication middleware. Its receive
    # exception must reach the parser directly, without a task-group wrapper
    # converting HTTP 413 into a generic body-parsing error.
    from .cluster_api import ClusterRequestLimit
    app.add_middleware(ClusterRequestLimit)

    @app.middleware('http')
    async def security(request:Request,call_next):
        if request.url.path.startswith('/internal/v1/cluster/'):
            try:
                instance=request.app.state.service
                if getattr(instance,'execution_mode','local')!='cluster':
                    raise ServiceError('CLUSTER_MODE_REQUIRED','Cluster execution is not enabled',409)
                authorization=request.headers.get('Authorization','')
                token=authorization[7:] if authorization.startswith('Bearer ') else ''
                worker=await run_in_threadpool(instance.cluster.authenticate,token)
                request.state.cluster_worker=worker
                from .cluster_api import upload_limit
                length=request.headers.get('Content-Length')
                maximum=upload_limit() if request.url.path.endswith('/complete') else 65536
                if length and (not length.isdigit() or int(length)>maximum):
                    raise ServiceError('INPUT_TOO_LARGE','Cluster request exceeds its budget',413)
                admission=None
                if request.url.path.endswith('/complete'):
                    from .cluster_admission import acquire_admission
                    parts=request.url.path.split('/')
                    job_id=parts[-2]
                    lease_token=request.headers.get('X-EMSolver-Lease','')
                    attempt=request.headers.get('X-EMSolver-Attempt','')
                    if attempt not in {'1','2','3'} or not 16<=len(lease_token)<=256:
                        raise ServiceError('LEASE_HEADERS_REQUIRED',status=422)
                    attempt=int(attempt)
                    await run_in_threadpool(instance.cluster.require_lease,worker['id'],job_id,
                                            lease_token,attempt,allow_completed=True)
                    admission=await run_in_threadpool(acquire_admission,instance,worker['id'],job_id,attempt)
                    request.state.cluster_submission=(job_id,lease_token,attempt)
            except ServiceError as exc:
                return JSONResponse({'error':{'code':exc.code,'message':exc.message}},status_code=exc.status)
            try:
                with identity_context(Identity('worker:'+worker['id'],True,'cluster-worker')):
                    if admission is None:response=await call_next(request)
                    else:
                        with admission.bind():response=await call_next(request)
            finally:
                if admission is not None:await run_in_threadpool(admission.close)
        elif request.url.path.startswith('/api/'):
            try:actor=authenticate(request.headers.get('Authorization',''))
            except ServiceError as exc:
                if hasattr(request.app.state,'service'):
                    with identity_context(Identity('anonymous',False,'denied')):
                        request.app.state.service.store.audit('authorization.denied',result='denied')
                return JSONResponse({'error':{'code':exc.code,'message':exc.message}},status_code=exc.status,headers={'WWW-Authenticate':'Bearer'})
            length=request.headers.get('Content-Length')
            if length and (not length.isdigit() or int(length)>1024**3+1024*1024):
                return JSONResponse({'error':{'code':'INPUT_TOO_LARGE','message':'Request exceeds import budget'}},status_code=413)
            with identity_context(actor):
                response=await call_next(request)
                if response.status_code==403:
                    request.app.state.service.store.audit('authorization.denied',request.url.path,result='denied')
        else:response=await call_next(request)
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Cache-Control']='no-store'
        return response

    @app.exception_handler(ServiceError)
    async def service_error(request,exc):
        return JSONResponse({'error':{'code':exc.code,'message':exc.message}},status_code=exc.status)

    @app.exception_handler(Exception)
    async def internal_error(request,exc):
        return JSONResponse({'error':{'code':'INTERNAL_ERROR','message':'The operation failed unexpectedly; no internal paths or inputs are exposed'}},status_code=500)

    @app.exception_handler(RequestValidationError)
    async def request_error(request,exc):
        return JSONResponse({'error':{'code':'INVALID_REQUEST','message':'Request validation failed','fields':[{'location':list(e['loc']),'type':e['type']} for e in exc.errors()]}},status_code=422)

    def service(request): return request.app.state.service
    prefix='/api/v1'

    @app.get(prefix+'/projects')
    def projects(request:Request,q:str|None=Query(None,max_length=200)): return service(request).list_projects(q)

    @app.get(prefix+'/identity')
    def identity(request:Request):
        actor=current_identity()
        return {'subject':actor.subject,'global_admin':actor.global_admin,'operations_admin':actor.operations_admin,'mode':actor.mode,
                'configured_subjects':[user['subject'] for user in configured_users()] if actor.global_admin else [actor.subject]}

    @app.get(prefix+'/projects/{project_id}/usage')
    def project_usage(project_id:str,request:Request):return service(request).project_usage(project_id)

    @app.get(prefix+'/operations/summary')
    def operations_summary(request:Request):
        from .operations import Operations
        return Operations(service(request)).summary()

    @app.get(prefix+'/operations/project-policies')
    def operations_policies(request:Request,limit:int=Query(50,ge=1,le=200),cursor:str|None=Query(None,max_length=1024)):
        from .operations import Operations
        return Operations(service(request)).policies(limit=limit,cursor=cursor)

    @app.patch(prefix+'/operations/project-policies')
    def operations_policy(body:OperationsPolicyPatch,request:Request):
        from .operations import Operations
        return Operations(service(request)).update_policy(body.project_id,body.policy)

    @app.post(prefix+'/projects',status_code=201)
    def create_project(body:ProjectCreate,request:Request): return service(request).create_project(**body.model_dump())

    @app.patch(prefix+'/projects/{project_id}')
    def patch_project(project_id:str,body:ProjectPatch,request:Request): return service(request).update_project(project_id,**body.model_dump(exclude_unset=True))

    @app.get(prefix+'/matrices')
    def matrices(request:Request): return service(request).list_matrices()

    @app.get(prefix+'/matrices/{matrix_id}')
    def matrix(matrix_id:str,request:Request): return service(request).get_matrix(matrix_id)

    @app.get(prefix+'/matrices/{matrix_id}/diagnostics')
    def matrix_diagnostics(matrix_id:str,request:Request): return service(request).get_matrix(matrix_id)['diagnostics']

    async def receive_upload(request,file,project_id,metadata):
        import json
        app_service=service(request);path=None
        try:
            app_service._project(project_id,True)
            filename=file.filename or ''
            if not filename or '/' in filename or '\\' in filename or Path(filename).suffix.lower() not in {'.npz','.npy'}:
                raise ServiceError('UNSAFE_FILENAME')
            try:parsed=app_service._validated_metadata(json.loads(metadata)) if metadata else None
            except (ValueError,TypeError):raise ServiceError('INVALID_METADATA') from None
            path=app_service.root/'inbox'/('upload_'+uuid.uuid4().hex+Path(filename).suffix.lower())
            size=0
            descriptor=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
            with os.fdopen(descriptor,'wb') as output:
                while block:=await file.read(1024*1024):
                    size+=len(block)
                    if size>1024**3:raise ServiceError('INPUT_TOO_LARGE',status=413)
                    output.write(block)
                    if await request.is_disconnected():raise ServiceError('UPLOAD_CANCELLED',status=499)
            return path,filename,parsed
        except BaseException:
            if path is not None:path.unlink(missing_ok=True)
            raise
        finally:await file.close()

    @app.post(prefix+'/matrices',status_code=201)
    async def upload(request:Request,file:UploadFile=File(...),project_id:str=Form(...),name:str|None=Form(None),storage:Literal['full','upper','lower']=Form('full'),metadata:str|None=Form(None)):
        path,filename,parsed=await receive_upload(request,file,project_id,metadata)
        try:return await run_in_threadpool(service(request)._ingest,project_id,path,name or filename,storage,metadata=parsed)
        finally:path.unlink(missing_ok=True)

    @app.post(prefix+'/imports',status_code=202)
    async def streamed_import(request:Request,file:UploadFile=File(...),project_id:str=Form(...),name:str|None=Form(None),storage:Literal['full','upper','lower']=Form('full'),metadata:str|None=Form(None)):
        path,filename,parsed=await receive_upload(request,file,project_id,metadata)
        try:return await run_in_threadpool(service(request).import_manager.submit,project_id,path,filename,name,storage,parsed)
        finally:path.unlink(missing_ok=True)

    @app.get(prefix+'/imports')
    def imports(request:Request,project_id:str|None=None):
        project_id=service(request)._scope(project_id)
        return service(request).import_manager.list(project_id)

    @app.get(prefix+'/imports/{import_id}')
    def import_task(import_id:str,request:Request):return service(request).import_manager.get(import_id)

    @app.post(prefix+'/imports/{import_id}/cancel')
    def cancel_import(import_id:str,request:Request):return service(request).import_manager.cancel(import_id)

    @app.post(prefix+'/matrices/import',status_code=201)
    def local_import(body:MatrixImport,request:Request): return service(request).import_matrix(**body.model_dump())

    @app.post(prefix+'/samples',status_code=201)
    def sample(body:SampleCreate,request:Request): return service(request).generate_sample(**body.model_dump())

    @app.post(prefix+'/configs',status_code=201)
    def config(body:ConfigCreate,request:Request): return service(request).create_config(body.config)

    @app.get(prefix+'/configs')
    def configs(request:Request): return service(request).list_configs()

    @app.post(prefix+'/configs/advice',status_code=201)
    def advice(body:AdviceCreate,request:Request): return service(request).apply_advice(**body.model_dump())

    @app.get(prefix+'/configs/{config_id}')
    def get_config(config_id:str,request:Request): return service(request)._get('configs',config_id)

    @app.post(prefix+'/plans',status_code=201)
    def plan(body:PlanCreate,request:Request): return service(request).create_plan(**body.model_dump())

    @app.get(prefix+'/plans/{plan_id}')
    def get_plan(plan_id:str,request:Request): return service(request)._get('plans',plan_id)

    @app.post(prefix+'/jobs',status_code=201)
    def submit(body:JobCreate,request:Request):
        key=body.idempotency_key or request.headers.get('Idempotency-Key')
        return service(request).submit_job(body.plan_id,key)

    @app.get(prefix+'/jobs')
    def jobs(request:Request): return service(request).list_jobs()

    @app.get(prefix+'/jobs/page')
    def job_page(request:Request,limit:int=Query(50,ge=1,le=200),
                 cursor:str|None=Query(None,max_length=1024),
                 project_id:str|None=Query(None,min_length=1,max_length=200),
                 status:JobStatus|None=None):
        return service(request).job_page(limit=limit,cursor=cursor,project_id=project_id,status=status)

    @app.get(prefix+'/workspace/summary')
    def workspace_summary(request:Request,project_id:str|None=Query(None,min_length=1,max_length=200)):
        return service(request).workspace_summary(project_id)

    @app.get(prefix+'/jobs/{job_id}')
    def job(job_id:str,request:Request): return service(request).get_job(job_id)

    @app.post(prefix+'/jobs/{job_id}/cancel')
    def cancel(job_id:str,request:Request): return service(request).cancel_job(job_id)

    @app.post(prefix+'/jobs/{job_id}/restart',status_code=201)
    def restart(job_id:str,request:Request,body:RestartRequest|None=None):
        return service(request).restart_job(job_id,use_checkpoint=bool(body and body.use_checkpoint),
                                           resume_mode=body.resume_mode if body else 'subspace')

    @app.patch(prefix+'/jobs/{job_id}/retention')
    def retained(job_id:str,body:RetentionRequest,request:Request):return service(request).set_retained(job_id,body.retained)

    @app.post(prefix+'/lifecycle')
    def lifecycle(body:CleanupRequest,request:Request):return service(request).lifecycle_cleanup(body.dry_run)

    @app.get(prefix+'/metrics')
    def metrics(request:Request):return Response(service(request).metrics_text(),media_type='application/openmetrics-text; version=1.0.0; charset=utf-8')

    @app.get(prefix+'/jobs/{job_id}/events')
    def events(job_id:str,request:Request,after:int=Query(0,ge=0)): return service(request).events(job_id,after)

    @app.get(prefix+'/jobs/{job_id}/events/page')
    def event_page(job_id:str,request:Request,after:int=Query(0,ge=0,le=9223372036854775807),
                   limit:int=Query(200,ge=1,le=1000)):
        return service(request).event_page(job_id,after,limit)

    @app.get(prefix+'/jobs/{job_id}/results')
    def results(job_id:str,request:Request): return service(request).results(job_id)

    @app.get(prefix+'/jobs/{job_id}/download')
    def download(job_id:str,request:Request,file:str=Query(...)):
        return FileResponse(service(request).download(job_id,file),filename=file)

    @app.get(prefix+'/jobs/{job_id}/bundle')
    def bundle(job_id:str,request:Request):
        return FileResponse(service(request).create_bundle(job_id),filename=job_id+'-public.zip')

    @app.post(prefix+'/compare')
    def compare(body:CompareCreate,request:Request): return service(request).compare_jobs(body.job_ids)

    @app.get(prefix+'/compare')
    def get_compare(request:Request,job_ids:list[str]=Query(...)): return service(request).compare_jobs(job_ids)

    @app.post(prefix+'/benchmarks',status_code=201)
    def benchmark(body:BenchmarkCreate,request:Request): return service(request).create_benchmark(**body.model_dump())

    @app.post(prefix+'/experiments/plans',status_code=201)
    def experiment_plan(body:ExperimentSpec,request:Request):
        return service(request).create_experiment_plan(body.model_dump(mode='json'))

    @app.get(prefix+'/experiments/plans/{plan_id}')
    def get_experiment_plan(plan_id:str,request:Request): return service(request)._get('experiment_plans',plan_id)

    @app.post(prefix+'/experiments/submit',status_code=201)
    def experiment_submit(body:ExperimentSubmit,request:Request):
        return service(request).submit_experiment(body.experiment_plan_id)

    @app.get(prefix+'/benchmarks')
    def benchmarks(request:Request): return service(request).list_benchmarks()

    @app.get(prefix+'/benchmarks/page')
    def benchmark_page(request:Request,limit:int=Query(20,ge=1,le=200),
                       cursor:str|None=Query(None,max_length=1024),
                       project_id:str|None=Query(None,min_length=1,max_length=200)):
        return service(request).benchmark_page(limit=limit,cursor=cursor,project_id=project_id)

    @app.get(prefix+'/benchmarks/{batch_id}')
    def get_benchmark(batch_id:str,request:Request): return service(request).get_benchmark(batch_id)

    @app.post(prefix+'/benchmarks/{batch_id}/cancel')
    def cancel_benchmark(batch_id:str,request:Request): return service(request).cancel_benchmark(batch_id)

    @app.post(prefix+'/diagnose')
    def diagnose(body:DiagnoseCreate,request:Request): return service(request).diagnose(**body.model_dump())

    @app.get(prefix+'/knowledge')
    def knowledge(q:str|None=Query(None,max_length=200)):
        from .knowledge import list_knowledge
        return list_knowledge(q)

    @app.post(prefix+'/diagnostics/explain')
    def explain(body:DiagnoseCreate,request:Request):return service(request).explain_diagnosis(**body.model_dump())

    @app.get(prefix+'/advice/effects')
    def effects(request:Request,source_job_id:str|None=None):return service(request).advice_effects(source_job_id)

    @app.get(prefix+'/advice/effects/{effect_id}')
    def effect(effect_id:str,request:Request):return service(request).advice_effect(effect_id)

    @app.post(prefix+'/advice/effects/{effect_id}/evaluate')
    def evaluate_effect(effect_id:str,request:Request):return service(request).evaluate_advice_effect(effect_id)

    @app.post(prefix+'/advice/effects/{effect_id}/link')
    def link_effect(effect_id:str,body:EffectLink,request:Request):return service(request).link_advice_effect(effect_id,body.job_id)

    @app.post(prefix+'/matrices/{matrix_id}/derive',status_code=201)
    def derive(matrix_id:str,body:MatrixDerive,request:Request):return service(request).derive_matrix(matrix_id,**body.model_dump())

    @app.post(prefix+'/comparisons/modes')
    def mode_compare(body:ModeCompare,request:Request):return service(request).compare_modes(**body.model_dump())

    @app.post(prefix+'/reports',status_code=201)
    def report(body:ReportCreate,request:Request): return service(request).create_report(**body.model_dump())

    @app.get(prefix+'/reports/{report_id}')
    def get_report(report_id:str,request:Request): return FileResponse(service(request).report_path(report_id),media_type='text/html')

    @app.post(prefix+'/backup')
    def backup(request:Request): return FileResponse(service(request).backup(),filename='emsolver-private-backup.zip')


    @app.patch(prefix+'/matrices/{matrix_id}')
    def patch_matrix(matrix_id:str,body:MatrixPatch,request:Request): return service(request).update_matrix(matrix_id,**body.model_dump(exclude_unset=True))

    @app.post(prefix+'/jobs/{job_id}/export')
    def export_selection(job_id:str,body:SelectionExport,request:Request):
        path=service(request).export_result(job_id,**body.model_dump())
        return FileResponse(path,filename=job_id+'-selected'+path.suffix)

    @app.get(prefix+'/jobs/{job_id}/vectors/{mode}')
    def preview_vector(job_id:str,mode:int,request:Request,sample_points:int=Query(512,ge=1,le=512)): return service(request).vector_preview(job_id,mode,sample_points)

    @app.post(prefix+'/cleanup')
    def cleanup(body:CleanupRequest,request:Request): return service(request).cleanup(body.dry_run)

    @app.post(prefix+'/matrices/{matrix_id}/archive')
    def archive_matrix(matrix_id:str,request:Request): return service(request).archive_matrix(matrix_id)

    @app.post(prefix+'/backup/restore')
    async def restore(request:Request,file:UploadFile=File(...),allow_missing:bool=Form(False)):
        temporary=service(request).root/'inbox'/('restore-'+__import__('uuid').uuid4().hex+'.zip')
        size=0
        try:
            with temporary.open('wb') as handle:
                while block:=await file.read(1024*1024):
                    size+=len(block)
                    if size>1024**3: raise ServiceError('BACKUP_TOO_LARGE',status=413)
                    handle.write(block)
            result=await run_in_threadpool(service(request).restore_backup,temporary,allow_missing=allow_missing)
            result.pop('destination',None)
            return result
        finally:
            await file.close()
            temporary.unlink(missing_ok=True)

    @app.get(prefix+'/capabilities')
    def capabilities(request:Request): return service(request).capabilities()

    @app.get(prefix+'/health')
    def health(request:Request): return service(request).health()

    @app.get(prefix+'/audit')
    def audit(request:Request): return service(request).audit()

    @app.get(prefix+'/coverage')
    def coverage(request:Request): return service(request).coverage()

    from .engineering_api import router as engineering_router
    app.include_router(engineering_router)
    from .industrial_api import router as industrial_router
    app.include_router(industrial_router)
    from .industrial_reference_api import router as reference_router
    app.include_router(reference_router)
    from .cluster_api import router as cluster_router
    app.include_router(cluster_router)

    # A traversal-safe SPA fallback serves only built frontend files.
    dist=Path(os.environ.get('EMSOLVER_WEB_DIST',Path(__file__).resolve().parent.parent/'web'/'dist'))
    @app.get('/{path:path}',include_in_schema=False)
    def frontend(path:str):
        if path.startswith('api/'):
            raise ServiceError('NOT_FOUND',status=404)
        candidate=(dist/path).resolve()
        if not candidate.is_relative_to(dist.resolve()): raise ServiceError('NOT_FOUND',status=404)
        if candidate.is_file(): return FileResponse(candidate)
        if (dist/'index.html').is_file(): return FileResponse(dist/'index.html')
        return JSONResponse({'application':'EMSolver Workbench','frontend':'Run npm run build in web/','api':'/api/v1','docs':'/docs'})
    return app


app=create_app()
