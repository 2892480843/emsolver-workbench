"""Block Jacobi-Davidson with projected, inexact FP64 correction equations.

The inverse operator used by ARPACK is never changed. Here GMRES approximately
solves a different, explicitly projected correction equation; all acceptance is
still based on fresh residuals of the original matrix.
"""
from __future__ import annotations

import time

import numpy as np
from scipy.sparse import linalg as sla

from .block import _basis, _reorthogonalize
from .accuracy import refinement_readiness


def solve_correction(a, config, runtime, initial_subspace=None, checkpoint_callback=None):
    from . import core
    solver, target = config['solver'], config['target']
    n, k = a.shape[0], target['k']
    limit = min(solver['subspace_max'], n-1)
    start_size = min(k+solver['block']['oversampling'], max(k+1, limit-k))
    if limit <= k or start_size > limit:
        raise ValueError('SUBSPACE_LIMIT: Jacobi-Davidson requires a subspace larger than k')
    scale = core._scale(a) or 1.
    scaled = core._divide_sparse(a, scale)
    mixed = solver['linear']['precision']=='mixed_float32'
    single_dtype = np.dtype(np.complex64 if np.iscomplexobj(a.data) else np.float32)
    single_matrix = scaled.astype(single_dtype) if mixed else None
    gpu_operator = None
    if config['resources']['device'] == 'gpu':
        from .gpu import MetalSparseOperator, validate_gpu_profile
        validate_gpu_profile(config)
        gpu_operator = MetalSparseOperator(scaled, memory_limit_bytes=int(config['resources']['memory_limit_gib'] * 1024**3 * .7))
    accuracy = solver['linear']['adaptive_accuracy']
    tolerance = accuracy['rtol_max'] if accuracy['enabled'] else solver['linear']['rtol_initial']
    counters = {'factorizations':0,'linear_solves':0,'linear_iterations':0,'outer_iterations':0,
                'matrix_vector_products':0,'shifted_residual_matvecs':0,'max_linear_residual':0.,
                'residual_checks':0,'residual_check_seconds':0.,'restarts':0,'soft_locked_modes':0,
                'warm_start':initial_subspace is not None,'inner_tolerance_min_used':tolerance,
                'inner_tolerance_max_used':tolerance,'inner_failures':0,'inner_accuracy_adaptive':accuracy['enabled']}
    counters.update(float32_linear_solves=0,float64_linear_solves=0,float32_failures=0,precision_promotions=0,
                    preconditioner_applications=0,matrix_vector_products_float32=0,inner_precision_policy=solver['linear']['precision'])
    counters.update(accuracy_refinement_checks=0, accuracy_refinement_iterations=0, accuracy_refinement_pending=0,
                    accuracy_certification_probes=0, accuracy_certification_probe_seconds=0.)
    probe_accuracy = config['validation']['completeness_required'] and not core._is_banded_csr(a, 1)
    refine_accuracy = False
    ilu = None
    if solver['linear']['preconditioner'] in {'ilu_budgeted_v1', 'ilu_equilibrated_compensated_v2'}:
        from .preconditioning import BudgetedILU
        # Keep setup ahead of dense search-basis allocations and reuse one
        # fixed target-shift factor across every inner and outer iteration.
        ilu = BudgetedILU.build(scaled, target['sigma']/scale, config, runtime, counters)
    rng = np.random.default_rng(solver['random_seed'])
    seed = _basis(rng, n, start_size, np.iscomplexobj(a.data))
    if initial_subspace is not None:
        columns = min(start_size, initial_subspace.shape[1])
        seed[:, :columns] = initial_subspace[:, :columns]
    basis = _reorthogonalize(seed)
    best = np.empty(0), np.empty((n,0),dtype=a.dtype)
    attempt = {'attempt':1,'method':'jacobi_davidson','sigma_target':target['sigma'],
               'sigma_work':None,'status':'MAX_ITERATIONS'}
    started = time.monotonic()
    state_basis = basis
    initial_outer = None
    best_outer=float('inf');stalled=0
    for iteration in range(1,solver['max_outer_iterations']+1):
        runtime.check()
        counters['outer_iterations'] = iteration
        product = scaled@basis
        counters['matrix_vector_products'] += basis.shape[1]
        roots, rotations = np.linalg.eigh(basis.conjugate().T@product)
        order = core._target_order(roots*scale,target['sigma'])
        roots = roots[order]
        vectors = basis@rotations[:,order]
        values = roots*scale
        validation_started = time.monotonic()
        report, vectors = core._validate(a,values,vectors,config)
        counters['residual_check_seconds'] += time.monotonic()-validation_started
        counters['residual_checks'] += 1
        counters['matrix_vector_products'] += 2*len(values)
        residuals = np.asarray(report['residuals'])
        valid = residuals <= config['validation']['residual_scaled_max']
        if config['validation']['absolute_residual_max'] is not None:
            valid &= np.asarray(report['absolute_residuals_2']) <= config['validation']['absolute_residual_max']
        valid &= report['orthogonality_error'] <= config['validation']['orthogonality_max']
        count = k
        if target['cluster_policy'] == 'cluster_complete':
            while count < len(values) and abs(values[count]-values[k-1]) <= core._cluster_tol(values):
                count += 1
        numerical_mask = valid[:count]
        if np.sum(numerical_mask) >= len(best[0]):
            # Keep valid candidates even if refinement later reaches a restart
            # or iteration budget. An accuracy hint must not erase partial work.
            best = values[:count][numerical_mask].copy(),vectors[:,:count][:,numerical_mask].copy()
        ready = valid.copy()
        if probe_accuracy and np.all(numerical_mask):
            # Only an actual value-enclosure refusal justifies more precision.
            # Geometry or proof-budget limitations cannot be cured by chasing
            # tiny residuals. This bounded probe is never the final certificate.
            probe_started = time.monotonic()
            probe = core._certify(a, values[:count], vectors[:, :count], config, check=runtime.check)
            runtime.check()
            counters['accuracy_certification_probe_seconds'] += time.monotonic()-probe_started
            counters['accuracy_certification_probes'] += 1
            probe_accuracy = False
            refine_accuracy = probe.get('certification_limit') == 'candidate_value_enclosure_too_wide'
            runtime.emit('SOLVING', 'certification_accuracy_probed', iteration=iteration,
                         metrics={'source': probe.get('certification_source'), 'reason': probe.get('certification_limit'),
                                  'refine_accuracy': refine_accuracy, 'is_final_certificate': False,
                                  'seconds': counters['accuracy_certification_probe_seconds']})
        if refine_accuracy:
            columns = np.flatnonzero(valid[:count])
            if len(columns):
                hint = refinement_readiness(scaled, values[:count], vectors[:, :count], scale, columns, check=runtime.check)
                counters['accuracy_refinement_checks'] += len(columns)
                counters['matrix_vector_products'] += len(columns)
                ready[:count] &= hint
            pending = int(np.sum(numerical_mask & ~ready[:count]))
            counters['accuracy_refinement_pending'] = pending
            if pending:
                counters['accuracy_refinement_iterations'] += 1
                runtime.emit('SOLVING', 'certification_accuracy_refinement', iteration=iteration,
                             metrics={'pending_modes': pending, 'numerically_valid_modes': int(np.sum(numerical_mask)),
                                      'policy': 'existing_exact_value_fidelity_with_8x_margin', 'is_certificate': False})
        mask = ready[:count]
        counters['soft_locked_modes'] = int(np.sum(mask))
        outer = float(np.max(residuals[:k]))
        if np.all(mask) or outer<best_outer*.9:best_outer,stalled=outer,0
        else:stalled+=1
        state_basis = vectors.copy()
        runtime.emit('SOLVING','block_residual_checked',iteration=iteration,residual=float(np.max(residuals[:k])),
                     residuals=residuals[:count].tolist(),n_accepted=int(np.sum(mask)),locked_indices=np.flatnonzero(mask).tolist(),
                     stalled_checks=stalled,stagnation_threshold=solver['block']['stagnation_checks'],metrics=dict(counters))
        if checkpoint_callback:
            checkpoint_callback(state_basis,iteration,np.flatnonzero(ready).tolist())
        if np.all(mask):
            attempt['status'] = 'CONVERGED'
            break
        if initial_outer is None:
            initial_outer = max(outer,np.finfo(float).tiny)
        if accuracy['enabled']:
            proposed = accuracy['forcing_factor']*np.sqrt(max(outer/initial_outer,np.finfo(float).eps))
            tolerance = min(tolerance,float(np.clip(proposed,accuracy['rtol_min'],accuracy['rtol_max'])))
        counters['inner_tolerance_min_used'] = min(counters['inner_tolerance_min_used'],tolerance)
        counters['inner_tolerance_max_used'] = max(counters['inner_tolerance_max_used'],tolerance)
        runtime.emit('SOLVING','inner_tolerance_updated',iteration=iteration,
                     metrics={'rtol':tolerance,'outer_residual':outer,'equation':'projected_Jacobi_Davidson'})
        # Project against the selected Ritz block; these columns are orthonormal.
        selected = vectors[:,:count]
        constrained_inverse = (ilu.constrained_inverse(selected)
                               if solver['linear']['preconditioner'] == 'ilu_equilibrated_compensated_v2' else None)
        def project(vector):
            return vector-selected@(selected.conjugate().T@vector)
        corrections=[]
        for column in np.flatnonzero(~ready[:k]):
            theta = float(roots[column])
            x = vectors[:,column]
            right = -project(scaled@x-theta*x)
            counters['matrix_vector_products'] += 1
            def operators(use_single):
                work_matrix=single_matrix if use_single else scaled
                dtype=single_dtype if use_single else a.dtype
                work_selected=selected.astype(dtype,copy=False)
                work_theta=np.asarray(theta,dtype=np.float32 if use_single else np.float64)
                def work_project(vector):
                    vector=np.asarray(vector,dtype=dtype)
                    return vector-work_selected@(work_selected.conjugate().T@vector)
                def operation(vector):
                    runtime.check()
                    projected=work_project(vector)
                    counters['matrix_vector_products']+=1
                    if use_single:counters['matrix_vector_products_float32']+=1
                    product = gpu_operator.matvec(projected) if use_single and gpu_operator is not None else work_matrix@projected
                    return work_project(product-work_theta*projected)+work_selected@(work_selected.conjugate().T@vector)
                operator=sla.LinearOperator(a.shape,matvec=operation,dtype=dtype)
                preconditioner=None
                if solver['linear']['preconditioner']=='jacobi':
                    # An absolute-diagonal approximate inverse is legal for
                    # GMRES even though the correction operator is indefinite.
                    real_dtype=np.float32 if use_single else np.float64
                    floor=max(np.sqrt(np.finfo(real_dtype).eps)*core._norm_inf_scaled(work_matrix),np.finfo(real_dtype).tiny)
                    inverse_diagonal=(1/np.maximum(np.abs(work_matrix.diagonal()),floor)).astype(real_dtype)
                    def apply_preconditioner(vector):
                        counters['preconditioner_applications']+=1
                        return work_project(inverse_diagonal*work_project(vector))+work_selected@(work_selected.conjugate().T@vector)
                    preconditioner=sla.LinearOperator(a.shape,matvec=apply_preconditioner,dtype=dtype)
                elif constrained_inverse is not None:
                    preconditioner=sla.LinearOperator(a.shape,matvec=constrained_inverse.apply,dtype=dtype)
                elif ilu is not None:
                    def apply_preconditioner(vector):
                        return work_project(ilu.apply(work_project(vector)))+work_selected@(work_selected.conjugate().T@vector)
                    preconditioner=sla.LinearOperator(a.shape,matvec=apply_preconditioner,dtype=dtype)
                return operator,preconditioner
            operator64,preconditioner64=operators(False)
            def inner_callback(_):
                counters['linear_iterations'] += 1
                runtime.check()
            denominator = core._stable_norm2(right)
            def run_inner(operator,preconditioner,rhs,use_single,initial=None):
                counters['linear_solves']+=1
                counters['float32_linear_solves' if use_single else 'float64_linear_solves']+=1
                return sla.gmres(operator,rhs,x0=initial,M=preconditioner,rtol=tolerance,atol=0.,
                                 restart=min(limit,50),maxiter=solver['linear']['max_iterations'],
                                 callback=inner_callback,callback_type='legacy')
            use_single=mixed and tolerance>=64*np.finfo(np.float32).eps and core._stable_norm2(right.astype(single_dtype))>0
            if use_single:
                operator32,preconditioner32=operators(True)
                correction,info=run_inner(operator32,preconditioner32,right.astype(single_dtype),True)
                correction=correction.astype(a.dtype)
                achieved=core._stable_norm2(operator64@correction-right)/denominator if denominator else 0.
                promote=bool(info or not np.isfinite(correction).all() or not np.isfinite(achieved) or achieved>tolerance*(1+1e-6))
                if promote:
                    counters['float32_failures']+=1
                    counters['precision_promotions']+=1
                    runtime.emit('SOLVING','inner_precision_promoted',iteration=iteration,
                                 metrics={'from':'float32','to':'float64','reason':'FP64_correction_residual_failed'})
                    initial=correction if np.isfinite(correction).all() else None
                    correction,info=run_inner(operator64,preconditioner64,right,False,initial)
            else:
                if mixed:
                    counters['precision_promotions']+=1
                    runtime.emit('SOLVING','inner_precision_promoted',iteration=iteration,
                                 metrics={'from':'float32','to':'float64','reason':'forcing_tolerance_or_scale_requires_FP64'})
                correction,info=run_inner(operator64,preconditioner64,right,False)
            correction=correction.astype(a.dtype,copy=False)
            achieved = core._stable_norm2(operator64@correction-right)/denominator if denominator else 0.
            counters['max_linear_residual'] = max(counters['max_linear_residual'],achieved)
            if info or not np.isfinite(correction).all() or not np.isfinite(achieved) or achieved > tolerance*(1+1e-6):
                counters['inner_failures'] += 1
                attempt['status'] = 'INNER_NOT_CONVERGED'
                attempt['error_code'] = 'LINEAR_NO_CONVERGENCE'
                break
            # A correction is useful only outside the whole search subspace.
            for _ in range(2):
                correction -= basis@(basis.conjugate().T@correction)
                for previous in corrections:
                    correction -= previous*np.vdot(previous,correction)
            norm = core._stable_norm2(correction)
            if norm > 128*np.finfo(float).eps:
                corrections.append(correction/norm)
        if attempt['status'] == 'INNER_NOT_CONVERGED':
            break
        if not corrections:
            attempt.update(status='STAGNATED',error_code='DEPENDENT_CORRECTION')
            break
        extra = np.column_stack(corrections)
        at_stagnation=stalled>=solver['block']['stagnation_checks']
        if basis.shape[1]+extra.shape[1] > limit or at_stagnation:
            if counters['restarts'] >= solver['block']['max_restarts']:
                attempt.update(status='RESTART_LIMIT',error_code='SUBSPACE_LIMIT')
                break
            retain = min(k+solver['block']['oversampling'],limit-extra.shape[1])
            basis = _reorthogonalize(np.column_stack((vectors[:,:retain],extra)))
            counters['restarts'] += 1
            runtime.emit('SOLVING','block_restarted',iteration=iteration,locked_indices=[],stalled_checks=stalled,
                         stagnation_threshold=solver['block']['stagnation_checks'],
                         restart_reason='residual_stagnation' if at_stagnation else 'subspace_capacity',
                         metrics={'restarts':counters['restarts'],'reason':'residual_stagnation' if at_stagnation else 'subspace_capacity'})
            stalled=0
        else:
            basis = _reorthogonalize(np.column_stack((basis,extra)))
    attempt.update(n_accepted=len(best[0]),seconds=time.monotonic()-started,outer_iterations=counters['outer_iterations'])
    if gpu_operator is not None:
        counters.update(gpu_operator.metrics())
    return *best,[attempt],counters,state_basis
