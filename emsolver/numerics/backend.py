"""Replaceable CPU adapters; final acceptance remains in the independent verifier.

The original matrix is retained deliberately: permutation and backend success
must not become evidence for a different problem.
"""
from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Protocol, runtime_checkable

import numpy as np
import scipy
from scipy import sparse
from scipy.sparse.csgraph import reverse_cuthill_mckee


@dataclass
class BackendResult:
    values: np.ndarray
    vectors: np.ndarray
    attempts: list[dict]
    counters: dict
    permutation: np.ndarray
    diagnostics: dict
    strategy: dict
    estimate: dict


@runtime_checkable
class SolverBackend(Protocol):
    def prepare(self, matrix) -> dict: ...
    def solve(self) -> None: ...
    def monitor(self) -> dict: ...
    def finalize(self) -> BackendResult: ...


def _structural_diagonal_strategy(plan):
    """Describe the selected analytic implementation, retaining user intent."""
    return {
        **plan,'actual_method':'structural_diagonal','transform':'none','compute_device':'cpu',
        'linear_solver':{'method':'not_used','preconditioner':'not_used','residual_tolerance':None,
                         'adaptive_tolerance':False,'accuracy_policy':None,'precision':None,
                         'mixed_precision':False,'promotion_rule':None},
        'shift_policy':{**plan['shift_policy'],'applies':False,'actual_controller':None},
        'fallback_applicable':False,
        'execution_profiles':[{**profile,'actual_method':'structural_diagonal' if index==0 else profile['config']['solver']['method'],
                               'applies':index==0,
                               'condition':'structural_diagonal_detected' if index==0 else 'not_applicable_to_structural_diagonal'}
                              for index,profile in enumerate(plan['execution_profiles'])],
        'monitoring':{**plan['monitoring'],'outer_iteration_count':False,'linear_solve_count':False,
                      'outer_iteration_residual_history':False,'residual_check_interval':None,
                      'locking':'not_applicable','max_restarts':None,
                      'iteration_note':'Analytic diagonal selection performs no outer or inner iterations'},
        'checkpoint':{'exact_resume':False,'warm_start':False,'periodic_interval':None,
                      'reason':'Analytic diagonal selection has no iterative subspace to resume'},
        'implementation':{'candidate_solver':'analytic_diagonal_selection',
                          'description':'Select original diagonal entries and construct their coordinate basis vectors',
                          'project_code':['safe normalization','analytic diagonal selection','exact target ordering',
                                          'original-problem verification','structural target certification']},
        'limitations':['Configured mode and cluster safety caps still apply',
                       'Fixed-k truncation of a repeated eigenvalue cluster remains unverified'],
    }


