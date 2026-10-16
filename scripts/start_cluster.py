#!/usr/bin/env python3
"""Start a native control plane using a private generated configuration file."""
import argparse
import json
import os
from pathlib import Path
import stat
import sys


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,default=Path('deployment/cluster/private/native-server.json'))
    parser.add_argument('--data-dir',type=Path,default=Path('data'))
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=8000)
    args=parser.parse_args(argv)
    try:
        with args.config.open('rb') as source:
            info=os.fstat(source.fileno())
            if (args.config.is_symlink() or not stat.S_ISREG(info.st_mode)
                    or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)&0o077):
                raise ValueError('unsafe permissions')
            config=json.loads(source.read(65537))
        keys={'EMSOLVER_EXECUTION_MODE','EMSOLVER_DATABASE_URL','EMSOLVER_DATABASE_SCHEMA','EMSOLVER_IDENTITIES_FILE'}
        if (not isinstance(config,dict) or set(config)!=keys
                or any(not isinstance(value,str) for value in config.values())
                or config['EMSOLVER_EXECUTION_MODE']!='cluster'
                or not config['EMSOLVER_DATABASE_URL'].startswith(('postgres://','postgresql://'))
                or not 1<=args.port<=65535):
            raise ValueError('invalid configuration')
    except (OSError,ValueError,TypeError):
        parser.error('Cluster configuration is unavailable, invalid or not private to its owner')
    environment={**os.environ,**config,'EMSOLVER_DATA_DIR':str(args.data_dir.resolve())}
    command=[sys.executable,'-m','emsolver','serve','--host',args.host,'--port',str(args.port)]
    os.execve(sys.executable,command,environment)


if __name__=='__main__':main()
