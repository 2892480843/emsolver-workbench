"""Application service with local execution or a durable remote worker queue."""
from __future__ import annotations
import copy
import fcntl
import hashlib
import json
import os
import platform
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import zipfile

from .store import utcnow, encode
from .persistence import open_store
from .jobs import JobRepository, JobStatus, TERMINAL, transition
from .events import EventJournal, WorkerEventReader
from .errors import ServiceError
from .integrity import file_hash
from .access import current_identity,can_read,require_project,require_admin
from .governance import ProjectGovernance,validated_policy,validated_members,directory_bytes
from . import __version__

DOWNLOADS = {'manifest.json','config.resolved.yaml','eigenvalues.npy','eigenvectors.npy','residuals.npy','validation.json','metrics.json','eigenvalues.csv','eigenvectors.npz','events.jsonl','eigenvalues.scipy.npz','eigenvectors.scipy.npz','environment.json','strategy.json','memory-estimate.json','diagnostics.json','permutation.npy','subspace.checkpoint.npz','exact-block.checkpoint.npz'}


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def identity(prefix):
    return prefix + '_' + uuid.uuid4().hex


class Service:
    def __init__(self, root=None, start_runner=True, execution_mode=None):
        self.execution_mode = os.environ.get('EMSOLVER_EXECUTION_MODE', 'local') if execution_mode is None else execution_mode
        if not isinstance(self.execution_mode,str) or self.execution_mode not in {'local', 'cluster'}:
            raise ValueError('execution_mode must be local or cluster')
        self.root = Path(root or os.environ.get('EMSOLVER_DATA_DIR','data')).resolve()
        self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        for directory in ('matrices','runs','reports','exports','inbox','engineering','industrial'):
            (self.root/directory).mkdir(exist_ok=True)
        self.store = open_store(self.root/'metadata.sqlite3')
        self.jobs = JobRepository(self.store)
        self.journal = EventJournal(self.root,self.store)
        self.governance = ProjectGovernance(self)
        from .telemetry import SecondaryTelemetry
        from .retention import LifecycleManager
        self.telemetry=SecondaryTelemetry()
        self.lifecycle=LifecycleManager(self)
        self._stop = threading.Event()
        self._mutex = threading.RLock()
        self._process = None
        self._thread = None
        self._active_job = None
        self._lock_file = (self.root/'runner.lock').open('a+')
        self._owns_runner = False
        if start_runner:
            try:
                fcntl.flock(self._lock_file,fcntl.LOCK_EX|fcntl.LOCK_NB)
                self._owns_runner = True
            except BlockingIOError:
                pass
        self._recovered_workers={}
        if self.execution_mode == 'cluster':
            from .cluster import ClusterCoordinator
            self.cluster = ClusterCoordinator(self)
        if self._owns_runner and self.execution_mode == 'local':
            from .recovery import verified_worker
            while recoverable := [job for job in self.jobs.recoverable() if job['id'] not in self._recovered_workers]:
                for job in recoverable:
                    process=verified_worker(self,job)
                    if process is not None and not self._recovered_workers:
                        self._recovered_workers[job['id']]=process
                        self._event(job['id'],{'stage':job.get('stage','running'),'event':'WORKER_REATTACHED'})
                    else:
                        if process is not None:process.terminate_tree()
                        if not self._recover_completed(job):self._finish(job['id'],'FAILED','WORKER_LOST')
        from .imports import ImportManager
        self.import_manager=ImportManager(self,start_worker=self._owns_runner)
        if self._owns_runner and self.execution_mode == 'local':
            self._thread=threading.Thread(target=self._loop,name='emsolver-queue',daemon=True)
            self._thread.start()


    def close(self):
        self._stop.set()
        if hasattr(self,'import_manager'):self.import_manager.close()
        self.telemetry.close()
        if self.execution_mode == 'local' and self._process is not None:
            self._kill_tree(self._process)
        if self._thread:
            self._thread.join(timeout=10)
        if self.execution_mode == 'local':
            for process in self._recovered_workers.values():self._kill_tree(process)
        if self._owns_runner:
            fcntl.flock(self._lock_file,fcntl.LOCK_UN)
        self._lock_file.close()
        self.store.close()

    def _recover_completed(self,job):
        from .reconcile import completed_result
        result=completed_result(self,job,DOWNLOADS)
        if result is None:return False
        metrics={**result.get('metrics',{}),'reconciled_published_run':True,'recovery_observation_gap':True,'peak_is_lower_bound':True}
        self._finish(job['id'],result['status'],result.get('error_code'),result=result,metrics=metrics)
        return True

    def _recover_orphan(self,job):
        import psutil
        pid=job.get('worker_pid')
        if not pid: return
        try:
            process=psutil.Process(pid)
            command=process.cmdline()
            expected=str(self.root/'runs'/job['id']/'worker.spec.json')
            birth=job.get('worker_create_time')
            if 'emsolver.worker' not in command or expected not in command or (birth and abs(process.create_time()-birth)>.01): return
            children=process.children(recursive=True)
            for child in reversed(children):
                try: child.terminate()
                except psutil.NoSuchProcess: pass
            process.terminate()
            _,alive=psutil.wait_procs(children+[process],timeout=.5)
            for child in alive:
                try: child.kill()
                except psutil.NoSuchProcess: pass
        except (psutil.NoSuchProcess,psutil.AccessDenied): pass

    def _get(self,kind,item_id):
        item = self.store.get(kind,item_id)
        if item is None:
            raise ServiceError('NOT_FOUND','The requested resource does not exist',404)
        project_id=item.get('project_id')
        if kind=='projects':self._authorize(item)
        elif project_id:self._project(project_id)
        elif kind=='configs':
            matrix=self.store.get('matrices',item.get('config',{}).get('problem',{}).get('matrix_id'))
            if matrix:self._project(matrix['project_id'])
        elif kind=='reports':
            if item.get('job_id'):self.get_job(item['job_id'])
            elif item.get('benchmark_id'):self._get('benchmarks',item['benchmark_id'])
        return item

    def _authorize(self,project,permission='read'):
        try:require_project(project,permission)
        except ServiceError:
            self.store.audit('authorization.denied',project['id'],result='denied')
            raise

    def _visible(self,item,visibility=None):
        project_id=item.get('project_id')
        if visibility is not None and project_id in visibility:
            return visibility[project_id]
        project=self.store.get('projects',project_id)
        allowed=bool(project and can_read(project))
        if visibility is not None:visibility[project_id]=allowed
        return allowed

    def _scope(self,project_id):
        if project_id is None and not current_identity().global_admin:
            visible=self.list_projects()
            if len(visible)==1:return visible[0]['id']
            raise ServiceError('PROJECT_SCOPE_REQUIRED','Select an authorized project',409)
        return project_id

    @staticmethod
    def _public(item):
        return {k:v for k,v in item.items() if not k.startswith('_')}

    def _project(self,project_id,writable=False):
        item = self._get('projects',project_id)
        if writable:self._authorize(item,'write')
        if writable and item.get('archived'):
            raise ServiceError('PROJECT_ARCHIVED','Archived projects cannot create experiments',409)
        return item

    def create_project(self,name,description=''):
        actor=current_identity()
        if actor.operations_admin and not actor.global_admin:
            raise ServiceError('FORBIDDEN','Operations role cannot create self-authorized business projects',403)
        name = str(name).strip()
        if not name or len(name)>200:
            raise ServiceError('INVALID_NAME')
        item = {'id':identity('p'),'name':name,'description':description,'archived':False,'created_at':utcnow(),'version':1,
                'classification':'internal','policy':validated_policy(),
                'members':[{'subject':current_identity().subject,'role':'admin'}]}
        self.store.put('projects',item['id'],item)
        self.store.audit('project.create',item['id'])
        return item

    def list_projects(self,q=None):
        return [item for item in self.store.list('projects') if can_read(item) and
                (not q or q.casefold() in (item['name']+' '+item.get('description','')).casefold())]

    @staticmethod
    def _check_edit_version(item, expected_version):
        if expected_version is not None and (type(expected_version) is not int or expected_version < 1):
            raise ServiceError('INVALID_EXPECTED_VERSION','expected_version must be a positive integer')
        version = item.get('version', 1)
        if expected_version is not None and expected_version != version:
            raise ServiceError('VERSION_CONFLICT','Resource changed; reload before applying this edit',409)
        return version

    def update_project(self,project_id,name=None,archived=None,description=None,classification=None,members=None,policy=None,expected_version=None):
        try:
            # Authorization and the merge must see the same committed document.
            # The database transaction also serializes other Service instances.
            with self._mutex,self.store.transaction():
                item = self._project(project_id)
                self._authorize(item,'admin')
                version = self._check_edit_version(item, expected_version)
                if name is not None:
                    if not isinstance(name,str) or not name.strip() or len(name)>200: raise ServiceError('INVALID_NAME')
                    item['name'] = name.strip()
                if description is not None: item['description'] = description
                if archived is not None: item['archived'] = bool(archived)
                if classification is not None:
                    if classification not in {'public','internal','confidential'}:raise ServiceError('INVALID_CLASSIFICATION')
                    item['classification']=classification
                if members is not None:item['members']=validated_members(members)
                if policy is not None:item['policy']=validated_policy({**item.get('policy',{}),**policy})
                item['version'] = version + 1
                self.store.put('projects',project_id,item)
                self.store.audit('project.update',project_id)
                return item
        except ServiceError as error:
            if error.code == 'FORBIDDEN':
                # _authorize's in-transaction audit rolled back with this edit.
                self.store.audit('authorization.denied',project_id,result='denied')
            raise

    def project_usage(self,project_id):return self.governance.usage(project_id)

    def list_configs(self):
        # These maps live for this call only: role changes and request identity
        # must be observed again on the next request.
        rows=[];matrix_visibility={};visibility={}
        for item in self.store.list('configs'):
            matrix_id=item['config']['problem']['matrix_id']
            if matrix_id not in matrix_visibility:
                matrix=self.store.get('matrices',matrix_id)
                # Only retain the decision, not each matrix's large diagnostics.
                matrix_visibility[matrix_id]=bool(matrix and self._visible(matrix,visibility))
            if matrix_visibility[matrix_id]:rows.append(item)
        return rows

    def list_matrices(self):
        visibility={}
        return [self._public(x) for x in self.store.list('matrices') if self._visible(x,visibility)]

    def get_matrix(self,matrix_id):
        return self._public(self._get('matrices',matrix_id))

    def _ingest(self,project_id,path,name,storage,source='private',metadata=None):
        self._project(project_id,True)
        from .numerics import load_matrix, inspect_matrix
        if path.suffix.lower() not in {'.npz','.npy'}:
            raise ServiceError('UNSUPPORTED_FORMAT')
        if path.stat().st_size>1024**3:
            raise ServiceError('INPUT_TOO_LARGE',status=413)
        try:
            matrix = load_matrix(path,storage=storage,max_bytes=1024**3)
            diagnostic = inspect_matrix(matrix)
        except (ValueError,RuntimeError,KeyError,OSError) as exc:
            code = re.match(r'^([A-Z][A-Z_]{3,})',str(exc))
            raise ServiceError(code.group(1) if code else 'INVALID_MATRIX','Matrix rejected by structural or Hermitian validation') from None
        return self._register_import_result(project_id,path,name,storage,metadata,diagnostic,source=source)

    def _register_import_result(self,project_id,path,name,storage,metadata,diagnostic,*,source='private'):
        project=self._project(project_id,True)
        if (metadata or {}).get('public') and source!='synthetic':self._authorize(project,'admin')
        if project.get('classification')=='confidential':
            metadata={**(metadata or {}),'public':False,'public_scope':'project_confidential_policy'}
        path=Path(path)
        content_hash = file_hash(path)
        destination = self.root/'matrices'/(content_hash+path.suffix.lower())
        if not destination.exists():
            temporary = destination.with_name(destination.name+'.'+uuid.uuid4().hex+'.tmp')
            shutil.copyfile(path,temporary)
            temporary.replace(destination)
            destination.chmod(0o400)
        item = {'id':identity('m'),'project_id':project_id,'name':name or path.name,'hash':content_hash,'storage':storage,'source':source,'public':bool((metadata or {}).get('public',source=='synthetic')),'created_at':utcnow(),'diagnostics':diagnostic,'_path':str(destination),'metadata':metadata or {},'version':1}
        item.update({key:diagnostic.get(key) for key in ('n','nnz','dtype','storage_bytes')})
        with self._mutex,self.store.transaction():
            exists=self.store.db.execute("SELECT 1 FROM entities WHERE kind='matrices' AND project_id=? AND json_extract(body,'$.hash')=? LIMIT 1",(project_id,content_hash)).fetchone()
            size=0 if exists else destination.stat().st_size
            self.governance.reserve(project_id,input_bytes=size)
            self.store.put('matrices',item['id'],item)
            self.governance.add_bytes(project_id,inputs=size)
            self.store.audit('matrix.import',item['id'])
        return self._public(item)

    def import_matrix(self,project_id,path,name=None,storage='full',metadata=None):
        if metadata is not None: metadata=self._validated_metadata(metadata)
        candidate = Path(path).expanduser()
        if candidate.is_symlink(): raise ServiceError('UNSAFE_PATH')
        candidate = candidate.resolve()
        roots = [Path(x).expanduser().resolve() for x in os.environ.get('EMSOLVER_IMPORT_ROOTS',str(Path.cwd())+os.pathsep+str(self.root/'inbox')).split(os.pathsep) if x]
        if not any(candidate.is_relative_to(root) for root in roots) or not candidate.is_file():
            raise ServiceError('UNSAFE_PATH','Local imports must be regular files inside configured import roots')
        # Freeze a private copy before inspecting an externally mutable local file.
        temporary=self.root/'inbox'/(uuid.uuid4().hex+candidate.suffix.lower())
        try:
            size=0
            with candidate.open('rb') as source,temporary.open('wb') as output:
                while block:=source.read(1024*1024):
                    size+=len(block)
                    if size>1024**3: raise ServiceError('INPUT_TOO_LARGE',status=413)
                    output.write(block)
            return self._ingest(project_id,temporary,name or candidate.name,storage,metadata=metadata)
        finally: temporary.unlink(missing_ok=True)

    def upload_matrix(self,project_id,filename,content,name=None,storage='full',metadata=None):
        if metadata is not None: metadata=self._validated_metadata(metadata)
        if len(content)>1024**3: raise ServiceError('INPUT_TOO_LARGE',status=413)
        if not filename or Path(filename).name != filename or '/' in filename or '\\' in filename:
            raise ServiceError('UNSAFE_FILENAME')
        temporary = self.root/'inbox'/(uuid.uuid4().hex+Path(filename).suffix.lower())
        try:
            temporary.write_bytes(content)
            return self._ingest(project_id,temporary,name or filename,storage,metadata=metadata)
        finally:
            temporary.unlink(missing_ok=True)

    def generate_sample(self,project_id,kind='indefinite',n=500,name=None,seed=20261016):
        self._project(project_id,True)
        if not isinstance(n,int) or isinstance(n,bool) or not 4 <= n <= 200000:
            raise ServiceError('INVALID_SAMPLE_SIZE')
        from .numerics import generate_sample
        path = self.root/'inbox'/(uuid.uuid4().hex+'.npz')
        try:
            metadata = generate_sample(path,kind=kind,n=n,seed=seed)
            metadata.update(public=True,license='CC0-1.0')
            return self._ingest(project_id,path,name or kind,'full','synthetic',metadata)
        except ValueError:
            raise ServiceError('INVALID_SAMPLE') from None
        finally:
            path.unlink(missing_ok=True)

    def create_config(self,config):
        from .contracts import resolve_config
        try:
            resolved = resolve_config(copy.deepcopy(config))
        except (ValueError,TypeError) as exc:
            raise ServiceError('INVALID_CONFIG','Configuration failed schema validation; check field names, types and finite numeric limits') from None
        matrix = self._get('matrices',resolved['problem']['matrix_id'])
        self._project(matrix['project_id'],True)
        # A frozen snapshot cannot reinterpret storage differently from its imported asset.
        if resolved['problem'].get('storage','full') != matrix['storage']:
            raise ServiceError('STORAGE_MISMATCH')
        item = {'id':identity('c'),'config':resolved,'hash':digest(resolved),'created_at':utcnow()}
        self.store.put('configs',item['id'],item)
        return item

    def capabilities(self):
        try:
            from .numerics import capabilities
            def clean(value):
                if isinstance(value,dict): return {k:clean(v) for k,v in value.items() if k.lower() not in {'filepath','path','filename'}}
                if isinstance(value,list): return [clean(v) for v in value]
                return value
            result = clean(capabilities())
        except (ImportError,RuntimeError):
            result = {**{name:{'available':False,'supported':False,'reason':'Numerical backend unavailable'} for name in ('scipy','slepc','mpi','gpu')},'limits':{'reason':'Numerical backend unavailable'}}
        if self.execution_mode == 'cluster':
            result['cluster_execution'] = {'supported_backends':['scipy'],'devices':['cpu'],
                                           'mpi_ranks':[1],'remote_checkpoint_publication':False}
            for name in ('slepc','mpi','gpu'):
                existing = result.get(name)
                result[name] = {**(existing if isinstance(existing,dict) else {}),
                                'available':False,'supported':False,
                                'reason':'Cluster workers support only SciPy on CPU with one MPI rank'}
        return result

    def _check_cluster_execution(self,config):
        if self.execution_mode == 'cluster' and (config['solver']['backend'] != 'scipy' or
                config['resources']['device'] != 'cpu' or config['resources']['mpi_ranks'] != 1):
            raise ServiceError('CLUSTER_BACKEND_UNAVAILABLE',
                               'Cluster workers support only SciPy on CPU with one MPI rank',409)

    def create_plan(self,config=None,config_id=None,project_id=None):
        if (config is None)==(config_id is None): raise ServiceError('CONFIG_REQUIRED')
        frozen = self._get('configs',config_id) if config_id else self.create_config(config)
        config = frozen['config']
        matrix = self._get('matrices',config['problem']['matrix_id'])
        project_id = project_id or matrix['project_id']
        self._project(project_id,True)
        if project_id != matrix['project_id']: raise ServiceError('PROJECT_MISMATCH',status=409)
        self._check_cluster_execution(config)
        n = matrix['n']; k = config['target'].get('k') or config['target'].get('max_modes',20)
        if config['target']['mode']=='nearest_k' and k >= n: raise ServiceError('INVALID_K','Requested modes must be smaller than matrix dimension')
        if matrix.get('archived'): raise ServiceError('MATRIX_ARCHIVED',status=409)
        if matrix.get('availability')=='MISSING' or not Path(matrix['_path']).is_file():raise ServiceError('INPUT_MISSING',status=409)
        cap = self.capabilities()
        backend = config['solver']['backend']
        limits=cap.get('limits',{})
        if k>limits.get('max_modes',10000): raise ServiceError('BACKEND_K_LIMIT')
        if config['solver']['subspace_max']>limits.get('max_subspace',10000): raise ServiceError('SUBSPACE_LIMIT')
        # Cluster resources follow the validated config protocol (up to 256
        # threads); claim admission checks each remote node's actual capacity.
        if self.execution_mode == 'local' and config['resources']['threads']>int(cap.get('cpu_count') or os.cpu_count() or 1): raise ServiceError('THREAD_BUDGET_EXCEEDED')
        if not cap.get(backend,{}).get('supported',False): raise ServiceError('BACKEND_UNAVAILABLE')
        if backend=='slepc' and config['solver']['method']!='krylovschur':raise ServiceError('BACKEND_UNAVAILABLE')
        if config['resources']['device']=='gpu':
            from .numerics.gpu import validate_gpu_profile
            try:validate_gpu_profile(config)
            except ValueError:raise ServiceError('BACKEND_UNAVAILABLE') from None
        elif config['resources']['mpi_ranks']!=1 and backend!='slepc':raise ServiceError('BACKEND_UNAVAILABLE')
        requested_method=config['solver']['method']
        supported_method='arpack' if requested_method=='auto' else requested_method
        if backend=='scipy' and supported_method not in cap.get('scipy',{}).get('methods',['arpack','block_shift_invert']): raise ServiceError('BACKEND_UNAVAILABLE')
        if config['problem']['type']!='standard_hermitian' or config['problem'].get('matrix_b_id'): raise ServiceError('UNSUPPORTED_PROBLEM')
        if config['problem']['scalar']=='float64' and 'complex' in str(matrix.get('dtype')): raise ServiceError('SCALAR_MISMATCH')
        # SLEPc owns an explicit backend contract, including its factor profiles.
        # algorithm_plan below validates that contract before a plan is stored.
        if config['solver']['method']!='jacobi_davidson' and backend!='slepc':
            if config['solver']['linear']['method'] not in {'sparse_lu','backend_validated'}: raise ServiceError('UNSUPPORTED_LINEAR_METHOD')
            if config['solver']['linear']['preconditioner'] not in {'auto_budgeted','none'}: raise ServiceError('UNSUPPORTED_PRECONDITIONER')
        from .numerics import algorithm_plan,estimate_resources
        try:strategy=algorithm_plan(config,matrix_statistics=matrix['diagnostics'])
        except (ValueError,TypeError):raise ServiceError('UNSUPPORTED_ALGORITHM_PLAN') from None
        except RuntimeError as error:
            if str(error).partition(':')[0]=='RESOURCE_EXHAUSTED':
                raise ServiceError('RESOURCE_ESTIMATE_EXCEEDED') from None
            raise
        if config['solver']['method']!='jacobi_davidson' and config['solver']['transform']!='shift_invert': raise ServiceError('UNSUPPORTED_TRANSFORM')
        if not config['output']['eigenvectors']: raise ServiceError('UNSUPPORTED_OUTPUT')
        estimate=estimate_resources(matrix['diagnostics'],config)
        if not estimate['minimum_fits_budget']:
            raise ServiceError('RESOURCE_ESTIMATE_EXCEEDED')
        from .engineering import EngineeringAssets
        engineering_model=EngineeringAssets(self).freeze(matrix)
        fingerprint={'config':frozen['hash'],'matrix':matrix['hash'],'capabilities':cap}
        if engineering_model is not None:fingerprint['engineering_model']=engineering_model
        item = {'id':identity('plan'),'config_id':frozen['id'],'config':config,'matrix_id':matrix['id'],'project_id':project_id,'estimate':estimate,'strategy':strategy,'capabilities':cap,'warnings':['Sparse LU fill-in is data dependent','Nearest-target certification depends on available numerical evidence'],'hash':digest(fingerprint),'input_hash':matrix['hash'],'matrix_metadata':copy.deepcopy(matrix.get('metadata',{})),'created_at':utcnow()}
        if engineering_model is not None:item['engineering_model']=engineering_model
        self.store.put('plans',item['id'],item)
        return item

    def submit_job(self,plan_id,idempotency_key=None,*,parent_job_id=None,experiment=None,resumption=None):
        with self._mutex,self.store.transaction():
            plan = self._get('plans',plan_id)
            # Membership is part of an experiment submission's identity. A
            # regular submission must not capture its predictable retry key.
            request_digest = digest({'plan_id':plan_id,**({'experiment':experiment} if experiment is not None else {}),**({'resumption':resumption} if resumption is not None else {})})
            if idempotency_key:
                if len(idempotency_key)>200: raise ServiceError('INVALID_IDEMPOTENCY_KEY')
                row = self.store.db.execute('SELECT digest,job_id FROM idempotency WHERE key=?',(idempotency_key,)).fetchone()
                if row:
                    if row[0] != request_digest: raise ServiceError('IDEMPOTENCY_CONFLICT',status=409)
                    return self.get_job(row[1])
            self._project(plan['project_id'],True)
            self._check_cluster_execution(plan['config'])
            matrix = self._get('matrices',plan['matrix_id'])
            if matrix.get('archived'): raise ServiceError('MATRIX_ARCHIVED',status=409)
            if not Path(matrix['_path']).is_file() or file_hash(matrix['_path']) != plan['input_hash'] or digest(self.capabilities()) != digest(plan['capabilities']):
                raise ServiceError('STALE_PLAN','Input data or runtime capabilities changed',409)
            if self.jobs.queued_count()>=100:
                raise ServiceError('QUEUE_FULL',status=429)
            self.governance.reserve(plan['project_id'],output_gib=plan['config']['resources']['disk_limit_gib'],queue=True)
            job = {'id':identity('j'),'plan_id':plan_id,'project_id':plan['project_id'],'matrix_id':plan['matrix_id'],'matrix_hash':matrix['hash'],'matrix_metadata':copy.deepcopy(plan.get('matrix_metadata',{})),'config':copy.deepcopy(plan['config']),'status':'QUEUED','stage':'queued','created_at':utcnow(),'result':None,'error':None,'metrics':{},'parent_job_id':None,'runtime':{'hardware_id':digest({'node':platform.node(),'machine':platform.machine(),'processor':platform.processor(),'cpu_count':os.cpu_count()}),'python':sys.version.split()[0],'platform':sys.platform,'capabilities':plan['capabilities']}}
            # Persist the retry relationship in the initial transaction: a worker
            # may advance this row immediately after the transaction commits.
            job['parent_job_id']=parent_job_id
            job['retained']=True
            if self.execution_mode == 'cluster':
                from .cluster_protocol import execution_fingerprint
                job['execution'] = execution_fingerprint()
            if plan.get('engineering_model') is not None:
                job['engineering_model']=copy.deepcopy(plan['engineering_model'])
            if resumption is not None:job['resumption']=copy.deepcopy(resumption)
            if experiment is not None:
                job['experiment']=copy.deepcopy(experiment)
            from .provenance import capture_provenance
            job['runtime']['provenance']=capture_provenance()
            self.store.put('jobs',job['id'],job)
            if idempotency_key:
                with self.store._write():
                    self.store.db.execute('INSERT INTO idempotency VALUES(?,?,?)',(idempotency_key,request_digest,job['id']))
            self._event(job['id'],{'stage':'queued','event':'QUEUED'})
            self.store.audit('job.submit',job['id'])
            return job

    def list_jobs(self): return [job for job in self.store.list('jobs') if self._visible(job)]
    def get_job(self,job_id): return self._get('jobs',job_id)

    def job_page(self,*,limit=50,cursor=None,project_id=None,status=None):
        project_id=self._scope(project_id)
        if project_id is not None:self._project(project_id)
        try:return self.jobs.page(limit=limit,cursor=cursor,project_id=project_id,status=status)
        except ValueError:raise ServiceError('INVALID_CURSOR_OR_FILTER','Invalid pagination cursor or filter') from None

    def workspace_summary(self,project_id=None):
        project_id=self._scope(project_id)
        if project_id is not None:self._project(project_id)
        summary=self.jobs.summary(project_id)
        summary['projects']=1 if project_id is not None else len(self.list_projects())
        return summary

    def _event(self,job_id,event):
        record=self.journal.append(job_id,event)
        # The authoritative journal commits first. A secondary exporter can
        # fail or saturate without altering lifecycle or numerical evidence.
        self.telemetry.emit(event)
        return record

    def set_retained(self,job_id,retained):
        if type(retained) is not bool:raise ServiceError('INVALID_RETENTION')
        with self._mutex,self.store.transaction():
            job=self.get_job(job_id)
            self._authorize(self._project(job['project_id']),'admin')
            job['retained']=retained
            self.store.put('jobs',job_id,job)
            self.store.audit('job.retention.update',job_id)
            return job

    def lifecycle_cleanup(self,dry_run=True):
        return self.lifecycle.run(dry_run=dry_run)

    def metrics_text(self):
        require_admin()
        from .telemetry import render_metrics
        return render_metrics(self.store,self.root,self.telemetry)

    def events(self,job_id,after=0):
        self.get_job(job_id)
        return self.store.events(job_id,after)

    def event_page(self,job_id,after=0,limit=200):
        self.get_job(job_id)
        try:return self.journal.page(job_id,after,limit)
        except ValueError:raise ServiceError('INVALID_EVENT_CURSOR','Invalid event cursor or limit') from None

    def _finish(self,job_id,status,code=None,result=None,metrics=None):
        with self._mutex,self.store.transaction():
            job = self.get_job(job_id)
            if job['status'] in TERMINAL: return job
            if status not in TERMINAL:raise ServiceError('INVALID_JOB_TRANSITION')
            if job.get('config',{}).get('solver',{}).get('backend')=='slepc':
                from .optional_backends import cleanup_slepc
                cleanup=cleanup_slepc(self.root/'runs'/job_id)
                metrics={**(metrics or {}),'container_cleanup':cleanup}
            if job.get('project_id') and self.store.get('projects',job['project_id']):
                self.governance.add_bytes(job['project_id'],outputs=directory_bytes(self.root/'runs'/job_id))
            if job['status']=='CANCELLING' and code!='WORKER_LOST':
                status,code,result='CANCELLED','USER_CANCELLED',None
            job=transition(job,status)
            if code: job['error']={'code':code,'message':code.replace('_',' ').capitalize()}
            if result is not None: job['result']=result
            if metrics is not None: job['metrics'].update(metrics)
            self.store.put('jobs',job_id,job)
            self._event(job_id,{'stage':job['stage'],'event':status,'error_code':code})
            return job

    def cancel_job(self,job_id):
        with self._mutex,self.store.transaction():
            job = self.get_job(job_id)
            self._authorize(self._project(job['project_id']),'write')
            self.store.audit('job.cancel',job_id,result='already-terminal' if job['status'] in TERMINAL else 'requested')
            if job['status'] in TERMINAL or job['status']=='CANCELLING': return job
            if job['status']=='QUEUED': return self._finish(job_id,'CANCELLED','USER_CANCELLED')
            job=transition(job,JobStatus.CANCELLING)
            self.store.put('jobs',job_id,job)
            self._event(job_id,{'stage':'cancelling','event':'CANCEL_REQUESTED'})
            return job

    def restart_job(self,job_id,*,use_checkpoint=False,resume_mode='subspace'):
        original=self.get_job(job_id)
        if original['status'] not in TERMINAL: raise ServiceError('JOB_ACTIVE',status=409)
        if resume_mode not in {'subspace','exact_block'} or (resume_mode=='exact_block' and not use_checkpoint):
            raise ServiceError('INVALID_RESUME_MODE')
        resumption=None
        if use_checkpoint:
            if original['config']['solver']['method'] not in {'block_shift_invert','jacobi_davidson'}:raise ServiceError('CHECKPOINT_UNSUPPORTED',status=409)
            from .contracts import resolve_config
            from .resumption import validate_snapshot,validate_exact_snapshot
            exact=resume_mode=='exact_block'
            name='exact-block.checkpoint.npz' if exact else 'subspace.checkpoint.npz'
            path=self.root/'runs'/job_id/'artifacts'/name
            validator=validate_exact_snapshot if exact else validate_snapshot
            sha=validator(path,resolve_config(original['config']),original['matrix_hash'])
            resumption={'kind':'exact_block' if exact else 'subspace_warm_start','exact_resume':exact,
                        'source_job_id':job_id,'checkpoint_sha256':sha}
        plan = self.create_plan(config=original['config'],project_id=original['project_id'])
        return self.submit_job(plan['id'],parent_job_id=job_id,resumption=resumption)

    def results(self,job_id):
        job=self.get_job(job_id)
        if job['result'] is None: raise ServiceError('RESULT_NOT_READY',status=409)
        result=copy.deepcopy(job['result'])
        metadata=job.get('matrix_metadata',{})
        model=job.get('engineering_model')
        if model and model.get('frequency_mapping'):
            from .engineering_contracts import frequency_values
            result.update(frequency_values(result.get('eigenvalues',[]),model['frequency_mapping']))
        elif metadata.get('frequency_mapping')=='lambda=omega_squared':
            import math
            units_known=metadata.get('units') in {'s^-2','1/s^2','rad^2/s^2'}
            result['frequencies_hz']=[math.sqrt(value)/(2*math.pi) if units_known and value>=0 else None for value in result.get('eigenvalues',[])]
            result['frequency_mapping']={'formula':'f=sqrt(lambda)/(2*pi)','units_confirmed':units_known,'negative_values':'No real frequency; reported as null','source':'Frozen user-declared matrix metadata'}
        return result

    def download(self,job_id,filename):
        self.get_job(job_id)
        if filename not in DOWNLOADS: raise ServiceError('ARTIFACT_NOT_ALLOWED',status=403)
        path=self.journal.export(job_id) if filename=='events.jsonl' else self.root/'runs'/job_id/'artifacts'/filename
        if not path.is_file() or path.is_symlink(): raise ServiceError('ARTIFACT_NOT_FOUND',status=404)
        self.store.audit('artifact.download',job_id)
        return path

    @staticmethod
    def _kill_tree(process):
        if hasattr(process,'terminate_tree'):
            process.terminate_tree();return
        import psutil
        try:
            parent=psutil.Process(process.pid)
            children=parent.children(recursive=True)
            for child in reversed(children):
                try: child.terminate()
                except psutil.NoSuchProcess: pass
            parent.terminate()
            _,alive=psutil.wait_procs(children+[parent],timeout=0.5)
            for child in alive:
                try: child.kill()
                except psutil.NoSuchProcess: pass
        except psutil.NoSuchProcess: pass
        try: process.wait(timeout=2)
        except subprocess.TimeoutExpired: process.kill()

    def _loop(self):
        while not self._stop.is_set():
            if self._recovered_workers:
                job_id,process=self._recovered_workers.popitem()
                try:
                    from datetime import datetime,timezone
                    job=self.get_job(job_id)
                    started=datetime.fromisoformat(job['started_at'].replace('Z','+00:00'))
                    elapsed=max(0,(datetime.now(timezone.utc)-started).total_seconds())
                    self._process=process;self._active_job=job_id
                    self._monitor_worker(job_id,self.root/'runs'/job_id,process,elapsed_before=elapsed,recovered=True)
                except Exception:
                    self._kill_tree(process);self._finish(job_id,'FAILED','WORKER_LOST')
                finally:self._process=None;self._active_job=None
                continue
            queued=self.jobs.next_queued()
            if queued:
                try: self._execute(queued['id'])
                except Exception:
                    if self._process: self._kill_tree(self._process)
                    self._finish(queued['id'],'FAILED','IO_ERROR')
                finally: self._process=None; self._active_job=None
            else: self._stop.wait(.15)

    def _execute(self,job_id):
        import psutil
        with self._mutex,self.store.transaction():
            job=self.get_job(job_id)
            if job['status']!='QUEUED': return
            matrix=self._get('matrices',job['matrix_id'])
            if file_hash(matrix['_path'])!=job['matrix_hash']:
                self._finish(job_id,'FAILED','INPUT_HASH_MISMATCH'); return
            directory=self.root/'runs'/job_id
            directory.mkdir(exist_ok=True)
            spec=directory/'worker.spec.json'
            specification={'config':job['config'],'input_path':matrix['_path'],'output_dir':str(directory/'artifacts')}
            if job['config']['solver']['backend']=='slepc':
                specification['backend_profile']=copy.deepcopy(job['runtime']['capabilities']['slepc'])
            if job.get('resumption'):
                exact=job['resumption'].get('exact_resume') is True
                name='exact-block.checkpoint.npz' if exact else 'subspace.checkpoint.npz'
                snapshot=self.root/'runs'/job['resumption']['source_job_id']/'artifacts'/name
                if snapshot.is_symlink() or not snapshot.is_file() or file_hash(snapshot)!=job['resumption']['checkpoint_sha256']:
                    self._finish(job_id,'FAILED','CHECKPOINT_INCOMPATIBLE');return
                specification['resume_checkpoint_path' if exact else 'initial_subspace_path']=str(snapshot)
            spec.write_text(encode(specification),encoding='utf-8')
            spec.chmod(0o600)
            from .process_environment import child_environment
            env=child_environment(job['config']['resources']['threads'])
            process=subprocess.Popen([sys.executable,'-m','emsolver.worker',str(spec)],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
            self._process=process; self._active_job=job_id
            job=transition(job,JobStatus.RUNNING)
            job.update(stage='starting',started_at=utcnow(),worker_pid=process.pid,worker_create_time=psutil.Process(process.pid).create_time())
            self.store.put('jobs',job_id,job)
            self._event(job_id,{'stage':'starting','event':'STARTED'})
        self._monitor_worker(job_id,directory,process)

    def _monitor_worker(self,job_id,directory,process,*,elapsed_before=0,recovered=False):
        import psutil
        job=self.get_job(job_id);spec=directory/'worker.spec.json'
        start=time.monotonic()-elapsed_before;peak=0;last_sample=elapsed_before
        if recovered:
            peak=max((event.get('metrics',{}).get('peak_rss_tree_bytes',0) or 0 for event in self.journal.tail(job_id)),default=0)
        reader=WorkerEventReader(directory/'worker.events.jsonl',offset=job.get('worker_event_offset',0))
        resources=job['config']['resources']; reason=None
        from .resource_budgets import memory_budgets
        partitions=memory_budgets(job['config'])
        from .optional_backends import private_directory_bytes
        while True:
            elapsed=time.monotonic()-start
            try:
                parent=psutil.Process(process.pid)
                rss=sum(p.memory_info().rss for p in [parent]+parent.children(recursive=True) if p.is_running())
                peak=max(peak,rss)
            except (psutil.NoSuchProcess,psutil.AccessDenied): pass
            disk=private_directory_bytes(directory,int(resources['disk_limit_gib']*1024**3))
            self._ingest_worker_events(job_id,reader)
            if self.get_job(job_id)['status']=='CANCELLING': reason=('CANCELLED','USER_CANCELLED')
            elif self._stop.is_set(): reason=('FAILED','WORKER_LOST')
            elif elapsed>resources['walltime_seconds']: reason=('TIMED_OUT','WALLTIME_EXCEEDED')
            elif peak>partitions['host_bytes'] or disk>resources['disk_limit_gib']*1024**3: reason=('FAILED','RESOURCE_EXHAUSTED')
            if reason: self._kill_tree(process); break
            if elapsed-last_sample>=max(.1,job['config'].get('observability',{}).get('metrics_interval_ms',1000)/1000):
                self._event(job_id,{'stage':self.get_job(job_id)['stage'],'event':'RESOURCE_SAMPLE','elapsed_seconds':elapsed,'metrics':{'rss_tree_bytes':rss if 'rss' in locals() else None,'peak_rss_tree_bytes':peak,'disk_bytes':disk}})
                last_sample=elapsed
            if process.poll() is not None: break
            self._stop.wait(.1)
        # The worker may publish its final events between the last read and exit.
        self._ingest_worker_events(job_id,reader)
        metrics={'walltime_seconds':time.monotonic()-start,'peak_rss_tree_bytes':peak or None,'disk_bytes':disk,'sampling_interval_seconds':.1,'measurement_scope':'worker process and descendants; sampled peak; excludes optional container', 'job_memory_partitions':partitions,'recovery_observation_gap':recovered,'peak_is_lower_bound':recovered}
        spec.unlink(missing_ok=True)
        if reason: self._finish(job_id,*reason,metrics=metrics); return
        result_path=directory/'worker.result.json'
        if not result_path.exists(): self._finish(job_id,'FAILED','WORKER_LOST',metrics=metrics); return
        try: result=json.loads(result_path.read_text(encoding='utf-8'))
        except (ValueError,OSError): self._finish(job_id,'FAILED','INVALID_WORKER_RESULT',metrics=metrics); return
        metrics={**result.get('metrics',{}),**metrics}
        result['metrics']=metrics
        status=result.get('status','FAILED')
        if status not in {'SUCCEEDED','PARTIAL','FAILED'}: status='FAILED'
        self._finish(job_id,status,result.get('error_code'),result=result,metrics=metrics)

    def _ingest_worker_events(self,job_id,reader):
        before=reader.offset
        events=reader.drain()
        if reader.offset==before:return
        try:
            with self._mutex,self.store.transaction():
                current=self.get_job(job_id)
                for event in events:
                    self._event(job_id,event)
                    if current['status']=='RUNNING' and event.get('stage'):
                        current['stage']=event['stage']
                # Byte cursor and events commit together. Roll back the live
                # reader too, so retry on this same object cannot skip a row.
                current['worker_event_offset']=reader.offset
                self.store.put('jobs',job_id,current)
        except BaseException:
            reader.rewind(before);raise

    def compare_jobs(self,job_ids):
        if not 2<=len(job_ids)<=20: raise ServiceError('INVALID_COMPARISON')
        jobs=[self.get_job(x) for x in job_ids]
        def comparable_config(job):
            return {k:job['config'].get(k) for k in ('problem','target','validation','resources')}
        baseline=jobs[0]
        comparable=all(job.get('matrix_hash')==baseline.get('matrix_hash') and comparable_config(job)==comparable_config(baseline) and job.get('runtime')==baseline.get('runtime') for job in jobs[1:])
        comparable=comparable and bool(baseline.get('runtime',{}).get('hardware_id')) and all(job['status']=='SUCCEEDED' for job in jobs)
        timings=[job.get('metrics',{}).get('walltime_seconds') for job in jobs]
        ratios=[timings[0]/value for value in timings] if comparable and all(isinstance(value,(int,float)) and value>0 for value in timings) else None
        return {'jobs':jobs,'comparable':comparable,'speedup':ratios[1] if ratios and len(ratios)==2 else None,'speedups_relative_to_first':ratios,'reason':'Only certified runs with matching input, target, precision, resources and environment support speed comparisons' if not comparable else 'Matching frozen conditions and certified results; speedup uses worker walltime only','differences':{key:[job['config'].get(key) for job in jobs] for key in ('problem','target','solver','validation','resources')}}

    def create_benchmark(self,project_id,matrix_ids,k=6,sigma=0.0,repeats=3):
        with self._mutex,self.store.transaction():
            self._project(project_id,True)
            if not matrix_ids or len(matrix_ids)>30 or not 1<=repeats<=10:
                raise ServiceError('INVALID_BENCHMARK')
            matrix_ids=list(dict.fromkeys(matrix_ids))
            queued=self.jobs.queued_count()
            if queued+len(matrix_ids)*repeats>100:
                raise ServiceError('BENCHMARK_QUEUE_LIMIT','Batch exceeds the available queue capacity',429)
            # Validate and freeze the whole suite in one transaction before jobs
            # become visible to the runner or another submitting connection.
            plans=[]
            for matrix_id in matrix_ids:
                matrix=self._get('matrices',matrix_id)
                if matrix['project_id']!=project_id: raise ServiceError('PROJECT_MISMATCH',status=409)
                plans.append(self.create_plan(config={'problem':{'matrix_id':matrix_id,'storage':matrix['storage']},'target':{'k':k,'sigma':sigma}},project_id=project_id))
            batch={'id':identity('b'),'project_id':project_id,'matrix_ids':matrix_ids,'k':k,'sigma':sigma,'repeats':repeats,'created_at':utcnow(),'jobs':[],'plan_hashes':[x['hash'] for x in plans]}
            self.store.put('benchmarks',batch['id'],batch)
            for plan in plans:
                for repeat in range(repeats):
                    job=self.submit_job(plan['id'],idempotency_key=f"{batch['id']}:{plan['id']}:{repeat}")
                    batch['jobs'].append(job['id'])
                    self.store.put('benchmarks',batch['id'],batch)
            self.store.audit('benchmark.create',batch['id'])
            return self.get_benchmark(batch['id'])

    def get_benchmark(self,batch_id):
        from .experiments import summarize_batch
        with self.store.read_transaction():
            batch=self._get('benchmarks',batch_id)
            jobs=[self.get_job(x) for x in batch['jobs']]
            batch.update(summarize_batch(batch,jobs))
            batch['runs']=jobs
            return batch

    def list_benchmarks(self):
        return [self.get_benchmark(x['id']) for x in self.store.list('benchmarks') if self._visible(x)]

    def benchmark_page(self,*,limit=20,cursor=None,project_id=None):
        project_id=self._scope(project_id)
        if project_id is not None:self._project(project_id)
        with self.store.read_transaction():
            try:page=self.store.page('benchmarks',limit=limit,cursor=cursor,project_id=project_id)
            except ValueError:raise ServiceError('INVALID_CURSOR_OR_FILTER','Invalid pagination cursor or filter') from None
            page['items']=[{key:value for key,value in self.get_benchmark(item['id']).items()
                            if key not in {'runs','cells'}} for item in page['items']]
            return page

    def create_experiment_plan(self,spec):
        from .experiments import ExperimentService
        return ExperimentService(self).plan(spec)

    def submit_experiment(self,experiment_plan_id):
        from .experiments import ExperimentService
        return ExperimentService(self).submit(experiment_plan_id)

    def cancel_benchmark(self,batch_id):
        with self._mutex,self.store.transaction():
            batch=self._get('benchmarks',batch_id)
            for job_id in batch['jobs']:
                if self.get_job(job_id)['status'] not in TERMINAL:
                    self.cancel_job(job_id)
            self.store.audit('benchmark.cancel',batch_id)
            return self.get_benchmark(batch_id)

    def apply_advice(self,*,changes,job_id=None,config=None):
        from .advice import apply_solver_advice
        from .contracts import content_hash,resolve_config
        from .experiments import config_diff
        if (job_id is None)==(config is None):raise ServiceError('CONFIG_REQUIRED')
        with self._mutex,self.store.transaction():
            base=self.get_job(job_id)['config'] if job_id else config
            candidate=apply_solver_advice(base,changes)
            resolved=resolve_config(base)
            item=self.create_config(candidate)
            item['configuration_diff']=config_diff(resolved,candidate)
            item['derivation']={'source':'rules_advice','source_config_hash':content_hash(resolved),
                                'source_job_id':job_id}
            self.store.put('configs',item['id'],item)
            self.store.audit('config.advice',item['id'])
            if job_id:
                effect=self._effects().create(job_id,item['id'])
                item['effect_id']=effect['id']
                self.store.put('configs',item['id'],item)
            return item

    def _effects(self):
        from .effects import AdviceEffects
        return AdviceEffects(self.store,self.get_job,lambda identity:self._get('configs',identity))

    def advice_effects(self,source_job_id=None):
        if source_job_id:self.get_job(source_job_id)
        return [item for item in self._effects().list(source_job_id) if self._visible(item)]

    def advice_effect(self,effect_id):
        self._get('advice_effects',effect_id)
        ledger=self._effects();effect=ledger.get(effect_id)
        if any(item['record_type']=='linked' for item in effect['records']):effect['evaluation']=ledger.evaluate(effect_id,persist=False)
        return effect

    def evaluate_advice_effect(self,effect_id):
        effect=self._get('advice_effects',effect_id)
        self._authorize(self._project(effect['project_id']),'write')
        return self._effects().evaluate(effect_id)

    def link_advice_effect(self,effect_id,job_id):
        effect=self._get('advice_effects',effect_id)
        self._authorize(self._project(effect['project_id']),'write')
        return self._effects().link(effect_id,job_id)

    def diagnose(self,job_id=None,config=None):
        from .knowledge import diagnose as rule_diagnosis
        job=self.get_job(job_id) if job_id else None
        matrix=self.get_matrix(job['matrix_id']) if job else None
        try:return rule_diagnosis(job,config,matrix=matrix,capabilities=self.capabilities())
        except (ValueError,TypeError):raise ServiceError('INVALID_CONFIG') from None

    def explain_diagnosis(self,job_id=None,config=None):
        from .ai_explainer import explain_diagnosis
        rules=self.diagnose(job_id,config);model=None
        configured=os.environ.get('EMSOLVER_LOCAL_MODEL_CONFIG')
        if configured:
            try:
                with Path(configured).open('rb') as source:raw=source.read(65537)
                model=json.loads(raw) if len(raw)<=65536 else {}
            except (OSError,ValueError):model={}
        return explain_diagnosis(rules,model_config=model)

    def derive_matrix(self,matrix_id,triangle,name=None):
        if triangle not in {'upper','lower'}:raise ServiceError('INVALID_STORAGE')
        original=self._get('matrices',matrix_id);self._project(original['project_id'],True)
        from .numerics import load_matrix
        from scipy import sparse
        matrix=load_matrix(Path(original['_path']),storage=original['storage'])
        selected=sparse.triu(matrix,format='csr') if triangle=='upper' else sparse.tril(matrix,format='csr')
        path=self.root/'inbox'/(uuid.uuid4().hex+'.npz')
        try:
            sparse.save_npz(path,selected)
            with self._mutex,self.store.transaction():
                result=self._ingest(original['project_id'],path,name or original['name']+' '+triangle,triangle,source='derived',metadata=copy.deepcopy(original.get('metadata',{})))
                item=self._get('matrices',result['id'])
                item['version']=1
                item['derivation']={'source_matrix_id':matrix_id,'source_hash':original['hash'],'operation':'extract_'+triangle,'input_nnz':int(matrix.nnz),'stored_nnz':int(selected.nnz),'problem_preserved':True}
                self.store.put('matrices',item['id'],item);self.store.audit('matrix.derive',item['id'])
                return self._public(item)
        finally:path.unlink(missing_ok=True)

    def compare_modes(self,candidate_job_id,reference_job_id):
        candidate=self.get_job(candidate_job_id);reference=self.get_job(reference_job_id)
        if candidate['matrix_hash']!=reference['matrix_hash']:raise ServiceError('INPUT_MISMATCH',status=409)
        if any(job['status'] not in TERMINAL or not job.get('result') for job in (candidate,reference)):
            raise ServiceError('RESULT_NOT_READY',status=409)
        from .evaluation import evaluate_results
        try:result=evaluate_results(self.root/'runs'/candidate_job_id/'artifacts',self.root/'runs'/reference_job_id/'artifacts')
        except (ValueError,OSError):raise ServiceError('MODE_COMPARISON_FAILED') from None
        result.update(candidate_job_id=candidate_job_id,reference_job_id=reference_job_id,interpretation='Cross-run matching; selected computed result is not independent reference certification')
        self.store.audit('result.mode.compare',candidate_job_id)
        return result

    def create_report(self,job_id=None,benchmark_id=None):
        if (job_id is None)==(benchmark_id is None): raise ServiceError('REPORT_SOURCE_REQUIRED')
        report_id=identity('report')
        path=self.root/'reports'/(report_id+'.html')
        if job_id is not None:
            from .reporting import build_report
            build_report(self.get_job(job_id),self.journal.tail(job_id),path)
            item={'id':report_id,'job_id':job_id,'created_at':utcnow(),'hash':file_hash(path),'url':'/api/v1/reports/'+report_id}
        else:
            from .reporting import build_benchmark_report
            build_benchmark_report(self.get_benchmark(benchmark_id),path)
            item={'id':report_id,'benchmark_id':benchmark_id,'created_at':utcnow(),'hash':file_hash(path),'url':'/api/v1/reports/'+report_id}
        self.store.put('reports',report_id,item)
        self.store.audit('report.create',job_id or benchmark_id)
        return item

    def report_path(self,report_id):
        self._get('reports',report_id)
        path=self.root/'reports'/(report_id+'.html')
        if not path.is_file(): raise ServiceError('ARTIFACT_NOT_FOUND',status=404)
        return path

    def create_bundle(self,job_id):
        from .reporting import build_bundle
        job=self.get_job(job_id)
        if job['status'] not in TERMINAL: raise ServiceError('JOB_ACTIVE',status=409)
        path=self.root/'exports'/(identity('bundle')+'.zip')
        metadata=job.get('matrix_metadata') or {}
        # Permission is frozen with the job. Later asset edits cannot silently
        # authorize release of an earlier private simulation's modal data.
        include_results=metadata.get('public') is True and bool(metadata.get('license'))
        build_bundle(job,self.journal.tail(job_id),self.root/'runs'/job_id/'artifacts',path,
                     include_input=False,include_results=include_results)
        self.store.audit('bundle.export',job_id)
        return path

    def backup(self):
        require_admin()
        # Serialize lifecycle changes across threads AND Store connections. A
        # separate read connection snapshots while this connection owns the
        # SQLite write reservation; backing up itself inside BEGIN would wait.
        with self._mutex,self.store.transaction():
            if self.store.count('jobs',status=('RUNNING','VERIFYING','CANCELLING')):
                raise ServiceError('BACKUP_BUSY','Wait for active solves to finish before a consistent backup',409)
            if self.store.count('imports',status=('RUNNING','CANCELLING')):
                raise ServiceError('BACKUP_BUSY','Wait for active imports to finish before a consistent backup',409)
            if self.lifecycle._recover():
                raise ServiceError('RETENTION_RECOVERY_PENDING','Recover interrupted retention before backing up',409)
            return self._backup_snapshot()

    def _backup_snapshot(self):
        import sqlite3
        destination=self.root/'exports'/(identity('backup')+'.zip')
        snapshot=destination.with_suffix('.sqlite3')
        if hasattr(self.store, 'export_sqlite'):
            self.store.export_sqlite(snapshot)
        else:
            source=sqlite3.connect((self.root/'metadata.sqlite3').as_uri()+'?mode=ro',uri=True)
            try:
                target=sqlite3.connect(snapshot)
                try: source.backup(target)
                finally: target.close()
            finally:source.close()
        try:
            with zipfile.ZipFile(destination,'w',compression=zipfile.ZIP_DEFLATED) as archive:
                archive.write(snapshot,'metadata.sqlite3')
                manifest={'metadata.sqlite3':file_hash(snapshot)}
                authority_file=os.environ.get('EMSOLVER_INDUSTRIAL_ATTESTATION_KEYS_FILE')
                authority_path=Path(authority_file).resolve() if authority_file else None
                for folder in ('matrices','runs','reports','engineering','industrial'):
                    for path in (self.root/folder).rglob('*'):
                        if path.is_file() and not path.is_symlink() and path.resolve()!=authority_path and path.name!='worker.spec.json' and not path.name.endswith('.tmp'):
                            member=str(path.relative_to(self.root))
                            sha=hashlib.sha256()
                            with path.open('rb') as source,archive.open(member,'w') as target:
                                for block in iter(lambda:source.read(1024*1024),b''):
                                    sha.update(block); target.write(block)
                            manifest[member]=sha.hexdigest()
                archive.writestr('backup-manifest.json',encode({'schema_version':'1.0','created_at':utcnow(),'files':manifest,'private':True,'restore_supported':True,'note':'Protected private backup; restore only into a new empty destination'}))
        finally: snapshot.unlink(missing_ok=True)
        self.store.audit('backup.export')
        return destination

    def health(self):
        writable=os.access(self.root,os.W_OK)
        capabilities=self.capabilities()
        scipy=capabilities.get('scipy',False)
        backend=bool(scipy.get('supported',False)) if isinstance(scipy,dict) else bool(scipy)
        queue_available = self.execution_mode == 'cluster' or (self._owns_runner and bool(self._thread and self._thread.is_alive()))
        ready = writable and (self.execution_mode == 'cluster' or (backend and self._owns_runner))
        return {'status':'ok' if ready else 'degraded','service_alive':True,'queue_available':queue_available,'storage_writable':writable,'storage_free_bytes':shutil.disk_usage(self.root).free,'backend_runnable':backend,'mode':current_identity().mode,'execution_mode':self.execution_mode,'persistence_backend':getattr(self.store,'dialect','sqlite'),'project_rbac':True,'worker_isolation':'independent process; optional container harness provides filesystem isolation','precise_resume':True,'precise_resume_profile':'fixed-shift SciPy block; same config/input/source/environment; no fallback','backup_restore':'new-empty-destination-only','version':__version__,'storage_schema':2}

    def audit(self):
        rows=self.store.audit()
        return rows if current_identity().global_admin else [row for row in rows if row.get('actor')==current_identity().subject]

    def coverage(self):
        registry=Path(os.environ.get('EMSOLVER_RESOURCE_DIR',Path(__file__).resolve().parent.parent))/'docs'/'feature-coverage.json'
        if registry.is_file():
            try: return json.loads(registry.read_text(encoding='utf-8'))
            except (ValueError,OSError): pass
        raise ServiceError('COVERAGE_REGISTRY_UNAVAILABLE','Feature evidence registry is unavailable',503)

    def archive_matrix(self,matrix_id):
        # Archival retains bytes and references needed by past experiments.
        project_id = None
        try:
            with self._mutex,self.store.transaction():
                item=self.store.get('matrices',matrix_id)
                if item is None:raise ServiceError('NOT_FOUND','The requested resource does not exist',404)
                project_id=item['project_id']
                self._project(project_id,True)
                item.update(archived=True,archived_at=utcnow(),version=item.get('version',1)+1)
                self.store.put('matrices',matrix_id,item)
                self.store.audit('matrix.archive',matrix_id)
                return self._public(item)
        except ServiceError as error:
            if error.code == 'FORBIDDEN':
                self.store.audit('authorization.denied',project_id,result='denied')
            raise

    def restore_backup(self,archive_path,destination=None,*,allow_missing=False):
        """Validate a private archive and reconstruct it in an empty data root."""
        require_admin()
        import sqlite3
        import stat
        from pathlib import PurePosixPath
        archive_path=Path(archive_path)
        requested=Path(destination) if destination else self.root/'restored'/identity('instance')
        if requested.is_symlink(): raise ServiceError('UNSAFE_RESTORE_DESTINATION')
        destination=requested.resolve()
        if destination==self.root or destination in self.root.parents or (destination.exists() and (not destination.is_dir() or any(destination.iterdir()))):
            raise ServiceError('RESTORE_DESTINATION_NOT_EMPTY',status=409)
        destination.parent.mkdir(parents=True,exist_ok=True)
        staging=destination.parent/('.restore-'+uuid.uuid4().hex)
        staging.mkdir()
        try:
            if archive_path.stat().st_size>1024**3: raise ServiceError('BACKUP_TOO_LARGE',status=413)
            with zipfile.ZipFile(archive_path) as archive:
                members=archive.infolist()
                if len(members)>100000 or sum(x.file_size for x in members)>2*1024**3:
                    raise ServiceError('BACKUP_TOO_LARGE',status=413)
                names=[x.filename for x in members]
                if len(names)!=len(set(names)) or 'backup-manifest.json' not in names or 'metadata.sqlite3' not in names:
                    raise ServiceError('INVALID_BACKUP')
                for member in members:
                    name=member.filename; relative=PurePosixPath(name)
                    mode=member.external_attr>>16
                    if relative.as_posix()!=name or relative.is_absolute() or '..' in relative.parts or '\\' in name or member.is_dir() or stat.S_ISLNK(mode) or member.flag_bits&1:
                        raise ServiceError('UNSAFE_BACKUP_MEMBER')
                    if relative.parts[0] not in {'matrices','runs','reports','engineering','industrial','metadata.sqlite3','backup-manifest.json'}:
                        raise ServiceError('UNSAFE_BACKUP_MEMBER')
                if archive.getinfo('backup-manifest.json').file_size>1024**2 or archive.getinfo('metadata.sqlite3').file_size>128*1024**2: raise ServiceError('BACKUP_TOO_LARGE',status=413)
                manifest=json.loads(archive.read('backup-manifest.json'))
                expected=manifest.get('files',{})
                if not isinstance(expected,dict) or len(expected)>100000 or 'metadata.sqlite3' not in expected:
                    raise ServiceError('INVALID_BACKUP_MANIFEST')
                for name,sha in expected.items():
                    relative=PurePosixPath(name)
                    if (relative.as_posix()!=name or relative.is_absolute() or '..' in relative.parts or '\\' in name or
                        relative.parts[0] not in {'matrices','runs','reports','engineering','industrial','metadata.sqlite3'} or not isinstance(sha,str) or not re.fullmatch(r'[0-9a-f]{64}',sha)):
                        raise ServiceError('INVALID_BACKUP_MANIFEST')
                actual=set(names)-{'backup-manifest.json'}
                missing_files=set(expected)-actual
                if actual-set(expected) or (missing_files and not allow_missing):raise ServiceError('INVALID_BACKUP_MANIFEST')
                for member in members:
                    if member.filename=='backup-manifest.json': continue
                    path=staging/member.filename
                    path.parent.mkdir(parents=True,exist_ok=True)
                    sha=hashlib.sha256()
                    with archive.open(member) as source,path.open('wb') as output:
                        for block in iter(lambda:source.read(1024*1024),b''):
                            sha.update(block); output.write(block)
                    if sha.hexdigest()!=expected[member.filename]: raise ServiceError('BACKUP_HASH_MISMATCH')
            from .backup import rebuild_metadata
            source_path=staging/'source-metadata.sqlite3'
            (staging/'metadata.sqlite3').replace(source_path)
            with sqlite3.connect(source_path.as_uri()+'?mode=ro',uri=True) as metadata_source:
                cluster_reenrollment_required = metadata_source.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='cluster_workers'").fetchone() is not None
            entity_count=rebuild_metadata(source_path,staging/'metadata.sqlite3',destination,staging,allow_missing=allow_missing,missing_files=missing_files)
            source_path.unlink()
            for folder in ('matrices','runs','reports','exports','inbox','engineering','industrial'): (staging/folder).mkdir(exist_ok=True)
            if destination.exists(): destination.rmdir()
            staging.replace(destination)
            self.store.audit('backup.restore')
            # Restoration deliberately discards cluster credentials and leases.
            # An archive cannot revive an old worker's authority over new runs.
            note = 'Launch a separate Service at the restored destination; active jobs are recovered as WORKER_LOST; missing inputs cannot run'
            if cluster_reenrollment_required:
                note += '; cluster workers must re-enroll because credentials and leases were reset'
            return {'restored':True,'instance_id':destination.name,'destination':str(destination),'entities':entity_count,'degraded':bool(missing_files),'missing_files':sorted(missing_files),'cluster_reenrollment_required':cluster_reenrollment_required,'note':note}
        except ServiceError: raise
        except (OSError,ValueError,KeyError,TypeError,sqlite3.Error,zipfile.BadZipFile,RuntimeError):
            raise ServiceError('INVALID_BACKUP','Archive failed integrity or reference validation') from None
        finally:
            if staging.exists(): shutil.rmtree(staging)

    @staticmethod
    def _validated_metadata(metadata):
        from urllib.parse import urlparse
        allowed={'source_url','license','units','frequency_mapping','public','description','purpose','generator_script','seed','classification','public_scope'}
        if not isinstance(metadata,dict) or set(metadata)-allowed: raise ServiceError('INVALID_METADATA')
        for key,value in metadata.items():
            if key=='public':
                if not isinstance(value,bool): raise ServiceError('INVALID_METADATA')
            elif key=='seed':
                if type(value) is not int or not 0<=value<=2**63-1:raise ServiceError('INVALID_METADATA')
            elif value is not None and (not isinstance(value,str) or len(value)>2000): raise ServiceError('INVALID_METADATA')
        if metadata.get('source_url') and (urlparse(metadata['source_url']).scheme not in {'http','https'} or not urlparse(metadata['source_url']).netloc): raise ServiceError('INVALID_SOURCE_URL')
        if metadata.get('source_url') and (urlparse(metadata['source_url']).username is not None or urlparse(metadata['source_url']).password is not None):raise ServiceError('INVALID_SOURCE_URL')
        if metadata.get('frequency_mapping') not in {None,'lambda=omega_squared'}: raise ServiceError('INVALID_FREQUENCY_MAPPING')
        if metadata.get('classification') not in {None,'public','internal','confidential'}:raise ServiceError('INVALID_METADATA')
        if metadata.get('public') and not metadata.get('license'): raise ServiceError('PUBLIC_LICENSE_REQUIRED')
        return copy.deepcopy(metadata)

    def update_matrix(self,matrix_id,name=None,metadata=None,expected_version=None):
        project_id = None
        try:
            with self._mutex,self.store.transaction():
                item=self.store.get('matrices',matrix_id)
                if item is None:raise ServiceError('NOT_FOUND','The requested resource does not exist',404)
                project_id = item['project_id']
                project=self._project(project_id,True)
                version=self._check_edit_version(item,expected_version)
                if name is not None:
                    if not isinstance(name,str) or not name.strip() or len(name)>200: raise ServiceError('INVALID_NAME')
                    item['name']=name.strip()
                if metadata is not None:
                    if not isinstance(metadata,dict): raise ServiceError('INVALID_METADATA')
                    if 'public' in metadata:self._authorize(project,'admin')
                    metadata=self._validated_metadata(metadata)
                    merged={**item.get('metadata',{}),**metadata}
                    if project.get('classification')=='confidential':
                        merged.update(public=False,public_scope='project_confidential_policy')
                    if merged.get('public') and not merged.get('license'): raise ServiceError('PUBLIC_LICENSE_REQUIRED')
                    versions=item.setdefault('metadata_versions',[])
                    versions.append({'time':utcnow(),'metadata':copy.deepcopy(item.get('metadata',{}))})
                    item['metadata']=merged
                    item['metadata_version']=len(versions)+1
                    item['public']=bool(merged.get('public',item.get('public',False)))
                item['version']=version+1
                self.store.put('matrices',matrix_id,item)
                self.store.audit('matrix.metadata.update',matrix_id)
                return self._public(item)
        except ServiceError as error:
            if error.code == 'FORBIDDEN':
                self.store.audit('authorization.denied',project_id,result='denied')
            raise

    def _vector_arrays(self,job_id):
        import numpy as np
        job=self.get_job(job_id)
        if job['status'] not in {'SUCCEEDED','PARTIAL'}: raise ServiceError('RESULT_NOT_READY',status=409)
        try:
            vectors=np.load(self.download(job_id,'eigenvectors.npy'),mmap_mode='r',allow_pickle=False)
            values=np.load(self.download(job_id,'eigenvalues.npy'),mmap_mode='r',allow_pickle=False)
            residuals=np.load(self.download(job_id,'residuals.npy'),mmap_mode='r',allow_pickle=False)
        except (ValueError,OSError): raise ServiceError('INVALID_RESULT_ARTIFACT') from None
        if vectors.ndim!=2 or values.ndim!=1 or residuals.ndim!=1 or vectors.shape[1]!=len(values) or len(values)!=len(residuals): raise ServiceError('INVALID_RESULT_ARTIFACT')
        return job,vectors,values,residuals

    def export_result(self,job_id,indices,format='numpy'):
        import numpy as np
        if format not in {'numpy','scipy'}: raise ServiceError('UNSUPPORTED_FORMAT')
        job,vectors,values,residuals=self._vector_arrays(job_id)
        if not isinstance(indices,list) or not indices or len(indices)>256 or len(set(indices))!=len(indices) or any(not isinstance(i,int) or isinstance(i,bool) or i<0 or i>=len(values) for i in indices):
            raise ServiceError('INVALID_MODE_INDICES')
        needed=vectors.shape[0]*len(indices)*vectors.dtype.itemsize
        resources=job['config']['resources']
        budget=min(256*1024**2,resources['memory_limit_gib']*1024**3*.1,resources['disk_limit_gib']*1024**3*.1)
        if needed>budget: raise ServiceError('EXPORT_BUDGET_EXCEEDED',status=413)
        path=self.root/'exports'/(identity('selection')+('.npz' if format=='numpy' else '.zip'))
        selected=np.asarray(vectors[:,indices])
        if format=='numpy':
            np.savez_compressed(path,eigenvalues=np.asarray(values[indices]),eigenvectors=selected,residuals=np.asarray(residuals[indices]),mode_indices=np.asarray(indices,dtype=np.int64))
        else:
            import io
            from scipy import sparse
            with zipfile.ZipFile(path,'w',compression=zipfile.ZIP_DEFLATED) as archive:
                for name,array in [('eigenvalues.npy',values[indices]),('residuals.npy',residuals[indices]),('mode_indices.npy',np.asarray(indices,dtype=np.int64))]:
                    buffer=io.BytesIO();np.save(buffer,array,allow_pickle=False);archive.writestr(name,buffer.getvalue())
                buffer=io.BytesIO();sparse.save_npz(buffer,sparse.csc_matrix(selected));archive.writestr('eigenvectors.scipy.npz',buffer.getvalue())
                archive.writestr('selection.json',encode({'job_id':job_id,'original_column_indices':indices,'status':job['status'],'target_certification':(job.get('result') or {}).get('validation',{}).get('target_certification')}))
        self.store.audit('result.selection.export',job_id)
        return path

    def vector_preview(self,job_id,mode,sample_points=512):
        import numpy as np
        if not isinstance(sample_points,int) or not 1<=sample_points<=512: raise ServiceError('INVALID_SAMPLE_BUDGET')
        job,vectors,values,residuals=self._vector_arrays(job_id)
        if not isinstance(mode,int) or mode<0 or mode>=len(values): raise ServiceError('INVALID_MODE_INDEX')
        positions=np.unique(np.linspace(0,vectors.shape[0]-1,min(sample_points,vectors.shape[0]),dtype=np.int64))
        column=vectors[positions,mode]
        return {'job_id':job_id,'mode':mode,'eigenvalue':float(values[mode]),'residual':float(residuals[mode]),'total_components':vectors.shape[0],'samples':[{'index':int(i),'real':float(v.real),'imag':float(v.imag),'abs':float(abs(v))} for i,v in zip(positions,column)],'interpretation':'algebraic mode samples; no physical field geometry supplied'}

    def cleanup(self,dry_run=True):
        require_admin()
        if not isinstance(dry_run,bool): raise ServiceError('INVALID_CLEANUP_MODE')
        cutoff=time.time()-86400
        references={Path(item['_path']).resolve() for item in self.store.list('matrices')}
        for item in self.store.list('imports'):
            if item.get('status') in {'QUEUED','RUNNING','CANCELLING'}:
                if item.get('_path'):references.add(Path(item['_path']).resolve())
                references.update(self.root/'inbox'/(item['id']+suffix) for suffix in ('.spec.json','.result.json','.result.json.tmp'))
        jobs={item['id'] for item in self.list_jobs()}
        candidates=[]
        for folder in ('inbox','matrices'):
            for path in (self.root/folder).iterdir():
                if path.is_file() and not path.is_symlink() and path.stat().st_mtime<cutoff and path.resolve() not in references:
                    candidates.append(path)
        for path in (self.root/'runs').iterdir():
            if path.is_dir() and not path.is_symlink() and re.fullmatch(r'j_[0-9a-f]{32}',path.name) and path.name not in jobs and path.stat().st_mtime<cutoff and not any(child.is_symlink() for child in path.rglob('*')):
                candidates.append(path)
        removed=[]
        if not dry_run:
            for path in candidates:
                if path.is_dir(): shutil.rmtree(path)
                else: path.unlink()
                removed.append(str(path.relative_to(self.root)))
        retention=self.lifecycle_cleanup(dry_run)
        self.store.audit('storage.cleanup.preview' if dry_run else 'storage.cleanup')
        return {'dry_run':dry_run,'candidates':[str(path.relative_to(self.root)) for path in candidates],'removed':removed,'retention':retention,'policy':'Only unreferenced files and orphan run directories older than 24 hours; registered matrices and retained jobs are protected'}
