"""Explicit resource-preflighted CPU ILU policies for JD corrections.

An incomplete factor is only an approximate inverse for GMRES. It never
replaces the original matrix, eigenvalue target, or FP64 residual checks.
"""
from __future__ import annotations

import math
import time
import warnings
from copy import deepcopy

import numpy as np
from scipy import linalg as dense
from scipy.sparse import linalg as sla


ILU_POLICY = {
    'id': 'ilu_budgeted_v1', 'drop_tol': 1e-3, 'fill_factor': 2.,
    'drop_rule': 'basic,area', 'permc_spec': 'COLAMD',
    'reuse': 'one_factorization_per_solve_fixed_target_shift',
    'shift_policy': 'target_scaled_plus_sqrt_epsilon_norm_floor',
    'factor_storage_bound': False,
}

ILU_V2_POLICY = {
    **ILU_POLICY, 'id': 'ilu_equilibrated_compensated_v2',
    'equilibration_sweeps': 4, 'equilibration': 'symmetric_max_norm_Ruiz',
    'scaling_step_clip': [1e-100, 1e100], 'diagonal_row_l1_floor': .01,
    'zero_diagonal_sign': 'positive', 'constraint': 'cached_block_projected_inverse',
    'constraint_schur_condition_max': 1e12, 'constraint_orthogonality_max': 1e-8,
    'retry_or_profile_fallback': False,
}


def ilu_policy(profile):
    return deepcopy(ILU_V2_POLICY if profile == ILU_V2_POLICY['id'] else ILU_POLICY)


def _divide_components(value, scale):
    """Avoid complex division squaring an extreme real denominator."""
    result = value.copy()
    if np.iscomplexobj(result):
        result.real[:] /= scale
        result.imag[:] /= scale
    else:
        result[:] /= scale
    return result


def storage_estimate(n, nnz, itemsize, *, profile='ilu_budgeted_v1', subspace_max=0):
    """Planning allocations; SuperLU fill policy is not an RSS upper bound.

    Reserve each L/U separately at fill_factor*nnz, with conservative 64-bit
    indices even on builds using C int. Include a 64-column panel scenario,
    symbolic/permutation storage and transient shifted CSC construction.
    Actual factors stay internal: accessing .L/.U would export large copies.
    """
    shifted_nnz = min(n*n, nnz+n)
    sparse_bytes = shifted_nnz*(itemsize+8)+(n+1)*8
    factor_nnz = math.ceil(ILU_POLICY['fill_factor']*shifted_nnz)
    factor_bytes = factor_nnz*(itemsize+8)+(n+1)*8
    result = {
        'ilu_shifted_csc_bytes': 2*sparse_bytes,
        'ilu_l_factor_bytes': factor_bytes,
        'ilu_u_factor_bytes': factor_bytes,
        'ilu_symbolic_permutation_bytes': 16*shifted_nnz+64*n,
        'ilu_panel_workspace_bytes': n*(64*(itemsize+8)+64),
        'ilu_solve_workspace_bytes': 8*n*itemsize,
    }
    if profile == ILU_V2_POLICY['id']:
        m = min(n, subspace_max)
        result.update(ilu_v2_equilibration_sparse_scratch_bytes=2*sparse_bytes+2*shifted_nnz*8,
                      ilu_v2_scaling_diagonal_vectors_bytes=12*n*8,
                      # Old closures can keep the previous outer cache alive
                      # while its replacement is constructed.
                      ilu_v2_constraint_vectors_bytes=4*n*m*itemsize,
                      ilu_v2_constraint_small_workspace_bytes=8*m*m*itemsize,
                      ilu_v2_scaling_history_bytes=4096)
    return result


