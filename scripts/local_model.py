#!/usr/bin/env python3
"""Verify/start an already downloaded, pinned optional explanation model."""
from pathlib import Path
import argparse
import json
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from emsolver.model_runtime import verify_model,serve_model,local_config


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['verify','configure','serve'])
    parser.add_argument('--directory',type=Path,required=True)
    parser.add_argument('--port',type=int,default=18321)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    evidence=verify_model(args.directory)
    if args.action=='verify':print(json.dumps(evidence,indent=2));return 0
    if args.action=='configure':
        if args.output is None:parser.error('--output is required for configure')
        if args.output.exists() or args.output.is_symlink():parser.error('output must be new')
        with args.output.open('x',encoding='utf-8') as handle:json.dump(local_config(args.port),handle,indent=2)
        args.output.chmod(0o600);return 0
    return serve_model(args.directory,args.port)

if __name__=='__main__':raise SystemExit(main())
