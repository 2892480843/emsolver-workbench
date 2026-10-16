#!/usr/bin/env python3
"""Linux container solver; reference evaluation runs only afterwards on the host."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from emsolver.contracts import resolve_config
from emsolver.integrity import file_hash
from emsolver.reporting import atomic_text,sanitize
import yaml


def container_options(matrix,config,output,resources):
    """Only three explicit file/directory mounts. Never mount a parent directory."""
    return ['--network','none','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges',
            '--pids-limit','64','--memory',str(int(resources['memory_limit_gib']*1024**3)),
            '--memory-swap',str(int(resources['memory_limit_gib']*1024**3)),
            '--cpus',str(resources['threads']),'--user',f'{os.getuid()}:{os.getgid()}',
            '--tmpfs','/tmp:rw,noexec,nosuid,size=67108864',
            '--mount',f'type=bind,src={matrix},dst=/input/matrix{matrix.suffix},readonly',
            '--mount',f'type=bind,src={config},dst=/input/config.yaml,readonly',
            '--mount',f'type=bind,src={output},dst=/output']


def docker(*args,timeout=30):
    return subprocess.run(['docker',*args],capture_output=True,text=True,timeout=timeout,check=True).stdout


PROBE="""import json,socket,sys
from pathlib import Path
result={}
for label,path in [('reference',sys.argv[1]),('docker_socket','/var/run/docker.sock')]:
 try:
  with open(path,'rb') as file:file.read(1)
  result[label]='READABLE'
 except (FileNotFoundError,PermissionError):result[label]='DENIED'
s=socket.socket();s.settimeout(1)
try:result['network_connect_errno']=s.connect_ex(('1.1.1.1',443))
finally:s.close()
print(json.dumps(result))
"""


def run(matrix,config,output,*,image='emsolver-solver:local',reference=None):
    matrix=Path(matrix).resolve(strict=True);config=Path(config).resolve(strict=True)
    output=Path(output).resolve()
    if not matrix.is_file() or not config.is_file():raise ValueError('Regular input/config files required')
    if matrix.suffix not in {'.npy','.npz'}:raise ValueError('Unsupported matrix suffix')
    if output.exists() and any(output.iterdir()):raise ValueError('Output must be new or empty')
    output.mkdir(parents=True,exist_ok=True)
    cfg=resolve_config(yaml.safe_load(config.read_text(encoding='utf-8')))
    resources=cfg['resources']
    # Linux hard memory/CPU/PID controls supplement the application monitor.
    # Reference bytes and reference parent directories never enter this image.
    if reference is not None:
        reference=Path(reference).resolve(strict=True)
        if reference==output or output in reference.parents or reference in matrix.parents:
            raise ValueError('Reference must be outside candidate output and input mounts')
    candidate=output/'candidate';candidate.mkdir()
    options=container_options(matrix,config,candidate,resources)
    image_info=json.loads(docker('image','inspect',image))[0]
    name='emsolver-isolated-'+uuid.uuid4().hex
    evidence={'schema_version':'1.0','image':image,'image_id':image_info['Id'],
              'image_platform':{'os':image_info['Os'],'architecture':image_info['Architecture']},
              'input_sha256':file_hash(matrix),'config_sha256':file_hash(config),
              'reference_mounted':False,'reference_evaluation_phase':'after_candidate_exit',
              'scope':'Linux container functionality; host emulation is not native competition performance'}
    reference_probe='/evaluator/private-reference'
    if reference is not None:reference_probe=str(reference/'eigenvalues.npy')
    try:
        # The access probe uses the exact same isolation/mount options in a
        # separate container; its path argument is never sent to the solver.
        probe=json.loads(docker('run','--rm',*options,'--entrypoint','python',image,'-c',PROBE,reference_probe))
        evidence['access_probe']=probe
        if probe.get('reference')!='DENIED' or probe.get('docker_socket')!='DENIED' or probe.get('network_connect_errno')==0:
            raise ValueError('ISOLATION_CHECK_FAILED')
        docker('create','--name',name,*options,image,'solve','--matrix','/input/matrix'+matrix.suffix,
               '--config','/input/config.yaml','--output','/output','--json')
        inspected=json.loads(docker('inspect',name))[0]
        host=inspected['HostConfig']
        evidence['security']={key:host.get(key) for key in ('NetworkMode','ReadonlyRootfs','CapDrop','SecurityOpt','Memory','MemorySwap','NanoCpus','PidsLimit','Privileged')}
        evidence['mounts']=[{'destination':mount['Destination'],'read_only':not mount['RW'],'kind':mount['Type']} for mount in inspected['Mounts']]
        start=time.monotonic()
        try:
            process=subprocess.run(['docker','start','--attach',name],capture_output=True,text=True,timeout=resources['walltime_seconds']+20)
            state=json.loads(docker('inspect',name))[0]['State']
            evidence.update(container_exit_code=state['ExitCode'],oom_killed=state.get('OOMKilled',False),
                            end_to_end_seconds=time.monotonic()-start)
            atomic_text(output/'candidate.stdout.json',process.stdout[-1024*1024:])
        except subprocess.TimeoutExpired:
            docker('kill',name)
            evidence.update(container_exit_code=None,error_code='WALLTIME_EXCEEDED')
        if reference is not None and (candidate/'summary.json').is_file():
            from emsolver.evaluation import evaluate_results
            evidence['evaluation']=evaluate_results(candidate,reference)
        evidence['candidate_manifest_present']=(candidate/'manifest.json').is_file()
        evidence['candidate_status']=json.loads((candidate/'summary.json').read_text())['status'] if (candidate/'summary.json').is_file() else 'FAILED'
    finally:
        subprocess.run(['docker','rm','--force',name],capture_output=True,timeout=15)
        atomic_text(output/'isolation-evidence.json',json.dumps(sanitize(evidence),ensure_ascii=False,indent=2,allow_nan=False))
    return evidence


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--matrix',type=Path,required=True);parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--reference',type=Path)
    parser.add_argument('--image',default='emsolver-solver:local')
    args=parser.parse_args()
    result=run(**vars(args));print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result.get('container_exit_code')==0 and result.get('evaluation',{'passed':True}).get('passed') else 4


if __name__=='__main__':raise SystemExit(main())