class BudgetedILU:
    def __init__(self, factor, runtime, counters):
        self.factor = factor
        self.runtime = runtime
        self.counters = counters

    @classmethod
    def build(cls, matrix, sigma_scaled, config, runtime, counters):
        if config['solver']['linear']['preconditioner'] == ILU_V2_POLICY['id']:
            return _build_v2(matrix, sigma_scaled, config, runtime, counters)
        from . import core
        from .backend import estimate_resources
        if (config['resources']['device'] != 'cpu' or
                config['solver']['linear']['precision'] != 'float64' or
                matrix.dtype not in (np.dtype('float64'), np.dtype('complex128'))):
            raise ValueError('UNSUPPORTED_PRECONDITIONER: CPU float64/complex128 ILU required')
        runtime.check()
        # This check precedes CSC, symbolic and factor allocations, and the
        # caller constructs the dense search basis only after it succeeds.
        estimate = estimate_resources(matrix, config)
        if not estimate['minimum_fits_budget']:
            raise RuntimeError('RESOURCE_EXHAUSTED: ILU live-buffer estimate exceeds safe budget')
        allocation = sum(storage_estimate(matrix.shape[0], matrix.nnz, matrix.dtype.itemsize).values())
        if runtime.process.memory_info().rss+allocation > estimate['budget_bytes']*.9:
            raise RuntimeError('RESOURCE_EXHAUSTED: insufficient process memory headroom for ILU setup')
        if not np.isfinite(matrix.data).all() or not np.isfinite(sigma_scaled):
            raise RuntimeError('PRECONDITIONER_FAILED: finite ILU matrix and target shift required')
        floor = np.sqrt(np.finfo(float).eps)*max(core._norm_inf_scaled(matrix), abs(sigma_scaled),
                                                np.finfo(float).tiny)
        work_shift = float(sigma_scaled+floor)
        if not np.isfinite(work_shift) or work_shift == sigma_scaled:
            raise RuntimeError('PRECONDITIONER_FAILED: stabilized ILU shift is not representable')
        setup_started = time.monotonic()
        counters.update(preconditioner_policy=dict(ILU_POLICY), preconditioner_target_shift_scaled=float(sigma_scaled),
                        preconditioner_work_shift_scaled=work_shift, preconditioner_factorizations=0,
                        preconditioner_factor_nnz=0, preconditioner_factorization_seconds=0.,
                        preconditioner_setup_seconds=0., preconditioner_apply_seconds=0.)
        runtime.emit('SOLVING', 'preconditioner_build_started',
                     metrics={'policy': dict(ILU_POLICY), 'target_shift_scaled': float(sigma_scaled),
                              'work_shift_scaled': work_shift, 'estimated_allocation_bytes': allocation})
        runtime.check()
        try:
            shifted = matrix.tocsc(copy=True)
            shifted.setdiag(shifted.diagonal()-work_shift)
            shifted.sum_duplicates()
            shifted.sort_indices()
        except MemoryError as exc:
            raise RuntimeError('RESOURCE_EXHAUSTED: ILU sparse setup allocation failed') from exc
        runtime.check()
        factor_started = time.monotonic()
        try:
            factor = sla.spilu(shifted, drop_tol=ILU_POLICY['drop_tol'], fill_factor=ILU_POLICY['fill_factor'],
                               drop_rule=ILU_POLICY['drop_rule'], permc_spec=ILU_POLICY['permc_spec'])
        except MemoryError as exc:
            raise RuntimeError('RESOURCE_EXHAUSTED: ILU factorization allocation failed') from exc
        except (RuntimeError, ValueError) as exc:
            # A deterministic failure is reported; neither a retry policy nor
            # hidden case-specific shift/drop tuning changes the experiment.
            runtime.emit('SOLVING', 'preconditioner_failed', metrics={
                'failure_stage': 'factorization', 'attempts': 1,
                'reason': 'singular_pivot' if 'singular' in str(exc).lower() else 'factorization_failed',
                'factorization_seconds': time.monotonic()-factor_started,
                'policy': dict(ILU_POLICY)})
            raise RuntimeError('PRECONDITIONER_FAILED: ILU factorization failed (including zero pivot)') from exc
        finally:
            counters['preconditioner_factorization_seconds'] = time.monotonic()-factor_started
        runtime.check()
        del shifted
        counters['factorizations'] += 1
        counters['preconditioner_factorizations'] = 1
        counters['preconditioner_factor_nnz'] = int(factor.nnz)
        counters['preconditioner_setup_seconds'] = time.monotonic()-setup_started
        runtime.emit('SOLVING', 'preconditioner_built', metrics={
            'factor_nnz': int(factor.nnz), 'factor_nnz_measurement': 'SuperLU.nnz_without_L_U_exports',
            'factorization_seconds': counters['preconditioner_factorization_seconds'],
            'setup_seconds': counters['preconditioner_setup_seconds'], 'policy': dict(ILU_POLICY)})
        return cls(factor, runtime, counters)

    def apply(self, vector):
        self.runtime.check()
        started = time.monotonic()
        try:
            result = self.factor.solve(vector)
        except MemoryError as exc:
            raise RuntimeError('RESOURCE_EXHAUSTED: ILU triangular solve allocation failed') from exc
        except (RuntimeError, ValueError) as exc:
            raise RuntimeError('PRECONDITIONER_FAILED: ILU triangular solve failed') from exc
        finally:
            self.counters['preconditioner_apply_seconds'] += time.monotonic()-started
        self.counters['preconditioner_applications'] += 1
        self.runtime.check()
        if not np.isfinite(result).all():
            raise RuntimeError('PRECONDITIONER_FAILED: ILU triangular solve produced nonfinite values')
        return result


