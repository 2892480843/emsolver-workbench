"""Sparse Hermitian CPU solver with independent, conservative certification.

Only candidate vectors and explicitly bounded projected matrices are dense.
The original and shifted matrices are always sparse.
"""
from .core import capabilities, generate_sample, inspect_matrix, load_matrix, run_solver, verify_run
from .backend import SolverBackend, create_backend, algorithm_plan, estimate_resources

__all__ = ['load_matrix', 'inspect_matrix', 'run_solver', 'verify_run', 'generate_sample', 'capabilities',
           'SolverBackend', 'create_backend', 'algorithm_plan', 'estimate_resources']
