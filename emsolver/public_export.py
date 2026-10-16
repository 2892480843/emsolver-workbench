"""Fail closed on private runtime metadata in public reproducibility archives."""
from __future__ import annotations
import json
from pathlib import PurePosixPath
import re
import stat
import zipfile
from .reporting import PRIVATE_PATH,IDENTITY_KEYS,SENSITIVE,credential_query

CREDENTIAL=re.compile(r'Bearer\s+[A-Za-z0-9_.~+/-]{8,}')


def scan_public_archive(path):
    problems=[];checked=0
    with zipfile.ZipFile(path) as archive:
        names=[entry.filename for entry in archive.infolist()]
        if len(names)!=len(set(names)):problems.append({'member':'archive','code':'DUPLICATE_MEMBER'})
        for entry in archive.infolist():
            name=entry.filename;relative=PurePosixPath(name)
            if relative.is_absolute() or '..' in relative.parts or '\\' in name or stat.S_ISLNK(entry.external_attr>>16):
                problems.append({'member':name,'code':'UNSAFE_MEMBER'});continue
            # Source code contains intentional path-redaction patterns. Inspect
            # generated runtime data, never mistake that code for leaked data.
            if name not in {'job.json','events.json'} and not name.startswith('run/'):continue
            if not name.endswith(('.json','.jsonl','.yaml')):continue
            if entry.file_size>16*1024**2:
                problems.append({'member':name,'code':'TEXT_EXPORT_TOO_LARGE'});continue
            text=archive.read(entry).decode('utf-8');checked+=1
            if PRIVATE_PATH.search(text):problems.append({'member':name,'code':'PRIVATE_PATH'})
            if CREDENTIAL.search(text):problems.append({'member':name,'code':'CREDENTIAL'})
            if name.endswith('.json'):
                try:value=json.loads(text)
                except ValueError:
                    problems.append({'member':name,'code':'INVALID_JSON'});continue
                def visit(item):
                    if isinstance(item,dict):
                        for key,child in item.items():
                            if key.lower() in IDENTITY_KEYS|SENSITIVE or any(word in key.lower() for word in ('password','secret','token')):
                                problems.append({'member':name,'code':'PRIVATE_FIELD'})
                            visit(child)
                    elif isinstance(item,list):
                        for child in item:visit(child)
                    elif isinstance(item,str):
                        from urllib.parse import urlsplit,parse_qsl
                        try:
                            parsed=urlsplit(item)
                            if parsed.scheme in {'http','https'} and parsed.netloc:
                                if parsed.username is not None or parsed.password is not None or any(credential_query(key) and value!='[redacted]' for key,value in parse_qsl(parsed.query)):
                                    problems.append({'member':name,'code':'URL_CREDENTIAL'})
                        except ValueError:problems.append({'member':name,'code':'INVALID_URL'})
                visit(value)
    return {'passed':not problems,'checked_runtime_members':checked,'problems':problems,
            'scope':'Runtime text, safe archive paths and explicit identity/credential fields; numerical arrays require frozen public permission'}