def _preconditioner_failure(runtime, stage, reason):
    runtime.emit('SOLVING', 'preconditioner_failed', metrics={
        'profile': ILU_V2_POLICY['id'], 'failure_stage': stage, 'reason': reason, 'attempts': 1})
    raise RuntimeError(f'PRECONDITIONER_FAILED: {stage}: {reason}')


def _build_v2(matrix, sigma_scaled, config, runtime, counters):
    from . import core
    from .backend import estimate_resources
    if (config['resources']['device'] != 'cpu' or config['solver']['linear']['precision'] != 'float64'
            or matrix.dtype not in (np.dtype('float64'), np.dtype('complex128'))):
        raise ValueError('UNSUPPORTED_PRECONDITIONER: CPU float64/complex128 ILU v2 required')
    runtime.check()
    estimate = estimate_resources(matrix, config)
    allocation = sum(storage_estimate(matrix.shape[0], matrix.nnz, matrix.dtype.itemsize,
        profile=ILU_V2_POLICY['id'], subspace_max=config['solver']['subspace_max']).values())
    if not estimate['minimum_fits_budget'] or runtime.process.memory_info().rss+allocation > estimate['budget_bytes']*.9:
        raise RuntimeError('RESOURCE_EXHAUSTED: ILU v2 setup and constraint estimate exceeds safe budget')
    if not np.isfinite(matrix.data).all() or not np.isfinite(sigma_scaled):
        _preconditioner_failure(runtime, 'setup', 'finite_matrix_and_shift_required')
    floor = np.sqrt(np.finfo(float).eps)*max(core._norm_inf_scaled(matrix), abs(sigma_scaled), np.finfo(float).tiny)
    work_shift = float(sigma_scaled+floor)
    if not np.isfinite(work_shift) or work_shift == sigma_scaled:
        _preconditioner_failure(runtime, 'setup', 'unrepresentable_stabilized_shift')
    started = time.monotonic()
    counters.update(preconditioner_policy=ilu_policy(ILU_V2_POLICY['id']),
        preconditioner_target_shift_scaled=float(sigma_scaled), preconditioner_work_shift_scaled=work_shift,
        preconditioner_factorizations=0, preconditioner_factor_nnz=0, preconditioner_factorization_seconds=0.,
        preconditioner_setup_seconds=0., preconditioner_apply_seconds=0., preconditioner_factor_solves=0,
        preconditioner_scaling_applications=0, preconditioner_equilibration_passes=0,
        preconditioner_equilibration_nonzero_scalings=0, preconditioner_constraint_builds=0,
        preconditioner_constraint_setup_factor_solves=0, preconditioner_constraint_solves=0,
        preconditioner_constraint_setup_seconds=0., preconditioner_constraint_apply_seconds=0.,
        preconditioner_constraint_schur_condition_max=0.)
    runtime.emit('SOLVING', 'preconditioner_build_started', metrics={
        'policy': ilu_policy(ILU_V2_POLICY['id']), 'target_shift_scaled': float(sigma_scaled),
        'work_shift_scaled': work_shift, 'estimated_allocation_bytes': allocation})
    history = []
    try:
        runtime.check()
        work = matrix.tocsr(copy=True)
        work.setdiag(work.diagonal()-work_shift)
        work.sum_duplicates(); work.sort_indices()
        scaling = np.ones(matrix.shape[0])
        for step in range(ILU_V2_POLICY['equilibration_sweeps']):
            runtime.check()
            row_max = abs(work).max(axis=1).toarray().ravel()
            factor = np.clip(1/np.sqrt(np.maximum(row_max, np.finfo(float).tiny)),
                             *ILU_V2_POLICY['scaling_step_clip'])
            work.data *= np.repeat(factor, np.diff(work.indptr))
            work.data *= factor[work.indices]
            scaling *= factor
            if not np.isfinite(work.data).all() or not np.isfinite(scaling).all() or np.any(scaling <= 0):
                _preconditioner_failure(runtime, 'equilibration', 'nonfinite_or_zero_scaling')
            history.append({'pass': step+1, 'row_max_min_before': float(np.min(row_max)),
                'row_max_max_before': float(np.max(row_max)), 'step_scale_min': float(np.min(factor)),
                'step_scale_max': float(np.max(factor))})
            counters['preconditioner_equilibration_passes'] += 1
            counters['preconditioner_equilibration_nonzero_scalings'] += 2*work.nnz
            runtime.check()
        diagonal = work.diagonal().real
        row_sum = np.asarray(abs(work).sum(axis=1)).ravel()
        compensated = np.where(diagonal < 0, -1., 1.)*np.maximum(abs(diagonal),
            ILU_V2_POLICY['diagonal_row_l1_floor']*row_sum)
        if not np.isfinite(compensated).all():
            _preconditioner_failure(runtime, 'compensation', 'nonfinite_diagonal')
        compensated_rows = int(np.sum(compensated != diagonal))
        compensation_max = float(np.max(abs(compensated-diagonal), initial=0.))
        work.setdiag(compensated)
        runtime.check()
        csc = work.tocsc()
        runtime.check()
    except MemoryError as exc:
        raise RuntimeError('RESOURCE_EXHAUSTED: ILU v2 sparse equilibration/setup allocation failed') from exc
    factor_started = time.monotonic()
    try:
        lu = sla.spilu(csc, drop_tol=ILU_V2_POLICY['drop_tol'], fill_factor=ILU_V2_POLICY['fill_factor'],
            drop_rule=ILU_V2_POLICY['drop_rule'], permc_spec=ILU_V2_POLICY['permc_spec'])
    except MemoryError as exc:
        raise RuntimeError('RESOURCE_EXHAUSTED: ILU v2 factorization allocation failed') from exc
    except (RuntimeError, ValueError) as exc:
        _preconditioner_failure(runtime, 'factorization', 'singular_pivot' if 'singular' in str(exc).lower() else 'factorization_failed')
    finally:
        counters['preconditioner_factorization_seconds'] = time.monotonic()-factor_started
    runtime.check()
    del work, csc
    scaling.flags.writeable = False
    counters['factorizations'] += 1
    counters.update(preconditioner_factorizations=1, preconditioner_factor_nnz=int(lu.nnz),
        preconditioner_setup_seconds=time.monotonic()-started, preconditioner_equilibration_history=history,
        preconditioner_scaling_min=float(np.min(scaling)), preconditioner_scaling_max=float(np.max(scaling)),
        preconditioner_compensated_rows=compensated_rows, preconditioner_compensation_max=compensation_max)
    runtime.emit('SOLVING', 'preconditioner_built', metrics={
        'policy': ilu_policy(ILU_V2_POLICY['id']), 'factor_nnz': int(lu.nnz),
        'factor_nnz_measurement': 'SuperLU.nnz_without_L_U_exports', 'equilibration_history': history,
        'compensated_rows': compensated_rows, 'compensation_max': compensation_max,
        'scaling_min': float(np.min(scaling)), 'scaling_max': float(np.max(scaling)),
        'factorization_seconds': counters['preconditioner_factorization_seconds'],
        'setup_seconds': counters['preconditioner_setup_seconds']})
    return EquilibratedILU(lu, runtime, counters, scaling, matrix.dtype, estimate['budget_bytes'],
                          min(matrix.shape[0], config['solver']['subspace_max']))


