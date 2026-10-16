"""Explicit, budgeted simultaneous inverse iteration with Rayleigh-Ritz updates.

Unlike the opaque ARPACK boundary, this adapter owns each iteration and can
recompute original-problem residuals before soft locking. It uses exact sparse
LU, not an inexact correction equation or a mixed-precision approximation.
"""
from __future__ import annotations

import math
import time
from copy import deepcopy

import numpy as np
from scipy import sparse
from scipy.sparse import linalg as sla


def _basis(rng, n, count, complex_path):
    block = rng.standard_normal((n, count))
    if complex_path:
        block = block+1j*rng.standard_normal((n, count))
    return block


def _reorthogonalize(block):
    # The second reduced QR limits accumulated loss of orthogonality without
    # constructing an n*n matrix or discarding a repeated eigenvalue's vectors.
    q, r = np.linalg.qr(block, mode='reduced')
    if np.any(np.abs(np.diag(r)) <= np.finfo(float).tiny):
        raise RuntimeError('DEPENDENT_MODES: inverse iteration block lost rank')
    q, _ = np.linalg.qr(q, mode='reduced')
    return q


def solve_block(a, config, runtime, initial_subspace=None, checkpoint_callback=None,*,resume_state=None,exact_checkpoint_callback=None):
    from . import core
    from .shift_control import create_shift_policy
    target, solver = config['target'], config['solver']
    if target['mode'] != 'nearest_k':
        raise ValueError('UNSUPPORTED_TARGET: block_shift_invert currently supports nearest_k only')
    n, requested = a.shape[0], target['k']
    block_config = solver['block']
    internal = min(requested+block_config['oversampling'], solver['subspace_max'], n-1)
    if internal <= requested:
        raise ValueError('SUBSPACE_LIMIT: block iteration requires at least k+1 bounded candidate columns')
    complex_path = np.iscomplexobj(a.data)
    scale = core._scale(a) or 1.
    scaled = core._divide_sparse(a, scale)
    sigma, base = target['sigma'], target['sigma']/scale
    if not math.isfinite(base):
        raise ValueError('NUMERICAL_SCALE_LIMIT: target/matrix scale ratio exceeds float64 range')
    delta = max(1., abs(base), core._norm_inf_scaled(scaled))*np.sqrt(np.finfo(float).eps)
    adaptive = solver['adaptive_shift']
    count = adaptive['max_attempts'] if adaptive['enabled'] else 1
    shift_policy=create_shift_policy(base,delta,count,adaptive['policy'])
    rng = np.random.default_rng(solver['random_seed'])
    counters = {'factorizations':0, 'linear_solves':0, 'linear_solve_batches':0,
                'matrix_vector_products':0, 'shifted_residual_matvecs':0, 'outer_iterations':0,
                'linear_iterations':None, 'max_linear_residual':0., 'residual_checks':0,
                'restarts':0, 'soft_locked_modes':0, 'warm_start':initial_subspace is not None,
                'residual_check_interval':block_config['residual_check_interval']}
    counters['residual_check_seconds'] = 0.
    counters.update(exact_resume=False,restored_outer_iterations=0,outer_iterations_executed_this_process=0)
    attempts, best = [], (np.empty(0), np.empty((n, 0), dtype=a.dtype))
    state_basis = None
    if resume_state is not None:
        counters=deepcopy(resume_state['counters'])
        counters.update(exact_resume=True,restored_outer_iterations=resume_state['iteration'],outer_iterations_executed_this_process=0)
        best=resume_state['best_values'].copy(),resume_state['best_vectors'].copy()
        rng.bit_generator.state=deepcopy(resume_state['rng_state'])
    last_exact_iteration=resume_state['iteration'] if resume_state is not None else 0
    for index in range(count):
        if counters['outer_iterations'] >= solver['max_outer_iterations']:
            break
        shift=shift_policy.next_shift()
        decision=dict(shift_policy.last_decision)
        work_sigma=shift*scale
        if not math.isfinite(work_sigma):
            attempts.append({'attempt':index+1,'sigma_target':sigma,'sigma_work':None,'status':'FAILED',
                             'error_code':'NUMERICAL_SCALE_LIMIT','shift_decision':decision,'seconds':0.})
            shift_policy.observe(error_code='NUMERICAL_SCALE_LIMIT')
            runtime.emit('PREPARING','shift_failed',attempt=index+1,error_code='NUMERICAL_SCALE_LIMIT',metrics={'shift_decision':decision})
            continue
        runtime.emit('PREPARING', 'shift_attempt', attempt=index+1, sigma_target=sigma, sigma_work=work_sigma,
                     metrics={'shift_decision':decision})
        started = time.monotonic()
        if resume_state is not None:started-=resume_state['attempt_seconds']
        attempt = {'attempt':index+1, 'sigma_target':sigma, 'sigma_work':work_sigma, 'status':'STARTED','shift_decision':decision}
        shifted = (scaled-shift*sparse.eye(n, format='csr')).tocsc()
        try:
            counters['factorizations'] += 1
            lu = sla.splu(shifted)
            runtime.check()
            if resume_state is not None:
                q=resume_state['basis'].copy();locked=resume_state['locked'].copy()
                best_residual,stalled=resume_state['best_residual'],resume_state['stalled_checks']
                attempt.update(resumed_from_iteration=resume_state['iteration'],refactorized=True)
            else:
                seed = _basis(rng, n, internal, complex_path)
                if initial_subspace is not None:
                    columns = min(initial_subspace.shape[1], internal)
                    seed[:, :columns] = initial_subspace[:, :columns]
                q = _reorthogonalize(seed)
                locked = np.zeros(internal, dtype=bool)
                best_residual, stalled = float('inf'), 0
            attempt['status'] = 'MAX_ITERATIONS'
            for iteration in range(solver['max_outer_iterations']-counters['outer_iterations']):
                runtime.check()
                counters['outer_iterations'] += 1
                counters['outer_iterations_executed_this_process']=counters['outer_iterations']-counters['restored_outer_iterations']
                # Soft locked columns remain in the Ritz space. All are
                # revalidated before acceptance, and can unlock after rotation.
                active = ~locked
                z = q.copy()
                if np.any(active):
                    right = q[:, active]
                    answer = lu.solve(right)
                    counters['linear_solve_batches'] += 1
                    counters['linear_solves'] += right.shape[1]
                    counters['shifted_residual_matvecs'] += right.shape[1]
                    error = shifted@answer-right
                    numerator = np.max(np.abs(error), axis=0)
                    denominator = core._norm_inf_scaled(shifted)*np.max(np.abs(answer), axis=0)+np.max(np.abs(right), axis=0)
                    linear_residual = float(np.max(numerator/denominator, initial=0))
                    counters['max_linear_residual'] = max(counters['max_linear_residual'], linear_residual)
                    if not np.isfinite(answer).all() or not math.isfinite(linear_residual) or linear_residual > solver['linear']['rtol_initial']:
                        raise RuntimeError('LINEAR_RESIDUAL_FAILED: inaccurate block sparse LU solve')
                    z[:, active] = answer
                q = _reorthogonalize(z)
                aq = scaled@q
                counters['matrix_vector_products'] += internal
                projected = q.conjugate().T@aq
                values, rotation = np.linalg.eigh(projected)
                values, q = values*scale, q@rotation
                order = core._target_order(values, sigma)
                values, q = values[order], q[:, order]
                shift_policy.observe(values/scale,linear_residual=counters['max_linear_residual'])
                state_basis = q.copy()
                final_iteration = counters['outer_iterations'] >= solver['max_outer_iterations']
                if counters['outer_iterations'] % block_config['residual_check_interval'] and not final_iteration:
                    # A Ritz rotation changes individual vectors; stale lock
                    # flags cannot authorize skipping their next correction.
                    locked[:] = False
                    continue
                validation_started = time.monotonic()
                report, q = core._validate(a, values, q, config)
                counters['residual_check_seconds'] += time.monotonic()-validation_started
                counters['residual_checks'] += 1
                counters['matrix_vector_products'] += 2*internal
                residuals = np.asarray(report['residuals'])
                valid = residuals <= config['validation']['residual_scaled_max']
                if config['validation']['absolute_residual_max'] is not None:
                    valid &= np.asarray(report['absolute_residuals_2']) <= config['validation']['absolute_residual_max']
                valid &= report['orthogonality_error'] <= config['validation']['orthogonality_max']
                selected_count = requested
                if target['cluster_policy'] == 'cluster_complete':
                    while selected_count < internal and abs(values[selected_count]-values[requested-1]) <= core._cluster_tol(values):
                        selected_count += 1
                selected_valid = valid[:selected_count]
                accepted_values, accepted_vectors = values[:selected_count][selected_valid], q[:, :selected_count][:, selected_valid]
                if len(accepted_values) >= len(best[0]):
                    best = accepted_values.copy(), accepted_vectors.copy()
                newly = int(np.sum(valid & ~locked))
                locked = valid.copy()
                counters['soft_locked_modes'] = int(np.sum(locked))
                score = float(np.max(residuals[:requested], initial=0))
                if np.all(selected_valid) or score < best_residual*0.9:
                    best_residual, stalled = score, 0
                else:
                    stalled += 1
                if checkpoint_callback:
                    checkpoint_callback(q,counters['outer_iterations'],np.flatnonzero(locked).tolist())
                if newly:
                    runtime.emit('SOLVING', 'modes_soft_locked', iteration=counters['outer_iterations'],
                                 n_accepted=len(accepted_values),locked_indices=np.flatnonzero(locked).tolist(),
                                 metrics={'soft_locked_modes':int(np.sum(locked))})
                runtime.emit('SOLVING', 'block_residual_checked', iteration=counters['outer_iterations'],
                             residual=float(np.max(residuals[:requested], initial=0)), residuals=residuals[:selected_count].tolist(),
                             n_accepted=len(accepted_values),locked_indices=np.flatnonzero(locked).tolist(),
                             stalled_checks=stalled,stagnation_threshold=block_config['stagnation_checks'],metrics=dict(counters))
                if np.all(selected_valid):
                    attempt['status'] = 'CONVERGED'
                    break
                if stalled >= block_config['stagnation_checks'] and counters['restarts'] < block_config['max_restarts']:
                    # Retain the complete requested Ritz block, refresh only
                    # oversampling columns, then orthogonalize the combined space.
                    q[:, requested:] = _basis(rng, n, internal-requested, complex_path)
                    q = _reorthogonalize(q)
                    counters['restarts'] += 1
                    locked[:] = False
                    counters['soft_locked_modes']=0
                    trigger_checks=stalled
                    stalled = 0
                    runtime.emit('SOLVING', 'block_restarted', iteration=counters['outer_iterations'],
                                 restart_reason='residual_stagnation',stalled_checks=trigger_checks,
                                 stagnation_threshold=block_config['stagnation_checks'],locked_indices=[],
                                 metrics={'restarts':counters['restarts'], 'retained_columns':requested,'reason':'residual_stagnation'})
                if (exact_checkpoint_callback and counters['outer_iterations']<solver['max_outer_iterations'] and
                    counters['outer_iterations']-last_exact_iteration>=block_config['checkpoint_interval']):
                    # Save only after all current-iteration control decisions;
                    # this is the exact input of the next inverse iteration.
                    exact_checkpoint_callback({'basis':q.copy(),'best_values':best[0].copy(),'best_vectors':best[1].copy(),
                                               'locked':locked.copy(),'iteration':counters['outer_iterations'],
                                               'counters':deepcopy(counters),'best_residual':best_residual,'stalled_checks':stalled,
                                               'rng_state':deepcopy(rng.bit_generator.state),'shift_scaled':shift,'matrix_scale':scale,
                                               'attempt_seconds':time.monotonic()-started})
                    last_exact_iteration=counters['outer_iterations']
            attempt.update(n_accepted=len(best[0]), seconds=time.monotonic()-started,
                           outer_iterations=counters['outer_iterations'])
            attempts.append(attempt)
            if attempt['status'] == 'CONVERGED':
                break
        except RuntimeError as exc:
            if str(exc).startswith(('RESOURCE_EXHAUSTED', 'WALLTIME_EXCEEDED')):
                raise
            attempt.update(status='FAILED', error_code=core._exception_code(exc), seconds=time.monotonic()-started)
            shift_policy.observe(error_code=attempt['error_code'],linear_residual=counters['max_linear_residual'])
            attempts.append(attempt)
            runtime.emit('SOLVING', 'shift_failed', attempt=index+1, error_code=attempt['error_code'])
        finally:
            if 'lu' in locals():
                del lu
    return *best, attempts, counters, state_basis
