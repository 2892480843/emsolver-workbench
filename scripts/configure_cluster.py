#!/usr/bin/env python3
"""Create private deployment credentials without printing or overwriting them."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=Path('deployment/cluster'))
    parser.add_argument('--api-port', type=int, default=8000)
    parser.add_argument('--postgres-port', type=int, default=15483)
    args = parser.parse_args(argv)
    if not all(1 <= port <= 65535 for port in (args.api_port,args.postgres_port)):
        parser.error('Ports must be from 1 to 65535')
    if args.directory.is_symlink():parser.error('Unsafe configuration directory')
    root=args.directory.resolve();root.mkdir(parents=True,exist_ok=True)
    private=root/'private'
    if root.is_symlink() or private.is_symlink():parser.error('Unsafe configuration directory')
    if (root/'.env').exists() or private.exists():parser.error('Configuration already exists; refusing to replace credentials')
    private.mkdir(mode=0o700)
    users=[]
    for subject,admin in [('local-user',True),('analyst-a',False),('viewer-a',False)]:
        token=secrets.token_urlsafe(48)
        path=private/(subject+'.token');path.write_text(token+'\n');path.chmod(0o600)
        users.append({'subject':subject,'token_sha256':hashlib.sha256(token.encode()).hexdigest(),'global_admin':admin})
    path=private/'identities.json';path.write_text(json.dumps({'users':users},indent=2)+'\n');path.chmod(0o600)
    password=secrets.token_urlsafe(36)
    env={'EMSOLVER_POSTGRES_PASSWORD':password,'EMSOLVER_UID':str(os.getuid()),'EMSOLVER_GID':str(os.getgid()),
         'EMSOLVER_API_PORT':str(args.api_port),'EMSOLVER_PG_PORT':str(args.postgres_port)}
    path=root/'.env';path.write_text(''.join(f'{key}={value}\n' for key,value in env.items()));path.chmod(0o600)
    path=private/'native-server.json'
    path.write_text(json.dumps({'EMSOLVER_EXECUTION_MODE':'cluster',
        'EMSOLVER_DATABASE_URL':f'postgresql://emsolver:{password}@127.0.0.1:{args.postgres_port}/emsolver',
        'EMSOLVER_DATABASE_SCHEMA':'emsolver','EMSOLVER_IDENTITIES_FILE':str(private/'identities.json')},indent=2)+'\n');path.chmod(0o600)
    (root/'data').mkdir(mode=0o700)
    print(json.dumps({'directory':str(root),'owner_token_file':str(private/'local-user.token'),
        'identities':['local-user','analyst-a','viewer-a'],'note':'Non-owner accounts need explicit project membership. Credentials were not displayed.'},ensure_ascii=False,indent=2))
    return 0


if __name__=='__main__':raise SystemExit(main())