class EquilibratedILU(BudgetedILU):
    def __init__(self, factor, runtime, counters, scaling, dtype, budget_bytes, max_columns):
        super().__init__(factor, runtime, counters)
        self.scaling, self.dtype = scaling, np.dtype(dtype)
        self.budget_bytes, self.max_columns = budget_bytes, max_columns
        self.shape = (len(scaling), len(scaling))

    def apply(self, vector):
        self.runtime.check()
        started = time.monotonic()
        try:
            result = self.scaling*self.factor.solve(self.scaling*vector)
        except MemoryError as exc:
            raise RuntimeError('RESOURCE_EXHAUSTED: ILU v2 scaled solve allocation failed') from exc
        except (ValueError, RuntimeError):
            _preconditioner_failure(self.runtime, 'apply', 'scaled_triangular_solve_failed')
        finally:
            self.counters['preconditioner_apply_seconds'] += time.monotonic()-started
        self.counters['preconditioner_factor_solves'] += 1
        self.counters['preconditioner_scaling_applications'] += 2
        self.counters['preconditioner_applications'] += 1
        self.runtime.check()
        if not np.isfinite(result).all():
            _preconditioner_failure(self.runtime, 'apply', 'nonfinite_scaled_inverse')
        return result

    def constrained_inverse(self, selected):
        return ConstrainedInverse(self, selected)


