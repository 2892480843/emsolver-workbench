"""Request identity and project roles; local CLI remains an explicit owner."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import re

from .errors import ServiceError


@dataclass(frozen=True)
class Identity:
    subject: str
    global_admin: bool = False
    mode: str = 'token_users'
    operations_admin: bool = False


CURRENT = ContextVar('emsolver_identity', default=Identity('local-user', True, 'local'))


def current_identity():
    return CURRENT.get()


@contextmanager
def identity_context(identity):
    token=CURRENT.set(identity)
    try:yield identity
    finally:CURRENT.reset(token)


def configured_users():
    path=os.environ.get('EMSOLVER_IDENTITIES_FILE')
    if not path:return []
    try:
        with Path(path).open('rb') as source:raw=source.read(1024**2+1)
        if len(raw)>1024**2:raise ValueError('oversized')
        value=json.loads(raw)
        if not isinstance(value,dict) or set(value)!={'users'}:raise ValueError('invalid identity document')
        users=value['users']
        if not isinstance(users,list) or not 1<=len(users)<=1000:raise ValueError('invalid users')
        seen=set();tokens=set()
        for item in users:
            if (not isinstance(item,dict) or set(item)-{'subject','token_sha256','global_admin','operations_admin'} or
                not isinstance(item.get('subject'),str) or not re.fullmatch(r'[A-Za-z0-9_.@-]{1,128}',item['subject']) or
                not isinstance(item.get('token_sha256'),str) or not re.fullmatch(r'[0-9a-f]{64}',item['token_sha256']) or
                type(item.get('global_admin',False)) is not bool or type(item.get('operations_admin',False)) is not bool or
                item['subject'] in seen or item['token_sha256'] in tokens):
                raise ValueError('invalid account')
            seen.add(item['subject']);tokens.add(item['token_sha256'])
        if not any(item.get('global_admin') for item in users):raise ValueError('owner required')
        return users
    except (OSError,ValueError,TypeError,KeyError):
        raise ServiceError('IDENTITY_CONFIGURATION_INVALID','Identity configuration is unavailable',503) from None


def authenticate(authorization):
    users=configured_users()
    if users:
        token=authorization[7:] if authorization.startswith('Bearer ') else ''
        digest=hashlib.sha256(token.encode()).hexdigest()
        for user in users:
            if token and hmac.compare_digest(digest,user['token_sha256']):
                return Identity(user['subject'],user.get('global_admin',False),operations_admin=user.get('operations_admin',False))
        raise ServiceError('UNAUTHORIZED','Valid bearer token required',401)
    token=os.environ.get('EMSOLVER_API_TOKEN')
    if token and not hmac.compare_digest(authorization,'Bearer '+token):
        raise ServiceError('UNAUTHORIZED','Valid bearer token required',401)
    return Identity('local-user',True,'shared-token' if token else 'local')


def project_role(project):
    identity=current_identity()
    if identity.global_admin:return 'admin'
    return next((member['role'] for member in project.get('members',[]) if member['subject']==identity.subject),None)


def can_read(project):return project_role(project) in {'viewer','analyst','admin'}


def require_project(project,permission='read'):
    role=project_role(project)
    allowed={'read':{'viewer','analyst','admin'},'write':{'analyst','admin'},'admin':{'admin'}}[permission]
    if role not in allowed:raise ServiceError('FORBIDDEN','Project role does not permit this operation',403)


def require_admin():
    if not current_identity().global_admin:raise ServiceError('FORBIDDEN','Workspace administrator required',403)


def require_operations():
    actor=current_identity()
    if not (actor.global_admin or actor.operations_admin):
        raise ServiceError('FORBIDDEN','Operations administrator required',403)
