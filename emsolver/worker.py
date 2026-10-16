"""One job per process. Inputs contain no reference-result paths."""
from __future__ import annotations
import json
import os
from pathlib import Path
import sys
import traceback


def main():
    spec = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    output = Path(spec['output_dir'])
    output.mkdir(parents=True, exist_ok=True)
    control = Path(sys.argv[1]).parent
    events = control / 'worker.events.jsonl'
    def emit(event):
        # Only numerical observability fields cross the process boundary.
        allowed = {'stage','event','elapsed_seconds','iteration','residual','residuals','residual_max',
                   'sigma_work','sigma_target','n_accepted','error_code','attempt','message','metrics',
                   'numerical_status','target_certification','status','internal_k','subspace','n','nnz','requested_modes'}
        allowed.update({'locked_indices','stalled_checks','stagnation_threshold','restart_reason'})
        allowed.update({'n_converged','backend_error_estimate_max','residual_scope'})
        clean = {k:v for k,v in event.items() if k in allowed and k != 'message'}
        with events.open('a',encoding='utf-8') as handle:
            handle.write(json.dumps(clean,allow_nan=False,default=lambda _:None)+'\n')
    try:
        if spec['config']['solver']['backend']=='slepc':
            from emsolver.optional_backends import seed_slepc_profile
            seed_slepc_profile(spec['backend_profile'])
        from threadpoolctl import threadpool_limits
        from emsolver.numerics import run_solver
        with threadpool_limits(limits=spec['config']['resources']['threads']):
            result = run_solver(spec['input_path'],spec['config'],output,event_callback=emit,
                                initial_subspace_path=spec.get('initial_subspace_path'),
                                resume_checkpoint_path=spec.get('resume_checkpoint_path'))
        temporary = control / 'worker.result.tmp'
        temporary.write_text(json.dumps(result,allow_nan=False),encoding='utf-8')
        temporary.replace(control / 'worker.result.json')
    except BaseException as exc:
        # Paths, matrix values and tracebacks never enter user-visible errors.
        code = 'WORKER_ERROR'
        import re
        found = re.match(r'^([A-Z][A-Z_]{3,})\b',str(exc))
        if found:
            code = found.group(1)
        error = {'status':'FAILED','error_code':code,'error_type':type(exc).__name__}
        (control / 'worker.result.json').write_text(json.dumps(error),encoding='utf-8')
        return 1
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
