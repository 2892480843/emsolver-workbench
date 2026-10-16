"""SLEPc candidate adapter; original-problem acceptance stays in core."""
from __future__ import annotations
from pathlib import Path
import tempfile

from .backend import ScipyBackend


def algorithm_plan_slepc(config):
    from emsolver.optional_backends import driver_config, probe_slepc, OOC_PROFILE
    subset = driver_config(config)
    capability = probe_slepc()
    if not capability['supported']:
        raise ValueError('BACKEND_UNAVAILABLE: tested SLEPc image and local Docker socket required')
    ooc = config['solver']['linear']['preconditioner'] == OOC_PROFILE
    if ooc and OOC_PROFILE not in capability.get('factor_profiles', []):
        raise ValueError('BACKEND_UNAVAILABLE: OOC profile requires its own passed runtime self-test')
    result = {'schema_version': '1.0', 'backend_protocol': 'prepare/solve/monitor/finalize',
            'requested_backend': 'slepc', 'requested_method': 'krylovschur',
            'actual_backend': 'slepc', 'actual_method': 'krylovschur',
            'backend_version': capability['versions']['slepc_version'],
            'problem_type': 'standard_hermitian', 'precision': 'float64_or_complex128',
            'requested_device': 'cpu', 'compute_device': 'cpu', 'transform': 'shift_invert',
            'reordering': config['problem']['reordering'], 'verification_input': 'original_unpermuted_matrix',
            'linear_solver': {'method': 'preonly', 'preconditioner': 'lu', 'factor_solver': 'mumps',
                              'precision': 'float64_or_complex128', 'mixed_precision': False},
            'mpi_ranks': config['resources']['mpi_ranks'], 'threads_per_rank': 1,
            'image_id': capability['image_id'], 'driver_sha256': capability['driver_sha256'],
            'reference_isolation': {'network': 'none', 'reference_mounted': False,
                                    'scope': 'SLEPc candidate container; host independently verifies the original matrix'},
            'monitoring': {'outer_iteration_count': True, 'outer_iteration_residual_history': False,
                           'backend_estimates_available': True,
                           'residual_scope': 'SLEPc internal estimates; never substituted for original-problem validation'},
            'checkpoint': {'exact_resume': False, 'warm_start': False,
                           'reason': 'This SLEPc profile does not expose compatible continuation state'},
            'fallback_profiles': [], 'execution_profiles': [], 'fallback_history': [],
            'adaptive_shift': {'requested': config['solver']['adaptive_shift']['enabled'], 'applies': False,
                               'reason': 'This SLEPc profile uses the frozen fixed target shift'},
            'limitations': ['Input CSR is replicated within MPI ranks; no distributed input-loader scalability claim',
                           'Sparse factorization fill-in is bounded only by the container memory guard',
                           'Target completeness requires independent count or reference evidence']}
    if ooc:
        result['precision'] = 'float64'
        result['linear_solver'].update(preconditioner='cholesky', precision='float64',
            factor_profile=OOC_PROFILE, factorization='symmetric_indefinite_ldlt',
            assumes_positive_definite=False, out_of_core=True,
            mumps_workspace_mb=subset['resources']['mumps_workspace_mb'])
        result['limitations'].append('Out-of-core factors share the private workspace disk budget; floating MUMPS inertia is not a certificate')
    return result


class SlepcBackend(ScipyBackend):
    def solve(self):
        from emsolver.optional_backends import solve_slepc
        from . import core
        import numpy as np
        self._require('PREPARED')
        self.phase = 'SOLVING'
        def observe(event):
            self.runtime.emit('SOLVING', 'slepc_iteration', iteration=event['iteration'],
                              n_accepted=event.get('n_converged', 0),
                              metrics={'backend_error_estimate_max': event.get('backend_error_estimate_max'),
                                       'residual_scope': event.get('residual_scope')})
        destination = getattr(self.runtime, 'output_dir', Path(tempfile.gettempdir()) / 'emsolver-slepc-candidates')
        result = solve_slepc(self.working, self.config, destination,
                             event_callback=observe, check=self.runtime.check)
        counters = result['metrics']
        values,vectors=core._orthogonalize_clusters(self.working,result['values'],result['vectors'])
        report,vectors=core._validate(self.working,values,vectors,self.config)
        valid=np.asarray(report['residuals'])<=self.config['validation']['residual_scaled_max']
        absolute=self.config['validation'].get('absolute_residual_max')
        if absolute is not None:valid &= np.asarray(report['absolute_residuals_2'])<=absolute
        values,vectors=values[valid],vectors[:,valid]
        order=core._target_order(values,self.config['target']['sigma'])
        limit=self.config['target']['k']
        if self.config['target']['cluster_policy']=='cluster_complete' and len(order)>limit:
            while limit<len(order) and abs(values[order[limit]]-values[order[self.config['target']['k']-1]])<=core._cluster_tol(values):
                limit+=1
        order=order[:limit]
        counters['backend_raw_candidates']=len(result['values'])
        counters['backend_candidates_after_original_validation']=len(values)
        for key in ('outer_iterations', 'linear_iterations', 'matrix_vector_products', 'linear_solves', 'factorizations'):
            counters.setdefault(key, None)
        self.strategy.update(result['strategy'])
        self.strategy['reference_isolation'] = {'network': 'none', 'reference_mounted': False,
                                               'scope': 'SLEPc candidate container'}
        self._candidates = values[order], vectors[:,order], result['attempts'], counters
        self.state_basis = None
        self.phase = 'CANDIDATES'
