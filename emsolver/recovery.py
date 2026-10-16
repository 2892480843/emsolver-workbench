"""Read-only worker identity verification and safe non-child process adapters.

A PID alone never authorizes adoption or a signal. Business results and event
offset recovery remain the application's responsibility.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time

import psutil


MAX_SPEC_BYTES = 1024**2
UNKNOWN_RETURN_CODE = 255
_IDENTITY = re.compile(r'[A-Za-z0-9_-]{1,200}')
_DIGEST = re.compile(r'[a-f0-9]{64}')
_MODULE_ROOT = str(Path(__file__).resolve().parent.parent)


class _Rejected(ValueError):
    pass


def _token(info):
    return (info.st_dev,info.st_ino,info.st_mode,info.st_uid,info.st_size,info.st_mtime_ns,info.st_ctime_ns)


def _path(root,path,*,directory=False,missing_leaf=False,readonly=False):
    """Reject lexical escapes and symlinks in every controlled component."""
    root,path=Path(root),Path(path)
    if not path.is_absolute() or path!=path.resolve() or not path.is_relative_to(root):
        raise _Rejected('uncontrolled path')
    relative=path.relative_to(root)
    parts=[root,*[root.joinpath(*relative.parts[:index]) for index in range(1,len(relative.parts)+1)]]
    final=None
    for index,current in enumerate(parts):
        try:info=current.lstat()
        except FileNotFoundError:
            if missing_leaf and index==len(parts)-1:return None
            raise
        if stat.S_ISLNK(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o022:
            raise _Rejected('untrusted filesystem component')
        final=info
        is_directory=index<len(parts)-1 or directory
        if is_directory and not stat.S_ISDIR(info.st_mode):raise _Rejected('directory required')
        if not is_directory and not stat.S_ISREG(info.st_mode):raise _Rejected('regular file required')
    if readonly and final.st_mode&0o222:raise _Rejected('matrix is writable')
    return _token(final)


def _reject_pairs(pairs):
    value={}
    for key,item in pairs:
        if key in value:raise _Rejected('duplicate JSON field')
        value[key]=item
    return value


def _constant(_):
    raise _Rejected('nonfinite JSON constant')


def _canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)


def _read_spec(path,token):
    descriptor=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
    with os.fdopen(descriptor,'rb') as handle:
        info=os.fstat(handle.fileno())
        if _token(info)!=token or not stat.S_ISREG(info.st_mode) or info.st_size>MAX_SPEC_BYTES:
            raise _Rejected('invalid specification')
        data=handle.read(MAX_SPEC_BYTES+1)
        if len(data)>MAX_SPEC_BYTES or _token(os.fstat(handle.fileno()))!=token:
            raise _Rejected('specification changed')
    value=json.loads(data.decode('utf-8'),object_pairs_hook=_reject_pairs,parse_constant=_constant)
    if not isinstance(value,dict) or set(value)-{'config','input_path','output_dir','initial_subspace_path','resume_checkpoint_path','backend_profile'}:
        raise _Rejected('invalid worker specification fields')
    if 'initial_subspace_path' in value and 'resume_checkpoint_path' in value:
        raise _Rejected('ambiguous worker resumption')
    if not {'config','input_path','output_dir'}<=value.keys():raise _Rejected('missing worker specification fields')
    return value,hashlib.sha256(data).hexdigest()


def _sha(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda:handle.read(1024**2),b''):digest.update(block)
    return digest.hexdigest()


def _alive(process,birth):
    try:
        return (process.create_time()==birth and process.is_running() and
                process.status() not in {psutil.STATUS_ZOMBIE,psutil.STATUS_DEAD})
    except psutil.Error:return False


class RecoveredWorker:
    """Popen-shaped monitoring of a verified worker that is no longer a child.

    POSIX cannot provide the original exit code for an unrelated process.
    UNKNOWN_RETURN_CODE means 'no longer this live process', never success.
    """
    def __init__(self,process,birth,args,filesystem_check):
        self.pid=process.pid
        self.args=list(args)
        self._process=process
        self._birth=birth
        self._filesystem_check=filesystem_check
        self.returncode=None
        self.exit_status_known=False

    def _same_identity(self):
        try:
            return (_alive(self._process,self._birth) and self._process.cmdline()==self.args and
                    self._process.uids().real==os.getuid() and os.path.samefile(self._process.exe(),sys.executable) and
                    self._process.environ().get('PYTHONPATH')==_MODULE_ROOT and self._filesystem_check())
        except (psutil.Error,OSError,ValueError,TypeError):return False

    def poll(self):
        if self.returncode is not None:return self.returncode
        if not _alive(self._process,self._birth):self.returncode=UNKNOWN_RETURN_CODE
        return self.returncode

    def wait(self,timeout=None):
        started=time.monotonic()
        while True:
            code=self.poll()
            if code is not None:return code
            if timeout is not None and time.monotonic()-started>=timeout:
                raise subprocess.TimeoutExpired(self.args,timeout)
            time.sleep(.02 if timeout is None else max(.001,min(.02,timeout)))

    def kill(self):
        if not self._same_identity():return False
        try:self._process.kill();return True
        except psutil.NoSuchProcess:return False

    def terminate_tree(self,grace_seconds=.5):
        """Signal only the verified parent and birth-time-pinned descendants."""
        if not self._same_identity():return False
        try:
            children=[(child,child.create_time()) for child in self._process.children(recursive=True)]
        except psutil.Error:return False
        # The parent must still be verified before touching any descendant.
        for child,birth in reversed(children):
            if not self._same_identity():return False
            if _alive(child,birth):
                try:child.terminate()
                except psutil.NoSuchProcess:pass
        if not self._same_identity():return False
        try:self._process.terminate()
        except psutil.NoSuchProcess:pass
        deadline=time.monotonic()+min(max(float(grace_seconds),0.),5.)
        owned=children+[(self._process,self._birth)]
        while time.monotonic()<deadline and any(_alive(item,birth) for item,birth in owned):time.sleep(.02)
        # A descendant remains the same previously verified process after it
        # is reparented. Its pinned birth time prevents signalling PID reuse.
        for child,birth in owned:
            if not self._filesystem_check():return False
            if child.pid==self.pid:
                if self._same_identity():
                    try:child.kill()
                    except psutil.NoSuchProcess:pass
            elif _alive(child,birth):
                try:child.kill()
                except psutil.NoSuchProcess:pass
        return True


def verified_worker(gateway,job):
    """Return a safe adapter or None; verification never sends a signal.

    gateway supplies root and store.get(kind,id). Frozen job/spec dictionaries
    are compared canonically without filling or changing legacy defaults.
    """
    try:
        identity=job['id'];pid=job['worker_pid'];birth=job['worker_create_time']
        if (not isinstance(identity,str) or not _IDENTITY.fullmatch(identity) or type(pid) is not int or pid<=0 or
            type(birth) not in (float,int) or not math.isfinite(birth) or birth<=0 or
            job.get('status') not in {'RUNNING','VERIFYING','CANCELLING'}):return None
        root=Path(gateway.root).resolve()
        directory=root/'runs'/identity
        spec=directory/'worker.spec.json'
        spec_token=_path(root,spec)
        value,spec_digest=_read_spec(spec,spec_token)
        if _canonical(value['config'])!=_canonical(job['config']):return None
        if job['config'].get('solver',{}).get('backend')=='slepc':
            profile=job.get('runtime',{}).get('capabilities',{}).get('slepc')
            if not profile or _canonical(value.get('backend_profile'))!=_canonical(profile):return None
        elif 'backend_profile' in value:return None
        matrix=gateway.store.get('matrices',job['matrix_id'])
        if not isinstance(matrix,dict) or matrix.get('id')!=job['matrix_id']:return None
        source=Path(matrix['_path'])
        if not source.is_relative_to(root/'matrices') or value['input_path']!=str(source):return None
        input_token=_path(root,source,readonly=True)
        if input_token[4]>1024**3:return None
        expected_hash=job.get('matrix_hash')
        if not isinstance(expected_hash,str) or not _DIGEST.fullmatch(expected_hash) or expected_hash!=matrix.get('hash') or _sha(source)!=expected_hash:return None
        output=directory/'artifacts'
        if value['output_dir']!=str(output):return None
        _path(root,output,directory=True,missing_leaf=True)
        checkpoint=None;checkpoint_token=None
        if 'initial_subspace_path' in value or 'resume_checkpoint_path' in value:
            resumption=job.get('resumption',{})
            exact='resume_checkpoint_path' in value
            if exact!=(resumption.get('exact_resume') is True):return None
            if resumption.get('kind')!=('exact_block' if exact else 'subspace_warm_start'):return None
            source_job=resumption.get('source_job_id')
            if not isinstance(source_job,str) or not _IDENTITY.fullmatch(source_job):return None
            checkpoint=root/'runs'/source_job/'artifacts'/('exact-block.checkpoint.npz' if exact else 'subspace.checkpoint.npz')
            if value['resume_checkpoint_path' if exact else 'initial_subspace_path']!=str(checkpoint):return None
            checkpoint_token=_path(root,checkpoint)
            if checkpoint_token[4]>1024**3:return None
            if _sha(checkpoint)!=resumption.get('checkpoint_sha256'):return None
        elif job.get('resumption'):return None
        process=psutil.Process(pid)
        args=[sys.executable,'-m','emsolver.worker',str(spec)]
        command=process.cmdline()
        if (len(command)!=4 or command[1:]!=args[1:] or not os.path.samefile(command[0],sys.executable) or
            not os.path.samefile(process.exe(),sys.executable) or not _alive(process,birth) or
            process.uids().real!=os.getuid() or process.environ().get('PYTHONPATH')!=_MODULE_ROOT):return None
        # Preserve the exact verified executable spelling for subsequent checks.
        args=command
        def filesystem_check():
            try:
                if _path(root,spec)!=spec_token or _path(root,source,readonly=True)!=input_token:return False
                _path(root,output,directory=True,missing_leaf=True)
                if checkpoint is not None and _path(root,checkpoint)!=checkpoint_token:return False
                _,current_digest=_read_spec(spec,spec_token)
                return current_digest==spec_digest
            except (OSError,ValueError,TypeError):return False
        if not filesystem_check():return None
        return RecoveredWorker(process,birth,args,filesystem_check)
    except (KeyError,ValueError,TypeError,OSError,psutil.Error):return None