def algorithm_plan(config, matrix_statistics=None):
    """Plan a supported family, selecting the analytic path with explicit evidence.

    Callers can supply validated inspect_matrix statistics. The backend always
    recomputes structural evidence from its own prepared matrix before execution.
    """
    from . import core
    config = core._config(config)
    if (config['solver']['backend']=='slepc' and
            config['solver']['linear']['preconditioner']=='mumps_ooc_symmetric_v1' and
            (config['problem']['scalar']=='complex128' or
             isinstance(matrix_statistics,dict) and 'complex' in str(matrix_statistics.get('dtype','')))):
        raise ValueError('UNSUPPORTED_SCALAR: symmetric OOC factor profile requires real input')
    core._check_config_support(config)
    if config['solver']['backend']=='slepc':
        from .slepc import algorithm_plan_slepc
        return algorithm_plan_slepc(config)
    solver = config['solver']
    is_block = solver['method'] == 'block_shift_invert'
    is_correction = solver['method'] == 'jacobi_davidson'
    uses_ilu = is_correction and solver['linear']['preconditioner'] in ('ilu_budgeted_v1','ilu_equilibrated_compensated_v2')
    owns_iteration = is_block or is_correction
    primary = core._config(config)
    primary['solver']['fallback_profiles'] = []
    profiles = [{'id':'primary', 'condition':'initial', 'config':primary}]
    if solver['fallback_profiles']:
        fallback = core._config(primary)
        fallback['solver'].update(backend='scipy', method='arpack', transform='shift_invert', fallback_profiles=[])
        fallback['solver']['linear'].update(method='sparse_lu', preconditioner='auto_budgeted')
        profiles.append({'id':'block_to_arpack_v1', 'condition':'insufficient_validated_candidates', 'config':fallback})
    plan = {
        'schema_version':'1.0', 'backend_protocol':'prepare/solve/monitor/finalize',
        'requested_backend':solver['backend'], 'requested_method':solver['method'],
        'actual_backend':'scipy', 'actual_method':solver['method'] if owns_iteration else 'arpack', 'backend_version':scipy.__version__,
        'problem_type':'standard_hermitian', 'precision':'float64_or_complex128',
        'requested_device':config['resources']['device'],
        'compute_device':'metal_mixed_correction_cpu64' if config['resources']['device']=='gpu' else 'cpu',
        'transform':'projected_correction_equation' if is_correction else 'shift_invert', 'reordering':config['problem']['reordering'],
        'verification_input':'original_unpermuted_matrix',
        'linear_solver':{'method':'projected_GMRES' if is_correction else 'SuperLU_sparse_LU',
                         'preconditioner':solver['linear']['preconditioner'] if is_correction else 'exact_factorization',
                         'residual_tolerance':solver['linear']['rtol_initial'],
                         'adaptive_tolerance':is_correction and solver['linear']['adaptive_accuracy']['enabled'],
                         'accuracy_policy':dict(solver['linear']['adaptive_accuracy']) if is_correction else None,
                         'precision':solver['linear']['precision'] if is_correction else 'float64_or_complex128',
                         'mixed_precision':is_correction and solver['linear']['precision']=='mixed_float32',
                         'sparse_product_device':'Metal_GPU_FP32' if config['resources']['device']=='gpu' else 'CPU',
                         'promotion_rule':'FP64 residual fails, or requested tolerance is below 64*float32_epsilon' if is_correction else None},
        'shift_policy':{**dict(solver['adaptive_shift']),'applies':not is_correction,
                        'actual_controller':None if is_correction else solver['adaptive_shift']['policy']},
        'fallback_profiles':list(solver['fallback_profiles']),
        'execution_profiles':profiles, 'fallback_history':[],
        'monitoring':{'original_residual_at_attempt_boundary':True, 'original_residual_at_output':True,
                      'outer_iteration_count':owns_iteration, 'linear_solve_count':True,
                      'outer_iteration_residual_history':owns_iteration, 'event_history_limit':4096,
                      'residual_check_interval':solver['block']['residual_check_interval'] if is_block else (1 if is_correction else None),
                      'locking':'residual_revalidated_soft_lock' if owns_iteration else 'backend_internal_unobservable',
                      'max_restarts':solver['block']['max_restarts'] if owns_iteration else None,
                      'secondary_observation_enabled':config['observability']['enabled'],'event_history_byte_limit':8*1024**2},
        'checkpoint':{'exact_resume':False, 'warm_start':owns_iteration,
                      'kind':solver['block']['checkpoint_kind'],
                      'supports_exact_resume':is_block and solver['block']['checkpoint_kind']=='exact_block',
                      'periodic_interval':solver['block']['checkpoint_interval'] if owns_iteration else None,
                      'reason':('Version-bound fixed-shift complete block state; LU is recomputed' if solver['block']['checkpoint_kind']=='exact_block' else
                                'New task initialized from a validated subspace; iteration state is not continued') if owns_iteration else
                               'ARPACK does not expose serializable iteration state through this adapter'},
        'implementation':{'candidate_solver':'Project projected Jacobi-Davidson with SciPy GMRES and NumPy Ritz projection' if is_correction else ('Project block inverse iteration with SciPy SuperLU and NumPy QR/Rayleigh-Ritz' if is_block else 'SciPy ARPACK and SuperLU'),
                          'project_code':['safe normalization', 'bounded shift control', 'cluster refinement',
                                          'original-problem verification', 'conservative structural certification']},
        'limitations':['Direct factorization fill-in has no guaranteed bound from sparsity statistics',
                       'General sparse nearest-target completeness requires additional independent evidence'],
    }
    if uses_ilu:
        from .preconditioning import ilu_policy
        plan['linear_solver']['preconditioner_policy'] = ilu_policy(solver['linear']['preconditioner'])
    if matrix_statistics is not None and not isinstance(matrix_statistics,dict):
        raise ValueError('INVALID_MATRIX_STATISTICS: expected a statistics object')
    return _structural_diagonal_strategy(plan) if matrix_statistics is not None and matrix_statistics.get('is_diagonal') is True else plan


