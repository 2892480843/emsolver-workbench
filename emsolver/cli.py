"""CLI shares configuration and numerical semantics with the workbench."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
import time
import zipfile

import yaml

from .contracts import resolve_config, EXIT_CODES, ERROR_EXIT_CODES, TERMINAL_STATES
from .reporting import atomic_text, build_report, sanitize


EXPERIMENT_SPEC_MAX_BYTES=1024*1024


def read_experiment_spec(path):
    with Path(path).open('rb') as stream:
        content=stream.read(EXPERIMENT_SPEC_MAX_BYTES+1)
    if len(content)>EXPERIMENT_SPEC_MAX_BYTES:
        raise ValueError(f'Experiment spec must be at most {EXPERIMENT_SPEC_MAX_BYTES} bytes')
    try:
        value=yaml.safe_load(content.decode('utf-8'))
    except (UnicodeError,yaml.YAMLError,RecursionError) as exc:
        raise ValueError(f'Invalid experiment spec YAML: {exc}') from None
    if not isinstance(value,dict):
        raise ValueError('Experiment spec must be a YAML or JSON object')
    return value


def _page_limit(value):
    try:limit=int(value)
    except ValueError:raise argparse.ArgumentTypeError('limit must be an integer from 1 to 200') from None
    if not 1<=limit<=200:raise argparse.ArgumentTypeError('limit must be an integer from 1 to 200')
    return limit


def read_config(path=None):
    if path is None:return resolve_config({})
    value=yaml.safe_load(Path(path).read_text(encoding='utf-8'))
    return resolve_config(value)


def parser():
    p=argparse.ArgumentParser(prog='emsolver',description='稀疏厄密特征值求解与实验工作台')
    p.add_argument('--data-dir',default=None)
    sub=p.add_subparsers(dest='command',required=True)
    def cmd(name):
        a=sub.add_parser(name);a.add_argument('--json',action='store_true');return a
    a=cmd('doctor');a.add_argument('--profile',default='cpu16')
    a=cmd('serve');a.add_argument('--host',default='127.0.0.1');a.add_argument('--port',default=8000,type=int)
    cmd('remote-worker')
    a=cmd('sample');a.add_argument('--kind',default='indefinite',choices=['indefinite','clustered','repeated','near_singular','complex']);a.add_argument('--n',type=int,default=500);a.add_argument('--seed',type=int,default=20261016);a.add_argument('--output',required=True)
    a=cmd('inspect');a.add_argument('--matrix',required=True);a.add_argument('--metadata');a.add_argument('--storage',default='full',choices=['full','lower','upper'])
    a=cmd('solve');a.add_argument('--matrix',required=True);a.add_argument('--config');a.add_argument('--output',default=None);a.add_argument('--k',type=int);a.add_argument('--sigma',type=float)
    a=cmd('validate');a.add_argument('--matrix',required=True);a.add_argument('--run',required=True);a.add_argument('--config')
    a=cmd('plan');a.add_argument('--config',required=True);a.add_argument('--project')
    a=cmd('config');a.add_argument('action',choices=['validate']);a.add_argument('--config',required=True)
    a=cmd('matrix');a.add_argument('action',choices=['import','list','archive']);a.add_argument('--matrix');a.add_argument('--id');a.add_argument('--project');a.add_argument('--name');a.add_argument('--metadata');a.add_argument('--storage',default='full')
    a=cmd('projects');a.add_argument('action',choices=['list','create','archive']);a.add_argument('--id');a.add_argument('--name')
    a=cmd('jobs');a.add_argument('action',choices=['submit','status','events','cancel','restart','wait','list']);a.add_argument('--plan');a.add_argument('--job');a.add_argument('--idempotency-key');a.add_argument('--after',type=int,default=0);a.add_argument('--follow',action='store_true');a.add_argument('--checkpoint')
    a=cmd('experiments');actions=a.add_subparsers(dest='action',required=True)
    def experiment_action(name):
        action=actions.add_parser(name)
        action.add_argument('--json',action='store_true',default=argparse.SUPPRESS)
        return action
    a=experiment_action('plan');a.add_argument('--spec',required=True,help='Path to a YAML or JSON experiment specification (at most 1 MiB)')
    a=experiment_action('submit');a.add_argument('--plan',required=True,help='Frozen experiment plan ID')
    a=experiment_action('status');a.add_argument('--batch',required=True,help='Experiment batch ID')
    a=experiment_action('cancel');a.add_argument('--batch',required=True,help='Experiment batch ID')
    a=experiment_action('list');a.add_argument('--project');a.add_argument('--limit',type=_page_limit,default=20);a.add_argument('--cursor')
    a=cmd('results');a.add_argument('action',choices=['export','compare']);a.add_argument('--run');a.add_argument('--ids',nargs='+');a.add_argument('--format',default='both',choices=['numpy','scipy','both']);a.add_argument('--output')
    a=cmd('benchmark');a.add_argument('--suite',default='benchmarks/required.yaml');a.add_argument('--output',default=None)
    a=cmd('report');a.add_argument('--run',required=True);a.add_argument('--format',default='html',choices=['html']);a.add_argument('--output')
    a=cmd('reproduce');a.add_argument('--bundle',required=True);a.add_argument('--matrix');a.add_argument('--output',required=True)
    return p


def _result_exit(result):
    error=result.get('error_code')
    if error is None and isinstance(result.get('error'),dict):
        error=result['error'].get('code')
    if error in {'UNSUPPORTED_PROBLEM','UNSUPPORTED_TRANSFORM','UNSUPPORTED_LINEAR_METHOD','UNSUPPORTED_PRECONDITIONER','UNSUPPORTED_OUTPUT'}:return 6
    if error in {'NO_VALID_MODES','NOT_CONVERGED'}:return 3
    if error in {'BACKEND_K_LIMIT','SUBSPACE_LIMIT','NOT_HERMITIAN','NON_HERMITIAN','OUTPUT_EXISTS','SCALAR_MISMATCH',
                 'INPUT_BUDGET_EXCEEDED','INVALID_DTYPE','NONFINITE_INPUT','INVALID_ARCHIVE','INVALID_SCHEMA',
                 'INVALID_SHAPE','INVALID_FORMAT','INVALID_STRUCTURE','INVALID_INDICES','INVALID_STORAGE',
                 'INVALID_TOLERANCE','TRIANGLE_MISMATCH','NUMERICAL_SCALE_LIMIT'}:return 2
    return ERROR_EXIT_CODES.get(error,EXIT_CODES.get(result.get('status'),9))


def _reproduce(args):
    dest=Path(args.output);dest.mkdir(parents=True,exist_ok=True)
    extracted=dest/'bundle'
    if extracted.exists():raise ValueError('OUTPUT_EXISTS: reproduction bundle directory exists')
    with zipfile.ZipFile(args.bundle) as archive:
        members=archive.infolist()
        if len(members)>100000 or sum(x.file_size for x in members)>2*1024**3:raise ValueError('ARCHIVE_TOO_LARGE')
        names=[member.filename for member in members]
        if len(names)!=len(set(names)):raise ValueError('DUPLICATE_ARCHIVE_MEMBER')
        if 'checksums.json' not in names or archive.getinfo('checksums.json').file_size>16*1024**2:
            raise ValueError('INVALID_CHECKSUM_MANIFEST')
        checks=json.loads(archive.read('checksums.json'))
        if not isinstance(checks,dict) or set(checks)!=set(names)-{'checksums.json'}:
            raise ValueError('CHECKSUM_COVERAGE_MISMATCH')
        for member in members:
            name=Path(member.filename)
            if name.is_absolute() or '..' in name.parts or '\\' in member.filename or member.is_dir() or ((member.external_attr>>16)&0o170000)==0o120000:
                raise ValueError('UNSAFE_ARCHIVE_PATH')
            if member.filename!='checksums.json':
                sha=hashlib.sha256()
                with archive.open(member) as stream:
                    for block in iter(lambda:stream.read(1024*1024),b''):sha.update(block)
                if sha.hexdigest()!=checks[member.filename]:raise ValueError('CHECKSUM_MISMATCH')
        archive.extractall(extracted)
    matrix=Path(args.matrix) if args.matrix else next((extracted/'input').glob('matrix.*'),None)
    if matrix is None:raise ValueError('INPUT_REQUIRED: public bundle excludes private input; pass --matrix')
    from .execution import execute_local
    result=execute_local(matrix,read_config(extracted/'run/config.resolved.yaml'),dest/'rerun')
    return result,_result_exit(result)


def _benchmark(args):
    from .execution import execute_local
    from .numerics import generate_sample
    spec=yaml.safe_load(Path(args.suite).read_text());root=Path(args.output or f'runs/benchmark-{int(time.time())}');root.mkdir(parents=True,exist_ok=True)
    rows=[]
    for case in spec['cases']:
        sample=root/f"{case['name']}.npz"
        generate_sample(sample,kind=case['kind'],n=int(case.get('n',500)),seed=20261016)
        for repeat in range(int(case.get('repeats',spec.get('repeats',1)))):
            cfg=resolve_config(case.get('config',{}));cfg['target']['k']=int(case.get('k',6))
            result=execute_local(sample,cfg,root/f"{case['name']}-{repeat}")
            expected=case.get('expected_status',['SUCCEEDED','PARTIAL'])
            passed=result['status'] in expected
            if case.get('expected_error_code'):
                passed=passed and result.get('error_code')==case['expected_error_code']
            elif result['status'] in {'SUCCEEDED','PARTIAL'}:
                passed=passed and result.get('validation',{}).get('numerical_status')=='PASSED'
            rows.append({'case':case['name'],'repeat':repeat,'n':case.get('n',500),'status':result['status'],
                         'accepted':result.get('n_accepted',0),'metrics':result.get('metrics',{}),
                         'validation':result.get('validation',{}),'error_code':result.get('error_code'),
                         'expected_status':expected,'expected_error_code':case.get('expected_error_code'),'pass':passed})
    summary={'runs':rows,'all_expected':all(x['pass'] for x in rows)}
    atomic_text(root/'benchmark.json',json.dumps(sanitize(summary),ensure_ascii=False,indent=2))
    return summary,0 if summary['all_expected'] else 7


def main(argv=None):
    arguments=sys.argv[1:] if argv is None else list(argv)
    if arguments and arguments[0]=='remote-worker':
        from .remote_worker import main as remote_main
        return remote_main(arguments[1:])
    args=parser().parse_args(argv)
    try:
        command=args.command;exit_code=0
        if command=='experiments' and args.action=='plan':spec=read_experiment_spec(args.spec)
        if command=='serve':
            import os,uvicorn
            if args.data_dir:os.environ['EMSOLVER_DATA_DIR']=args.data_dir
            if args.host not in ('127.0.0.1','localhost','::1') and not (os.environ.get('EMSOLVER_API_TOKEN') or os.environ.get('EMSOLVER_IDENTITIES_FILE')):
                raise ValueError('AUTH_REQUIRED: non-loopback server requires EMSOLVER_API_TOKEN or EMSOLVER_IDENTITIES_FILE')
            uvicorn.run('emsolver.api:app',host=args.host,port=args.port);return 0
        if command=='config': result={'valid':True,'config':read_config(args.config)}
        elif command=='doctor':
            import platform,psutil
            from .numerics import capabilities
            result={**capabilities(),'platform':platform.platform(),'cpu_count':psutil.cpu_count(),'memory_bytes':psutil.virtual_memory().total,'profile':args.profile}
        elif command=='sample':
            from .numerics import generate_sample
            result=generate_sample(Path(args.output),kind=args.kind,n=args.n,seed=args.seed)
        elif command=='inspect':
            from .numerics import load_matrix,inspect_matrix
            storage=args.storage
            if args.metadata:storage=json.loads(Path(args.metadata).read_text()).get('storage',storage)
            result=inspect_matrix(load_matrix(Path(args.matrix),storage=storage))
        elif command=='solve':
            from .execution import execute_local
            cfg=read_config(args.config)
            if args.k is not None:cfg['target']['k']=args.k
            if args.sigma is not None:cfg['target']['sigma']=args.sigma
            cfg=resolve_config(cfg)
            output=Path(args.output or f'runs/run-{time.time_ns()}')
            result=execute_local(Path(args.matrix),cfg,output);exit_code=_result_exit(result)
        elif command=='validate':
            from .numerics import verify_run
            result=verify_run(Path(args.matrix),Path(args.run),read_config(args.config) if args.config else None)
            numerical=result.get('numerical_status') in ('valid','passed','PASS','PASSED') or result.get('passed') or result.get('valid')
            certified=result.get('target_certification')=='CERTIFIED'
            exit_code=(0 if certified else 4) if numerical else 7
        elif command=='benchmark': result,exit_code=_benchmark(args)
        elif command=='reproduce':result,exit_code=_reproduce(args)
        elif command=='report':
            folder=Path(args.run);result_data=json.loads((folder/'summary.json').read_text())
            config_path=folder/'config.resolved.yaml'
            if not config_path.exists() and result_data.get('status') in {'FAILED','CANCELLED','TIMED_OUT'}:
                cfg={'recording_status':'UNAVAILABLE','reason':'No frozen configuration was recorded for this failed run'}
            else:cfg=read_config(config_path)
            events=[]
            if (folder/'events.jsonl').exists():events=[json.loads(l) for l in (folder/'events.jsonl').read_text().splitlines() if l]
            path=build_report({'id':folder.name,'status':result_data['status'],'config':cfg,'result':result_data},events,Path(args.output or folder/'report.html'))
            result={'report':str(path)}
        elif command=='results' and args.action=='export':
            import numpy as np
            from scipy import sparse
            folder=Path(args.run);dest=Path(args.output);dest.mkdir(parents=True,exist_ok=True)
            values=np.load(folder/'eigenvalues.npy',allow_pickle=False);vectors=np.load(folder/'eigenvectors.npy',allow_pickle=False)
            if args.format in ('numpy','both'):
                np.save(dest/'eigenvalues.npy',values);np.save(dest/'eigenvectors.npy',vectors)
            if args.format in ('scipy','both'):
                sparse.save_npz(dest/'eigenvalues.scipy.npz',sparse.csr_matrix(values.reshape(1,-1)))
                sparse.save_npz(dest/'eigenvectors.scipy.npz',sparse.csr_matrix(vectors))
            result={'n_exported':len(values),'format':args.format,'output':str(dest)}
        else:
            from .service import Service
            # Read/plan/submit commands must never start then kill a queue worker.
            # An explicit wait may own the queue until its target reaches a terminal state.
            service=Service(root=args.data_dir,start_runner=command=='jobs' and args.action=='wait')
            try:
                if command=='projects':
                    result=service.list_projects() if args.action=='list' else service.create_project(args.name) if args.action=='create' else service.update_project(args.id,archived=True)
                elif command=='matrix':
                    if args.action=='list':result=service.list_matrices()
                    elif args.action=='archive':result=service.archive_matrix(args.id)
                    else:
                        project=args.project or service.create_project('命令行导入')['id']
                        metadata=json.loads(Path(args.metadata).read_text(encoding='utf-8')) if args.metadata else None
                        result=service.import_matrix(project,Path(args.matrix),name=args.name,storage=args.storage,metadata=metadata)
                elif command=='plan':result=service.create_plan(config=read_config(args.config),project_id=args.project)
                elif command=='experiments':
                    if args.action=='plan':result=service.create_experiment_plan(spec)
                    elif args.action=='submit':result=service.submit_experiment(args.plan)
                    elif args.action=='status':result=service.get_benchmark(args.batch)
                    elif args.action=='cancel':result=service.cancel_benchmark(args.batch)
                    else:result=service.benchmark_page(project_id=args.project,limit=args.limit,cursor=args.cursor)
                elif command=='jobs':
                    action=args.action
                    if action=='submit':result=service.submit_job(args.plan,args.idempotency_key)
                    elif action=='list':result=service.list_jobs()
                    elif action=='status':result=service.get_job(args.job)
                    elif action=='cancel':result=service.cancel_job(args.job)
                    elif action=='restart':
                        if args.checkpoint not in (None,'subspace'):
                            raise ValueError('INVALID_CHECKPOINT: use --checkpoint subspace for the server-owned warm-start snapshot')
                        result=service.restart_job(args.job,use_checkpoint=args.checkpoint=='subspace')
                    elif action=='events':
                        if args.follow:
                            cursor=args.after
                            while True:
                                records=service.events(args.job,cursor)
                                for record in records:
                                    print(json.dumps(sanitize(record),ensure_ascii=False),flush=True)
                                    cursor=record['seq']
                                if service.get_job(args.job)['status'] in TERMINAL_STATES:return 0
                                time.sleep(.25)
                        result=service.events(args.job,args.after)
                    else:
                        while True:
                            result=service.get_job(args.job)
                            if result['status'] in TERMINAL_STATES:break
                            time.sleep(.15)
                        exit_code=_result_exit(result)
                elif command=='results':result=service.compare_jobs(args.ids)
                else:raise ValueError('INVALID_COMMAND')
            finally:service.close()
        print(json.dumps(sanitize(result),ensure_ascii=False,indent=None if args.json else 2,allow_nan=False,default=str))
        return exit_code
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({'code':'INVALID_CONFIG','message':str(exc)},ensure_ascii=False),file=sys.stderr)
        return 2
    except Exception as exc:
        print(json.dumps({'code':getattr(exc,'code','INTERNAL_ERROR'),'message':str(exc)},ensure_ascii=False),file=sys.stderr)
        return 9


if __name__=='__main__':raise SystemExit(main())
