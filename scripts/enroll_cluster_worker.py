#!/usr/bin/env python3
"""Enroll one project-scoped worker and save its credential to a private file."""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from emsolver.remote_worker import load_token,validate_server_url


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server',required=True)
    parser.add_argument('--admin-token-file',type=Path,required=True)
    parser.add_argument('--name',required=True)
    parser.add_argument('--project',action='append',required=True)
    parser.add_argument('--threads',type=int,default=1)
    parser.add_argument('--memory-gib',type=float,default=2)
    parser.add_argument('--disk-gib',type=float,default=4)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--allow-insecure-http',action='store_true')
    args=parser.parse_args(argv)
    if args.output.exists() or args.output.is_symlink():parser.error('Output exists; refusing to replace a credential')
    server=validate_server_url(args.server,allow_insecure_http=args.allow_insecure_http)
    import httpx
    with httpx.Client(trust_env=False,follow_redirects=False,timeout=15) as client:
        response=client.post(server+'/api/v1/cluster/workers',
            headers={'Authorization':'Bearer '+load_token(args.admin_token_file)},
            json={'name':args.name,'project_ids':args.project,'backends':['scipy'],
                  'resources':{'threads':args.threads,'memory_gib':args.memory_gib,'disk_gib':args.disk_gib}})
    if response.status_code!=201:
        print('Worker enrollment failed; HTTP '+str(response.status_code),file=sys.stderr);return 1
    value=response.json()
    args.output.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    descriptor=os.open(args.output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(descriptor,'w') as target:target.write(value['token']+'\n')
    print(json.dumps({'worker':value['worker'],'token_file':str(args.output.resolve())},ensure_ascii=False,indent=2))
    return 0


if __name__=='__main__':raise SystemExit(main())