class ConstrainedInverse:
    """Fixed block constrained inverse, prepared once per outer Ritz block."""
    def __init__(self, inverse, selected):
        self.inverse, self.runtime, self.counters = inverse, inverse.runtime, inverse.counters
        self.shape, self.dtype = inverse.shape, inverse.dtype
        self.runtime.check()
        if (not isinstance(selected, np.ndarray) or selected.ndim != 2 or selected.shape[0] != self.shape[0]
                or not 1 <= selected.shape[1] <= inverse.max_columns):
            _preconditioner_failure(self.runtime, 'constraint_setup', 'invalid_selected_block')
        n, k = selected.shape
        allocation = 2*n*k*self.dtype.itemsize+8*k*k*self.dtype.itemsize
        if self.runtime.process.memory_info().rss+allocation > inverse.budget_bytes*.9:
            raise RuntimeError('RESOURCE_EXHAUSTED: ILU v2 constraint cache exceeds memory headroom')
        if selected.dtype.kind not in 'fc' or not np.isfinite(selected).all() or (self.dtype.kind != 'c' and np.iscomplexobj(selected) and np.any(selected.imag != 0)):
            _preconditioner_failure(self.runtime, 'constraint_setup', 'invalid_selected_scalar')
        started = time.monotonic()
        try:
            q = np.array(selected if self.dtype.kind == 'c' else selected.real, dtype=self.dtype, copy=True)
            gram_error = float(np.linalg.norm(q.conjugate().T@q-np.eye(k), ord=np.inf))
            if gram_error > ILU_V2_POLICY['constraint_orthogonality_max']:
                _preconditioner_failure(self.runtime, 'constraint_setup', 'selected_block_not_orthonormal')
            z = np.empty_like(q)
            for column in range(k):
                self.runtime.check()
                z[:, column] = inverse.apply(q[:, column])
                self.counters['preconditioner_constraint_setup_factor_solves'] += 1
            schur = q.conjugate().T@z
            if not np.isfinite(schur).all():
                _preconditioner_failure(self.runtime, 'constraint_setup', 'nonfinite_schur')
            self.runtime.check()
            schur_scale = max(float(np.max(abs(schur.real), initial=0.)),
                float(np.max(abs(schur.imag), initial=0.)) if np.iscomplexobj(schur) else 0.)
            if not schur_scale:
                _preconditioner_failure(self.runtime, 'constraint_setup', 'singular_or_ill_conditioned_schur')
            normalized_schur = _divide_components(schur, schur_scale)
            condition = float(np.linalg.cond(normalized_schur))
            if not np.isfinite(condition) or condition > ILU_V2_POLICY['constraint_schur_condition_max']:
                _preconditioner_failure(self.runtime, 'constraint_setup', 'singular_or_ill_conditioned_schur')
            with warnings.catch_warnings():
                warnings.simplefilter('error', dense.LinAlgWarning)
                small_factor = dense.lu_factor(normalized_schur)
            self.runtime.check()
        except MemoryError as exc:
            raise RuntimeError('RESOURCE_EXHAUSTED: ILU v2 constraint allocation failed') from exc
        except (dense.LinAlgWarning, np.linalg.LinAlgError):
            _preconditioner_failure(self.runtime, 'constraint_setup', 'small_factorization_failed')
        q.flags.writeable = z.flags.writeable = False
        self.q, self.z, self.small_factor, self.schur_scale = q, z, small_factor, schur_scale
        self.counters['preconditioner_constraint_builds'] += 1
        self.counters['preconditioner_constraint_schur_condition_max'] = max(
            self.counters['preconditioner_constraint_schur_condition_max'], condition)
        self.counters['preconditioner_constraint_setup_seconds'] += time.monotonic()-started
        self.runtime.emit('SOLVING', 'preconditioner_constraint_built', metrics={
            'columns': k, 'schur_condition': condition, 'schur_scale': schur_scale, 'gram_error_inf': gram_error,
            'cached_factor_solves': k, 'cache_bytes': q.nbytes+z.nbytes,
            'policy': ILU_V2_POLICY['id']})

    def apply(self, vector):
        self.runtime.check()
        started = time.monotonic()
        try:
            coordinates = self.q.conjugate().T@vector
            value = self.inverse.apply(vector-self.q@coordinates)
            weights = dense.lu_solve(self.small_factor,
                _divide_components(self.q.conjugate().T@value, self.schur_scale))
            result = value-self.z@weights
            result -= self.q@(self.q.conjugate().T@result)
            result += self.q@coordinates
        except MemoryError as exc:
            raise RuntimeError('RESOURCE_EXHAUSTED: ILU v2 constrained apply allocation failed') from exc
        except (ValueError, np.linalg.LinAlgError):
            _preconditioner_failure(self.runtime, 'constraint_apply', 'small_solve_failed')
        finally:
            self.counters['preconditioner_constraint_apply_seconds'] += time.monotonic()-started
        self.counters['preconditioner_constraint_solves'] += 1
        self.runtime.check()
        if not np.isfinite(result).all():
            _preconditioner_failure(self.runtime, 'constraint_apply', 'nonfinite_constrained_inverse')
        return result
