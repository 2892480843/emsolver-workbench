"""Optional Metal FP32 sparse correction work with authoritative CPU FP64 checks.

This hybrid profile accelerates only the sparse products inside inexact JD
corrections. It never claims GPU FP64, CUDA, a GPU factorization, or a speedup.
"""
from __future__ import annotations

from importlib import metadata, util
import platform
import time

import numpy as np
from scipy import sparse


def probe_metal():
    result = {'available': False, 'supported': False, 'backend': 'MLX_Metal',
              'cuda_supported': False, 'gpu_precision': 'float32_or_complex64',
              'authoritative_validation': 'CPU_float64_original_problem',
              'profiles': ['metal_jd_correction_v1'], 'version': None,
              'reason': 'Requires optional MLX and actual Apple Metal hardware'}
    if platform.system() != 'Darwin' or platform.machine() != 'arm64' or util.find_spec('mlx') is None:
        return result
    try:
        import mlx.core as mx
        if not mx.metal.is_available():
            return result
        with mx.stream(mx.gpu):
            values = mx.zeros(2).at[mx.array([0, 1, 1])].add(mx.array([2., 3., 4.]))
            mx.eval(values)
        if not np.array_equal(np.asarray(values), np.array([2., 7.])):
            result['reason'] = 'Actual Metal sparse scatter probe failed'
            return result
        result.update(available=True, supported=True, version=metadata.version('mlx'), reason=None)
    except (ImportError, OSError, RuntimeError, ValueError, metadata.PackageNotFoundError):
        result['reason'] = 'Optional Metal runtime is not runnable'
    return result


def validate_gpu_profile(config, *, require_available=True):
    solver, resources = config['solver'], config['resources']
    if (solver['backend'] != 'scipy' or solver['method'] != 'jacobi_davidson'
            or solver['transform'] != 'precond' or solver['linear']['method'] != 'gmres'
            or solver['linear']['precision'] != 'mixed_float32' or resources['mpi_ranks'] != 1):
        raise ValueError('UNSUPPORTED_GPU_PROFILE: Metal supports only single-rank mixed-precision JD correction products')
    if require_available and not probe_metal()['supported']:
        raise ValueError('BACKEND_UNAVAILABLE: runnable Apple Metal and MLX are required')
    return 'metal_jd_correction_v1'


class MetalSparseOperator:
    """Bounded COO gather/scatter; memory scales with nnz+n, never n squared."""

    def __init__(self, matrix, *, memory_limit_bytes):
        if not probe_metal()['supported']:
            raise ValueError('BACKEND_UNAVAILABLE: actual Metal device is required')
        if not sparse.issparse(matrix) or matrix.ndim != 2 or matrix.shape[0] != matrix.shape[1]:
            raise ValueError('INVALID_GPU_INPUT: square sparse matrix required')
        if type(memory_limit_bytes) is not int or memory_limit_bytes <= 0:
            raise ValueError('INVALID_GPU_BUDGET')
        if not np.isfinite(matrix.data).all():
            raise ValueError('INVALID_GPU_INPUT: finite coefficients required')
        self.n, self.nnz = matrix.shape[0], matrix.nnz
        if self.n > np.iinfo(np.int32).max or self.nnz > 100_000_000:
            raise ValueError('GPU_INPUT_BUDGET_EXCEEDED')
        # Include coordinates, real/imag coefficients and gather/reduce scratch.
        self.estimated_bytes = int(self.nnz * 40 + self.n * 64)
        if self.estimated_bytes > memory_limit_bytes * .5:
            raise ValueError('RESOURCE_ESTIMATE_EXCEEDED: Metal COO and scratch budget')
        coo = matrix.tocoo(copy=False)
        import mlx.core as mx
        self.mx = mx
        self.is_complex = np.iscomplexobj(matrix.data)
        self.calls = 0
        self.seconds = 0.
        self.peak = 0
        self.memory_limit = memory_limit_bytes
        mx.set_memory_limit(memory_limit_bytes)
        mx.set_cache_limit(0)
        mx.reset_peak_memory()
        with mx.stream(mx.gpu):
            self.rows = mx.array(np.asarray(coo.row, dtype=np.int32))
            self.cols = mx.array(np.asarray(coo.col, dtype=np.int32))
            self.real = mx.array(np.asarray(coo.data.real, dtype=np.float32))
            self.imag = mx.array(np.asarray(coo.data.imag, dtype=np.float32)) if self.is_complex else None
            mx.eval(self.rows, self.cols, self.real)
            if self.imag is not None:
                mx.eval(self.imag)
        self._check_memory()

    def _check_memory(self):
        # MLX documents set_memory_limit as a guideline, not an allocation
        # ceiling. Reject a measured overrun before returning any product.
        self.peak=max(self.peak,int(self.mx.get_peak_memory()))
        if self.peak>self.memory_limit or int(self.mx.get_active_memory())>self.memory_limit:
            raise RuntimeError('RESOURCE_EXHAUSTED: actual Metal allocation exceeded configured budget')

    def matvec(self, vector):
        vector = np.asarray(vector)
        if vector.shape != (self.n,) or not np.isfinite(vector).all():
            raise ValueError('INVALID_GPU_VECTOR: finite original-dimension vector required')
        mx = self.mx
        start = time.monotonic()
        complex_output = self.is_complex or np.iscomplexobj(vector)
        with mx.stream(mx.gpu):
            real = mx.array(np.asarray(vector.real, dtype=np.float32))
            imag = mx.array(np.asarray(vector.imag, dtype=np.float32)) if complex_output else None
            product_real = self.real * real[self.cols]
            if self.imag is not None:
                product_real = product_real - self.imag * imag[self.cols]
            output_real = mx.zeros(self.n, dtype=mx.float32).at[self.rows].add(product_real)
            if complex_output:
                product_imag = self.real * imag[self.cols]
                if self.imag is not None:
                    product_imag = product_imag + self.imag * real[self.cols]
                output_imag = mx.zeros(self.n, dtype=mx.float32).at[self.rows].add(product_imag)
                mx.eval(output_real, output_imag)
                result = np.asarray(output_real) + 1j * np.asarray(output_imag)
            else:
                mx.eval(output_real)
                result = np.asarray(output_real)
        if not np.isfinite(result).all():
            raise ValueError('GPU_NONFINITE_RESULT')
        self.calls += 1
        self.seconds += time.monotonic() - start
        self._check_memory()
        return result

    def metrics(self):
        return {'gpu_backend': 'MLX_Metal_sparse_COO', 'gpu_profile': 'metal_jd_correction_v1',
                'gpu_precision': 'float32_or_complex64', 'gpu_matvec_calls': self.calls,
                'gpu_nnz': self.nnz, 'gpu_seconds': self.seconds,
                'gpu_peak_allocated_bytes': self.peak, 'gpu_memory_limit_bytes': self.memory_limit,
                'gpu_memory_enforcement':'MLX guideline plus measured allocation guard after each evaluated operation',
                'gpu_estimated_working_bytes': self.estimated_bytes, 'gpu_full_matrix_dense': False,
                'cpu_authoritative_precision': 'float64_or_complex128'}