def estimate_resources(matrix_or_statistics, config):
    """Conservative live-buffer model, with an explicitly unbounded fill risk.

    The range is a planning scenario, not an upper bound or measured peak.
    Process-tree enforcement is still required during factorization.
    """
    from . import core
    config = core._config(config)
    if sparse.issparse(matrix_or_statistics):
        a = matrix_or_statistics.tocsr(copy=False)
        n, nnz = a.shape[0], a.nnz
        itemsize = a.dtype.itemsize
        csr = a.data.nbytes+a.indices.nbytes+a.indptr.nbytes
        diagonal = core._is_diagonal(a)
    else:
        stats = matrix_or_statistics
        n, nnz = int(stats['n']), int(stats['nnz'])
        if n <= 0 or nnz < 0:
            raise ValueError('INVALID_SHAPE: positive n and nonnegative nnz required')
        itemsize = 16 if 'complex' in str(stats.get('dtype', '')) or config['problem']['scalar'] == 'complex128' else 8
        csr = int(stats.get('storage_bytes') or (nnz*(itemsize+8)+(n+1)*8))
        diagonal = bool(stats.get('is_diagonal', False))
    if config['problem']['scalar'] == 'complex128':
        itemsize = 16
    ooc = config['solver']['backend']=='slepc' and config['solver']['linear']['preconditioner']=='mumps_ooc_symmetric_v1'
    if ooc and itemsize==16:
        raise ValueError('UNSUPPORTED_SCALAR: symmetric OOC factor profile requires real input')
    if config['solver']['backend']=='slepc':
        # This profile always runs ST/LU, including diagonal inputs.
        diagonal=False
    target = config['target']
    k = target['k'] if target['mode'] == 'nearest_k' else min(target['max_modes'], n)
    # Diagonal cluster_complete can exceed k, so reserve the configured safety cap.
    if diagonal and target['mode'] == 'nearest_k' and target['cluster_policy'] == 'cluster_complete':
        k = min(n, core._MAX_MODES)
    m = min(n, config['solver']['subspace_max'])
    reordered = config['problem']['reordering'] != 'none'
    is_correction = config['solver']['method'] == 'jacobi_davidson'
    uses_ilu = is_correction and not diagonal and config['solver']['linear']['preconditioner'] in ('ilu_budgeted_v1','ilu_equilibrated_compensated_v2')
    components = {
        'input_and_recovery_bytes':2*csr,
        'working_csr_bytes':csr if reordered else 0,
        'csr_csc_conversion_bytes':0 if diagonal else 2*csr,
        'scaled_matrix_bytes':0 if diagonal else csr,
        'permutation_bytes':n*8*(2 if reordered else 1),
        'subspace_bytes':0 if diagonal else 5*n*m*itemsize,
        'candidate_vectors_bytes':2*n*k*itemsize,
        'validation_bytes':2*n*k*itemsize+3*k*k*itemsize,
        'inner_krylov_bytes':2*n*min(m,50)*itemsize if is_correction and not diagonal else 0,
        'projected_matrix_bytes':4*m*m*itemsize if not diagonal else 0,
        'nullspace_diagnostic_bytes':csr if config['problem']['nullspace']['basis_indices'] else 0,
        'mixed_precision_matrix_bytes':csr if is_correction and not diagonal and config['solver']['linear']['precision']=='mixed_float32' else 0,
        'preconditioner_storage_bytes':4*n*itemsize if is_correction and not diagonal and config['solver']['linear']['preconditioner']=='jacobi' else 0,
        'bounded_event_log_bytes':32*1024**2,
        'gpu_coo_scratch_bytes':nnz*40+n*64 if config['resources']['device']=='gpu' and not diagonal else 0,
        'mpi_replicated_input_bytes':csr*max(0,config['resources']['mpi_ranks']-1),
    }
    if uses_ilu:
        from .preconditioning import ilu_policy, storage_estimate
        components.update(storage_estimate(n, nnz, itemsize,
            profile=config['solver']['linear']['preconditioner'], subspace_max=m))
    minimum = int(sum(components.values()))
    factor_low = 0 if diagonal or is_correction else 2*csr
    factor_high = 0 if diagonal or is_correction else 20*csr
    from emsolver.resource_budgets import memory_budgets
    partitions=memory_budgets(config)
    budget = partitions['host_bytes']
    factorization = {'bounded':False, 'scenario_storage_multipliers':[0,0] if is_correction else [2, 20],
                     'reason':'No factorization in projected GMRES' if is_correction else
                              'Fill-in depends on graph, pivoting and ordering; scenarios are not bounds'}
    if uses_ilu:
        factorization.update(policy=ilu_policy(config['solver']['linear']['preconditioner']),
                             scenario_storage_multipliers=[2, 2],
                             reason='Each L/U reserves fill_factor*shifted_nnz plus indices, symbolic and panel storage; '
                                    'SuperLU fill_factor is not a process RSS bound; runtime guard remains authoritative')
    extra = {}
    if ooc:
        from emsolver.optional_backends import driver_config
        workspace = driver_config(config)['resources']['mumps_workspace_mb']*1024**2
        # Factors live in the private container output, not the host LU pool.
        # Keep the conservative host live-buffer estimate unchanged.
        factor_low = factor_high = 0
        extra['out_of_core']={'workspace_limit_bytes':workspace,'workspace_is_total_rss_bound':False,
                              'disk_fill_bounded':False,'storage_directory':'task_private_output',
                              'disk_scenarios_bytes':[2*csr,20*csr],
                              'container_memory_limit_bytes':partitions['container_bytes']}
        factorization['reason']='OOC factor files use task disk; MUMPS workspace is only part of container RSS; kernel and parent guards remain required'
    return {'schema_version':'1.0', 'n':int(n), 'nnz':int(nnz), 'components':components,
            'minimum_memory_bytes':minimum, 'estimated_memory_range_bytes':[minimum+factor_low, minimum+factor_high],
            'factorization_fill_in':factorization,
            'budget_bytes':budget, 'job_memory_partitions':partitions, 'minimum_fits_budget':minimum <= budget*0.7,
            'risk':'minimum_exceeds_safe_budget' if minimum > budget*0.7 else 'factorization_fill_in_unknown',
            'is_measured_peak':False, 'requires_runtime_process_tree_guard':True, **extra}


