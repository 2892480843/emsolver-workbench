"""Minimal environment for trusted local numerical and validation children."""
from __future__ import annotations
import os
from pathlib import Path


def child_environment(threads):
    allowed=('PATH','LANG','LC_ALL','TMPDIR','SYSTEMROOT','WINDIR','EMSOLVER_SLEPC_IMAGE','EMSOLVER_DOCKER_SOCKET')
    environment={key:os.environ[key] for key in allowed if key in os.environ}
    environment.update(PYTHONPATH=str(Path(__file__).resolve().parent.parent),PYTHONNOUSERSITE='1',PYTHONDONTWRITEBYTECODE='1',PYTHONUNBUFFERED='1')
    for key in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','VECLIB_MAXIMUM_THREADS'):
        environment[key]=str(threads)
    return environment
