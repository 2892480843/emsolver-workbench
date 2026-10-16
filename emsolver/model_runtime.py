"""Pinned, locally verified optional Qwen explanation runtime; never solver input."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

REPOSITORY='mlx-community/Qwen2.5-0.5B-Instruct-4bit'
REVISION='a5339a4131f135d0fdc6a5c8b5bbed2753bbe0f3'
UPSTREAM='Qwen/Qwen2.5-0.5B-Instruct'
UPSTREAM_REVISION='7ae557604adf67be50417f59c2c2f167def9a775'
WEIGHTS_BYTES=278064920
FILES={
 'model.safetensors':'ddffab9cbc7bf6dde941c6724841eeca8981fcfa81ca20ff8efff1396326d153',
 'tokenizer_config.json':'f7c61e32b7a17d19bf8e7037dcb74079a833e53ea9801f24008cac68458f03b7',
 'special_tokens_map.json':'76862e765266b85aa9459767e33cbaf13970f327a0e88d1c65846c2ddd3a1ecd',
 'config.json':'b045e57ea90b8f1b35f89f954b176a5c1faa02bd0af2c89bcec191239d66cef4',
 'tokenizer.json':'a8506e7111b80c6d8635951a02eab0f4e1a8e4e5772da83846579e97b16f61bf',
 'README.md':'8cd48183a9d10279e02291f12f03a2db5585c222a0cf77f97dca3c87f79bd5e1',
 'merges.txt':'8831e4f1a044471340f7c0a83d7bd71306a5b867e95fd870f74d0c5308a904d5',
 'LICENSE.upstream':'832dd9e00a68dd83b3c3fb9f5588dad7dcf337a0db50f7d9483f310cd292e92e',
 'vocab.json':'ca10d7e9fb3ed18575dd1e277a2579c16d108e32f27439684afa0e10b1440910'}


def verify_model(directory):
    directory=Path(directory).absolute()
    if directory.is_symlink() or not directory.is_dir():raise ValueError('MODEL_DIRECTORY_UNSAFE')
    for parent in directory.parents:
        if parent.is_symlink():raise ValueError('MODEL_DIRECTORY_UNSAFE')
    checked={}
    for name,expected in FILES.items():
        path=directory/name
        size=WEIGHTS_BYTES if name=='model.safetensors' else 16*1024**2
        if path.is_symlink() or not path.is_file() or path.stat().st_size>size:
            raise ValueError('MODEL_FILE_MISSING_OR_OVERSIZED: '+name)
        with path.open('rb') as handle:actual=hashlib.file_digest(handle,'sha256').hexdigest()
        if actual!=expected:raise ValueError('MODEL_HASH_MISMATCH: '+name)
        checked[name]=actual
    return {'schema_version':'1.0','repository':REPOSITORY,'revision':REVISION,
            'source':f'https://huggingface.co/{REPOSITORY}/tree/{REVISION}',
            'upstream':UPSTREAM,'upstream_revision':UPSTREAM_REVISION,'license':'Apache-2.0',
            'license_source':f'https://huggingface.co/{UPSTREAM}/blob/{UPSTREAM_REVISION}/LICENSE',
            'quantization':{'bits':4,'group_size':64},'remote_code':False,'files':checked}


def local_config(port=18321):
    if type(port) is not int or not 1024<=port<=65535:raise ValueError('INVALID_MODEL_PORT')
    return {'endpoint':f'http://127.0.0.1:{port}','model':'default_model',
            'model_version':f'{REPOSITORY}@{REVISION}', 'license_id':'Apache-2.0',
            'license_approved':True,'timeout_seconds':10.,'max_response_bytes':65536}


def serve_model(directory,port=18321):
    evidence=verify_model(directory)
    local_config(port)
    from importlib import metadata
    if sys.platform!='darwin' or metadata.version('mlx-lm')!='0.32.1':
        raise ValueError('MODEL_RUNTIME_UNAVAILABLE: validated macOS MLX-lm 0.32.1 required')
    import mlx.core as mx
    if not mx.metal.is_available():raise ValueError('MODEL_RUNTIME_UNAVAILABLE: actual Apple Metal required')
    # Offline flags and trust_remote_code=False are effective before model load.
    environment={**os.environ,'HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1',
                 'HF_HUB_DISABLE_IMPLICIT_TOKEN':'1','HF_HUB_DISABLE_TELEMETRY':'1'}
    for name in ('HF_TOKEN','HUGGING_FACE_HUB_TOKEN','OPENAI_API_KEY','HTTP_PROXY','HTTPS_PROXY','ALL_PROXY'):
        environment.pop(name,None)
    command=[sys.executable,'-m','mlx_lm.server','--model',str(Path(directory).resolve()),
             '--host','127.0.0.1','--port',str(port),'--max-tokens','512']
    print(json.dumps({'model_verified':True,'repository':evidence['repository'],'revision':REVISION,
                      'endpoint':local_config(port)['endpoint'],'network_downloads':False}),flush=True)
    return subprocess.call(command,env=environment)