def matrix_diagnostics(a, config, values=None):
    from . import core
    declaration = config['problem']['nullspace']
    indices = declaration['basis_indices']
    if any(index >= a.shape[0] for index in indices):
        raise ValueError('INVALID_NULLSPACE: coordinate index is outside the original matrix')
    # Only exact structural zero columns establish an exact coordinate nullspace.
    # Near-zero residuals are retained as numerical evidence, not physical proof.
    csc = a.tocsc() if indices else None
    scale = core._scale(a)
    residuals, exact = [], []
    for index in indices:
        data = csc.data[csc.indptr[index]:csc.indptr[index+1]]
        residuals.append(float(np.max(np.abs(data), initial=0))/scale if scale else 0.)
        exact.append(not np.any(data != 0))
    verified = sum(exact)
    complete = bool(indices and verified == len(indices))
    physical = ('metadata_expected_nullspace' if complete and declaration['interpretation'] == 'expected_nullspace'
                else 'unverified')
    eigenvalues = [] if values is None else np.asarray(values).real
    near_zero_scale = core._cluster_tol(np.asarray(eigenvalues))
    return {'matrix_layer':{'original_n':int(a.shape[0]), 'original_nnz':int(a.nnz),
                             'normalization':getattr(a, '_emsolver_normalization', None),
                             'hermitian_relative_error':core._hermitian_error(a),
                             'returned_near_zero_count':int(np.sum(np.abs(eigenvalues) <= near_zero_scale)),
                             'near_zero_threshold':near_zero_scale},
            'nullspace':{'basis_kind':'original_coordinate_vectors', 'declared_dimension':len(indices),
                         'verified_dimension':verified, 'scaled_column_residuals':residuals,
                         'metadata_status':'VERIFIED_EXACT_COORDINATE_NULLSPACE' if complete else ('NOT_VERIFIED' if indices else 'NOT_PROVIDED'),
                         'physical_classification':physical, 'modes_removed':0,
                         'interpretation':declaration['interpretation'], 'source':declaration['source'],
                         'limitation':'Only declared coordinate basis vectors are checked; no spurious-mode classification without model evidence'}}


