"""Apply explainable solver advice without relaxing a frozen experiment contract."""
from __future__ import annotations

import copy

from .contracts import resolve_config
from .errors import ServiceError


# Advice may tune the algorithm, but cannot change the question or its acceptance
# criteria. New tunable fields require an explicit policy change here.
ALLOWED_PATHS = frozenset({
    'solver.backend', 'solver.method', 'solver.transform', 'solver.subspace_max',
    'solver.max_outer_iterations', 'solver.random_seed', 'solver.fallback_profiles',
    'solver.adaptive_shift.enabled', 'solver.adaptive_shift.max_attempts',
})
INTEGER_PATHS = frozenset({
    'solver.subspace_max', 'solver.max_outer_iterations', 'solver.random_seed',
    'solver.adaptive_shift.max_attempts',
})


def apply_solver_advice(base: dict, changes: dict) -> dict:
    """Return a validated copy; reject every supplied path outside the policy."""
    if not isinstance(changes, dict):
        raise ServiceError('INVALID_ADVICE')
    try:
        resolved = resolve_config(copy.deepcopy(base))
    except (ValueError, TypeError):
        raise ServiceError('INVALID_CONFIG') from None
    candidate = copy.deepcopy(resolved)

    def visit(patch, destination, prefix=''):
        for key, value in patch.items():
            path = f'{prefix}.{key}' if prefix else key
            if not isinstance(key, str):
                raise ServiceError('INVALID_ADVICE')
            if isinstance(value, dict):
                if not value or not any(allowed.startswith(path + '.') for allowed in ALLOWED_PATHS):
                    raise ServiceError('ADVICE_FIELD_PROTECTED', 'Advice may change only approved solver settings')
                if not isinstance(destination.get(key), dict):
                    raise ServiceError('INVALID_ADVICE')
                visit(value, destination[key], path)
            else:
                if path not in ALLOWED_PATHS:
                    raise ServiceError('ADVICE_FIELD_PROTECTED', 'Advice may change only approved solver settings')
                if path in INTEGER_PATHS and type(value) is not int:
                    raise ServiceError('INVALID_ADVICE')
                if path == 'solver.adaptive_shift.enabled' and type(value) is not bool:
                    raise ServiceError('INVALID_ADVICE')
                destination[key] = copy.deepcopy(value)

    visit(changes, candidate)
    try:
        return resolve_config(candidate)
    except (ValueError, TypeError):
        raise ServiceError('INVALID_ADVICE', 'Advice failed solver configuration validation') from None
