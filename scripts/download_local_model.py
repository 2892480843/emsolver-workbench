#!/usr/bin/env python3
"""Download the pinned optional model over verified HTTPS; publish only by SHA-256.

Requires curl. It never executes repository code, uses implicit HF credentials,
prints signed redirect URLs, or disables certificate verification. A partial
prefix and bounded range shards are retained on failure for another attempt.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from emsolver.model_runtime import FILES,REPOSITORY,REVISION,UPSTREAM,UPSTREAM_REVISION,WEIGHTS_BYTES,verify_model


def digest(path):
    with Path(path).open('rb') as handle:return hashlib.file_digest(handle,'sha256').hexdigest()


def partition(start,total,count):
    if type(start) is not int or type(total) is not int or type(count) is not int or not 0<=start<=total or not 1<=count<=4:
        raise ValueError('INVALID_RANGE_PARTITION')
    remaining=total-start
    count=min(count,remaining)
    if count==0:return []
    size=(remaining+count-1)//count
    return [(offset,min(total-1,offset+size-1)) for offset in range(start,total,size)]


def valid_range(headers,start,end,total,received):
    # Redirect and CONNECT headers can precede the final origin response.
    blocks=re.split(r'(?=HTTP/\S+\s+\d{3})',headers)
    final=next((block for block in reversed(blocks) if re.match(r'HTTP/\S+\s+\d{3}',block)), '')
    status=re.match(r'HTTP/\S+\s+(\d{3})',final)
    values=re.findall(r'^content-range:\s*bytes\s+(\d+)-(\d+)/(\d+)\s*$',final,re.I|re.M)
    return bool(status and status[1]=='206' and values==[(str(start),str(end),str(total))] and 0<received<=end-start+1)


def curl_request(url,output,headers,maximum,byte_range=None):
    command=['curl','--disable','--silent','--show-error','--fail','--location','--proto','=https','--proto-redir','=https',
             '--connect-timeout','10','--max-time','30','--max-filesize',str(maximum),'--retry','0',
             '--dump-header',str(headers),'--output',str(output)]
    if byte_range is not None:command+=['--range',f'{byte_range[0]}-{byte_range[1]}']
    # No Authorization header or tokens are supplied. Redirect URLs stay in
    # private temporary headers which are removed after validation.
    return subprocess.run(command+[url],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=35).returncode


def download(directory,parallel=4):
    if type(parallel) is not int or not 1<=parallel<=4:raise ValueError('INVALID_PARALLELISM')
    if not shutil.which('curl'):raise ValueError('CURL_REQUIRED')
    directory=Path(directory).absolute()
    if directory.is_symlink() or any(parent.is_symlink() for parent in directory.parents):raise ValueError('UNSAFE_MODEL_DIRECTORY')
    directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    directory.chmod(0o700)
    for name,expected in FILES.items():
        if name=='model.safetensors':continue
        path=directory/name
        if path.is_symlink():raise ValueError('UNSAFE_MODEL_FILE')
        if path.exists():
            if not path.is_file() or path.stat().st_size>16*1024**2 or digest(path)!=expected:raise ValueError('PINNED_METADATA_MISMATCH: '+name)
            continue
        url=(f'https://huggingface.co/{UPSTREAM}/resolve/{UPSTREAM_REVISION}/LICENSE' if name=='LICENSE.upstream' else
             f'https://huggingface.co/{REPOSITORY}/resolve/{REVISION}/{name}')
        with tempfile.TemporaryDirectory(prefix='.metadata-',dir=directory) as temporary:
            target,headers=Path(temporary)/'body',Path(temporary)/'headers'
            result=curl_request(url,target,headers,16*1024**2)
            if result or not target.is_file() or digest(target)!=expected:raise ValueError('PINNED_METADATA_DOWNLOAD_FAILED: '+name)
            target.replace(path);path.chmod(0o400)
    target=directory/'model.safetensors'
    prefix=directory/'model.safetensors.part'
    if target.is_symlink() or prefix.is_symlink() or (prefix.exists() and not prefix.is_file()):raise ValueError('UNSAFE_MODEL_FILE')
    if target.exists():
        if target.stat().st_size!=WEIGHTS_BYTES or digest(target)!=FILES['model.safetensors']:raise ValueError('PINNED_WEIGHT_MISMATCH')
        initial=WEIGHTS_BYTES;ranges=[]
    else:
        initial=prefix.stat().st_size if prefix.is_file() else 0
        if initial>WEIGHTS_BYTES:raise ValueError('OVERSIZED_PARTIAL_WEIGHT')
        ranges=partition(initial,WEIGHTS_BYTES,parallel)
        url=f'https://huggingface.co/{REPOSITORY}/resolve/{REVISION}/model.safetensors'
        def shard(bounds):
            start,end=bounds
            path=directory/f'.weight-range-{start}-{end}.part'
            if path.is_symlink():raise ValueError('UNSAFE_MODEL_FILE')
            if path.exists() and (not path.is_file() or path.stat().st_size>end-start+1):raise ValueError('OVERSIZED_RANGE_SHARD')
            for attempt in range(100):
                size=path.stat().st_size if path.is_file() else 0
                if size==end-start+1:return path
                request_start=start+size
                with tempfile.TemporaryDirectory(prefix='.range-',dir=directory) as temporary:
                    part,headers=Path(temporary)/'body',Path(temporary)/'headers'
                    try:curl_request(url,part,headers,end-request_start+1,(request_start,end))
                    except subprocess.TimeoutExpired:pass
                    received=part.stat().st_size if part.is_file() else 0
                    header_text=headers.read_text(encoding='iso-8859-1') if headers.is_file() else ''
                    if valid_range(header_text,request_start,end,WEIGHTS_BYTES,received):
                        with path.open('ab') as destination,part.open('rb') as source:shutil.copyfileobj(source,destination,1024**2)
                        path.chmod(0o600)
                        print(json.dumps({'range_start':start,'range_end':end,'received_bytes':size+received,'attempt':attempt+1}),flush=True)
                    else:time.sleep(1)
            raise ValueError('BOUNDED_RANGE_RETRIES_EXHAUSTED')
        with ThreadPoolExecutor(max_workers=parallel) as pool:parts=list(pool.map(shard,ranges))
        assembled=directory/'.weights-assembled.part'
        if assembled.is_symlink():raise ValueError('UNSAFE_MODEL_FILE')
        with assembled.open('wb') as destination:
            if initial:
                with prefix.open('rb') as source:shutil.copyfileobj(source,destination,1024**2)
            for part in parts:
                with part.open('rb') as source:shutil.copyfileobj(source,destination,1024**2)
        assembled.chmod(0o600)
        if assembled.stat().st_size!=WEIGHTS_BYTES or digest(assembled)!=FILES['model.safetensors']:
            raise ValueError('PINNED_WEIGHT_HASH_MISMATCH; partial bytes retained, model not published')
        assembled.replace(target);target.chmod(0o400)
        for part in parts:part.unlink()
        prefix.unlink(missing_ok=True)
    verified=verify_model(directory)
    provenance={**verified,'downloaded_at':datetime.now(timezone.utc).isoformat(),
                'transport':{'source':'pinned official Hugging Face repositories','tls_verification':True,'implicit_credentials':False,
                             'initial_prefix_bytes':initial,'range_parallelism':parallel,'strict_http_206_content_range':True,
                             'requested_ranges':[{'start':start,'end':end} for start,end in ranges]},
                'weights_bytes':WEIGHTS_BYTES,'complete':True}
    path=directory/'download-provenance.json'
    if path.is_symlink():raise ValueError('UNSAFE_PROVENANCE_FILE')
    if path.exists():
        previous=json.loads(path.read_text(encoding='utf-8'))
        if previous.get('files')==verified['files'] and previous.get('revision')==REVISION and previous.get('complete') is True:return previous
        raise ValueError('EXISTING_PROVENANCE_MISMATCH')
    with path.open('x',encoding='utf-8') as handle:json.dump(provenance,handle,ensure_ascii=False,indent=2,allow_nan=False)
    path.chmod(0o400)
    return provenance


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',type=Path,required=True)
    parser.add_argument('--parallel',type=int,default=4,choices=range(1,5))
    args=parser.parse_args()
    try:result=download(args.directory,args.parallel)
    except (ValueError,OSError) as error:print(json.dumps({'status':'failed','reason':str(error).split(':')[0]}),file=sys.stderr);return 1
    print(json.dumps(result,ensure_ascii=False,allow_nan=False));return 0


if __name__=='__main__':raise SystemExit(main())