class ScipyBackend:
    def __init__(self, config, runtime=None, initial_subspace_path=None, input_sha256=None, checkpoint_path=None,resume_checkpoint_path=None):
        from . import core
        self.config = core._config(config)
        self.strategy = algorithm_plan(self.config)
        self.runtime = runtime or core._Runtime(self.config, None, None)
        self.phase = 'NEW'
        self.result = None
        self.initial_subspace_path = initial_subspace_path
        self.resume_checkpoint_path=resume_checkpoint_path
        self.resume_state=None
        self.input_sha256 = input_sha256
        self.initial_subspace = None
        self.state_basis = None
        self.checkpoint_path = checkpoint_path
        self.last_checkpoint_iteration = 0
        self.checkpoints_written = 0

    def _require(self, phase):
        if self.phase != phase:
            raise ValueError(f'BACKEND_LIFECYCLE: expected {phase}, found {self.phase}')

    def prepare(self, matrix):
        from . import core
        self._require('NEW')
        self.runtime.check()
        normalization = getattr(matrix, '_emsolver_normalization', None)
        a = sparse.csr_matrix(matrix, copy=True)
        a.sum_duplicates()
        a.sort_indices()
        a.eliminate_zeros()
        if len(a.shape) != 2 or not a.shape[0] or a.shape[0] != a.shape[1]:
            raise ValueError('INVALID_SHAPE: a nonempty square matrix is required')
        core._numeric(a.data, 'backend matrix')
        a = a.astype(np.complex128 if np.iscomplexobj(a.data) else np.float64)
        if self.config['problem']['scalar'] == 'float64' and np.iscomplexobj(a.data):
            raise ValueError('SCALAR_MISMATCH: complex input cannot be silently converted to float64')
        if self.config['problem']['scalar'] == 'complex128':
            a = a.astype(np.complex128)
        if normalization is not None:
            a._emsolver_normalization = dict(normalization)
        target = self.config['target']
        requested = target['k'] if target['mode'] == 'nearest_k' else target['max_modes']
        if requested > core._MAX_MODES or (target['mode'] == 'nearest_k' and requested >= a.shape[0]):
            raise ValueError('BACKEND_K_LIMIT: 0 < k < n and the configured mode safety cap are required')
        if self.config['solver']['fallback_profiles']:
            complex_extra = 1 if np.iscomplexobj(a.data) else 0
            if requested >= a.shape[0]-complex_extra or min(a.shape[0], self.config['solver']['subspace_max']) <= requested+complex_extra:
                raise ValueError('SUBSPACE_LIMIT: the declared ARPACK fallback is incompatible with dimensions/subspace')
        hermitian_error = core._hermitian_error(a)
        if not np.isfinite(hermitian_error) or hermitian_error > self.config['problem']['hermitian_rtol']:
            raise ValueError('NOT_HERMITIAN: backend matrix validation failed')
        self.original = a
        self.strategy=algorithm_plan(self.config,matrix_statistics={'is_diagonal':core._is_diagonal(a)})
        self.diagnostics = matrix_diagnostics(a, self.config)
        self.estimate = estimate_resources(a, self.config)
        if not self.estimate['minimum_fits_budget']:
            raise RuntimeError('RESOURCE_EXHAUSTED: minimum live-buffer estimate exceeds safe budget')
        self.permutation = np.arange(a.shape[0], dtype=np.int64)
        if self.config['problem']['reordering'] == 'reverse_cuthill_mckee':
            graph = a.copy()
            graph.data = np.ones(len(graph.data), dtype=np.int8)
            graph = graph.maximum(graph.T).tocsr()
            self.permutation = np.asarray(reverse_cuthill_mckee(graph, symmetric_mode=True), dtype=np.int64)
            self.working = a[self.permutation][:, self.permutation].tocsr()
        else:
            self.working = a
        if self.initial_subspace_path is not None:
            if self.config['solver']['method'] not in ('block_shift_invert','jacobi_davidson'):
                raise ValueError('UNSUPPORTED_CHECKPOINT: subspace warm start requires a project-owned iteration adapter')
            from .checkpoint import load_checkpoint
            basis, _ = load_checkpoint(self.initial_subspace_path, a, self.config, input_sha256=self.input_sha256)
            self.initial_subspace = basis[self.permutation]
        if self.resume_checkpoint_path is not None:
            from .exact_checkpoint import load_exact_checkpoint
            state,_=load_exact_checkpoint(self.resume_checkpoint_path,a,self.config,input_sha256=self.input_sha256,
                                          permutation=self.permutation)
            self.resume_state={**state,'basis':state['basis'][self.permutation],
                               'best_vectors':state['best_vectors'][self.permutation]}
            self.last_checkpoint_iteration=state['iteration'];self.checkpoints_written=state['checkpoint_sequence']
            self.strategy['checkpoint'].update(exact_resume=True,warm_start=False,factorization_reused=False)
            self.runtime.start-=state['elapsed_seconds']
            self.runtime.check()
            self.runtime.emit('PREPARING','exact_checkpoint_restored',iteration=state['iteration'],
                              metrics={'kind':'exact_block','exact_resume':True,'factorization_reused':False})
        self.phase = 'PREPARED'
        self.runtime.emit('PREPARING', 'backend_prepared', metrics={'estimate':self.estimate, 'strategy':self.strategy})
        return {'reordering':self.config['problem']['reordering'], 'n':a.shape[0], 'nnz':a.nnz,
                'estimate':self.estimate, 'strategy':self.strategy}

    def solve(self):
        from . import core
        self._require('PREPARED')
        self.phase = 'SOLVING'
        a, target = self.working, self.config['target']
        try:
            if core._is_diagonal(a):
                indices, _ = core._diagonal_selection(a.diagonal().real, target)
                if len(indices) > core._MAX_MODES:
                    raise RuntimeError('RESOURCE_EXHAUSTED: complete boundary cluster exceeds mode safety cap')
                values = a.diagonal().real[indices]
                vectors = np.zeros((a.shape[0], len(indices)), dtype=a.dtype)
                vectors[indices, np.arange(len(indices))] = 1
                attempts = [{'attempt':1, 'status':'ANALYTIC_DIAGONAL', 'sigma_target':target.get('sigma'),
                             'sigma_work':None, 'n_accepted':len(values)}]
                counters = {'factorizations':0, 'linear_solves':0, 'matrix_vector_products':0,
                            'outer_iterations':0, 'linear_iterations':0,'warm_start':False}
                self.strategy = _structural_diagonal_strategy(self.strategy)
                self.runtime.emit('SOLVING', 'structural_diagonal_solved', n_accepted=len(values))
            elif self.config['solver']['method'] == 'block_shift_invert':
                from .block import solve_block
                values, vectors, attempts, counters, self.state_basis = solve_block(a, self.config, self.runtime, self.initial_subspace,
                                                                                  self.periodic_checkpoint,resume_state=self.resume_state,
                                                                                  exact_checkpoint_callback=self.exact_checkpoint if self.config['solver']['block']['checkpoint_kind']=='exact_block' else None)
            elif self.config['solver']['method'] == 'jacobi_davidson':
                from .correction import solve_correction
                values, vectors, attempts, counters, self.state_basis = solve_correction(a,self.config,self.runtime,self.initial_subspace,
                                                                                       self.periodic_checkpoint)
            else:
                values, vectors, attempts, counters = core._solve_arpack(a, self.config, self.runtime)
            for attempt in attempts:
                attempt['method'] = self.strategy['actual_method']
            counters['fallbacks_executed'] = 0
            if self.config['solver']['fallback_profiles'] and len(values) < target['k']:
                fallback_config = self.strategy['execution_profiles'][1]['config']
                reason = 'insufficient_validated_candidates'
                self.runtime.emit('PREPARING', 'fallback_started', metrics={'profile':'block_to_arpack_v1', 'reason':reason,
                                                                          'preserves_target':True})
                started = time.monotonic()
                fallback_values, fallback_vectors, fallback_attempts, fallback_counters = core._solve_arpack(a, fallback_config, self.runtime)
                fallback_seconds = time.monotonic()-started
                for fallback_attempt in fallback_attempts:
                    fallback_attempt['method'] = 'arpack'
                    fallback_attempt['attempt'] += len(attempts)
                attempts.extend(fallback_attempts)
                counters['block_outer_iterations'] = counters['outer_iterations']
                counters['block_matrix_vector_products'] = counters['matrix_vector_products']
                for field in ('factorizations', 'linear_solves', 'shifted_residual_matvecs'):
                    counters[field] = counters.get(field, 0)+fallback_counters.get(field, 0)
                counters['max_linear_residual'] = max(counters.get('max_linear_residual', 0), fallback_counters.get('max_linear_residual', 0))
                counters.update(outer_iterations=None, matrix_vector_products=None, fallbacks_executed=1, fallback_seconds=fallback_seconds)
                history = {'profile':'block_to_arpack_v1', 'reason':reason, 'seconds':fallback_seconds,
                           'n_accepted':len(fallback_values), 'config':fallback_config}
                self.strategy['fallback_history'].append(history)
                if len(fallback_values) >= len(values):
                    values, vectors = fallback_values, fallback_vectors
                    self.strategy['actual_method'] = 'arpack'
                self.strategy['monitoring']['outer_iteration_count'] = False
                self.strategy['monitoring']['outer_iteration_residual_history'] = False
                self.strategy['monitoring']['observed_block_prefix'] = True
                self.runtime.emit('SOLVING', 'fallback_completed', n_accepted=len(fallback_values),
                                  metrics={'profile':'block_to_arpack_v1', 'seconds':fallback_seconds})
            counters.setdefault('warm_start', self.initial_subspace is not None)
            counters['periodic_checkpoints_written'] = self.checkpoints_written
            self._candidates = values, vectors, attempts, counters
            self.phase = 'CANDIDATES'
        except BaseException:
            self.phase = 'FAILED'
            raise

    def periodic_checkpoint(self,basis,iteration,locked_columns):
        from pathlib import Path
        from .checkpoint import save_checkpoint
        interval = self.config['solver']['block']['checkpoint_interval']
        if self.config['solver']['block']['checkpoint_kind']!='subspace':return
        if not interval or self.checkpoint_path is None or iteration-self.last_checkpoint_iteration < interval:
            return
        self.runtime.check()
        if basis.nbytes*3+65536 > self.config['resources']['disk_limit_gib']*1024**3:
            raise RuntimeError('RESOURCE_EXHAUSTED: periodic checkpoint exceeds disk budget')
        original = np.empty_like(basis)
        original[self.permutation] = basis
        path = Path(self.checkpoint_path)
        path.parent.mkdir(parents=True,exist_ok=True)
        progress = {'phase':'SOLVING','iteration':int(iteration),'locked_columns':locked_columns,
                    'method':self.config['solver']['method'],'exact_resume':False}
        save_checkpoint(path,self.original,self.config,original,input_sha256=self.input_sha256,progress=progress)
        self.last_checkpoint_iteration = iteration
        self.checkpoints_written += 1
        self.runtime.emit('SOLVING','checkpoint_written',iteration=iteration,
                          metrics={'kind':'subspace_warm_start','exact_resume':False,'locked_count':len(locked_columns)})

    def exact_checkpoint(self,state):
        from pathlib import Path
        from .exact_checkpoint import save_exact_checkpoint
        block=self.config['solver']['block'];iteration=state['iteration']
        if (block['checkpoint_kind']!='exact_block' or self.checkpoint_path is None or
            iteration-self.last_checkpoint_iteration<block['checkpoint_interval']):return
        self.runtime.check()
        original_basis=np.empty_like(state['basis']);original_basis[self.permutation]=state['basis']
        original_best=np.empty_like(state['best_vectors']);original_best[self.permutation]=state['best_vectors']
        state={**state,'basis':original_basis,'best_vectors':original_best,
               'elapsed_seconds':time.monotonic()-self.runtime.start,'checkpoint_sequence':self.checkpoints_written+1}
        path=Path(self.checkpoint_path);path.parent.mkdir(parents=True,exist_ok=True)
        save_exact_checkpoint(path,self.original,self.config,state,input_sha256=self.input_sha256,permutation=self.permutation)
        self.last_checkpoint_iteration=iteration;self.checkpoints_written+=1
        self.runtime.emit('SOLVING','checkpoint_written',iteration=iteration,
                          metrics={'kind':'exact_block','exact_resume':True,'locked_count':int(np.sum(state['locked']))})

    def monitor(self):
        latest = self.runtime.events[-1] if self.runtime.events else None
        return {'phase':self.phase, 'strategy':self.strategy, 'latest_event':latest,
                'peak_memory_bytes':self.runtime.peak, 'retained_event_count':len(self.runtime.events)}

    def finalize(self):
        from . import core
        self._require('CANDIDATES')
        values, working_vectors, attempts, counters = self._candidates
        if self.config['resources']['device']=='gpu' and not counters.get('gpu_matvec_calls',0):
            self.strategy.update(compute_device='cpu',gpu_not_used_reason='No low-precision correction sparse product was executed')
        vectors = np.empty_like(working_vectors)
        vectors[self.permutation, :] = working_vectors
        self.diagnostics = matrix_diagnostics(self.original, self.config, values)
        self.result = BackendResult(values, vectors, attempts, counters, self.permutation,
                                    self.diagnostics, self.strategy, self.estimate)
        self.phase = 'FINALIZED'
        self.runtime.emit('VALIDATING', 'backend_finalized', n_accepted=len(values))
        return self.result


def create_backend(config, *, runtime=None, initial_subspace_path=None, input_sha256=None,checkpoint_path=None,resume_checkpoint_path=None) -> SolverBackend:
    if config['solver']['backend']=='slepc':
        from .slepc import SlepcBackend
        return SlepcBackend(config, runtime, initial_subspace_path, input_sha256,checkpoint_path,resume_checkpoint_path)
    if initial_subspace_path is not None and resume_checkpoint_path is not None:
        raise ValueError('INCOMPATIBLE_CHECKPOINT_REQUEST: warm start and exact resume are exclusive')
    return ScipyBackend(config, runtime, initial_subspace_path, input_sha256,checkpoint_path,resume_checkpoint_path)
