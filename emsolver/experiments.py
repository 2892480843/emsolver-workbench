"""Freeze and submit bounded experiment grids through the application gateway."""
from __future__ import annotations

import copy
from contextlib import AbstractContextManager
import hashlib
from itertools import product
import math
from typing import Annotated, Any, Callable, Protocol
import uuid

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .contracts import SolveConfig, content_hash
from .errors import ServiceError
from .jobs import JobRepository, TERMINAL
from .store import Store, encode, utcnow


Identifier = Annotated[str, Field(min_length=1, max_length=200)]
KValue = Annotated[int, Field(ge=1, le=10000)]
ThreadValue = Annotated[int, Field(ge=1, le=256)]
MemoryValue = Annotated[float, Field(gt=0, le=1024)]


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False, validate_default=True)


class ExperimentGateway(Protocol):
    """The application operations needed by experiments, without importing Service."""
    store: Store
    jobs: JobRepository
    _mutex: AbstractContextManager

    def _project(self, project_id: str, writable: bool = False) -> dict: ...
    def _get(self, kind: str, item_id: str) -> dict: ...
    def create_plan(self, *, config: dict, project_id: str) -> dict: ...
    def submit_job(self, plan_id: str, idempotency_key: str | None = None,
                   *, experiment: dict | None = None) -> dict: ...
    def get_benchmark(self, batch_id: str) -> dict: ...


class ExperimentBudget(StrictRequest):
    max_runs: int = Field(default=100, ge=1, le=100)
    max_total_walltime_seconds: float = Field(default=7200, gt=0, le=8640000)
    max_total_disk_gib: float = Field(default=200, gt=0, le=409600)


class ExperimentSpec(StrictRequest):
    project_id: Identifier
    matrix_ids: list[Identifier] = Field(min_length=1, max_length=30)
    base_config: dict[str, Any] = Field(default_factory=dict)
    k_values: list[KValue] = Field(default_factory=lambda: [6], min_length=1, max_length=10)
    sigma_values: list[float] = Field(default_factory=lambda: [0.0], min_length=1, max_length=10)
    threads_values: list[ThreadValue] = Field(default_factory=lambda: [1], min_length=1, max_length=10)
    memory_limit_gib_values: list[MemoryValue] = Field(default_factory=lambda: [16.0], min_length=1, max_length=10)
    adaptive_shift_values: list[bool] = Field(default_factory=lambda: [True], min_length=1, max_length=2)
    repeats: int = Field(default=1, ge=1, le=10)
    budget: ExperimentBudget = Field(default_factory=ExperimentBudget)

    @field_validator('base_config')
    @classmethod
    def strict_base_configuration(cls, value):
        # Apply strict validation to nested configuration fields as well as axes.
        config = SolveConfig.model_validate(value, strict=True).model_dump(mode='json')
        if config['target']['mode'] != 'nearest_k':
            raise ValueError('Experiments require the nearest_k target')
        return config

    @model_validator(mode='after')
    def deduplicate_axes(self):
        for name in ('matrix_ids', 'k_values', 'sigma_values', 'threads_values',
                     'memory_limit_gib_values', 'adaptive_shift_values'):
            setattr(self, name, list(dict.fromkeys(getattr(self, name))))
        return self


def config_diff(before: dict, after: dict) -> list[dict]:
    """Return sorted leaf differences without retaining mutable caller values."""
    differences = []
    missing = object()

    def visit(left, right, path):
        if left is missing and isinstance(right, dict) and right:
            left = {}
        if right is missing and isinstance(left, dict) and left:
            right = {}
        if isinstance(left, dict) and isinstance(right, dict):
            for key in sorted(left.keys() | right.keys()):
                visit(left.get(key, missing), right.get(key, missing), f'{path}.{key}' if path else key)
        elif left is missing or right is missing or left != right or (isinstance(left, bool) != isinstance(right, bool)):
            differences.append({'path': path,
                                'before': None if left is missing else copy.deepcopy(left),
                                'after': None if right is missing else copy.deepcopy(right)})

    visit(before, after, '')
    return differences


def _digest(value):
    """Use the gateway's existing encoding for solve-plan and config integrity."""
    return hashlib.sha256(encode(value).encode()).hexdigest()


def _combinations(definition):
    return product(definition['matrix_ids'], definition['k_values'], definition['sigma_values'],
                   definition['threads_values'], definition['memory_limit_gib_values'],
                   definition['adaptive_shift_values'])


def _budget(definition):
    count = math.prod(len(definition[name]) for name in ('matrix_ids', 'k_values', 'sigma_values',
                     'threads_values', 'memory_limit_gib_values', 'adaptive_shift_values'))
    runs = count * definition['repeats']
    if count > 100 or runs > 100:
        raise ServiceError('EXPERIMENT_RUN_LIMIT', 'Experiments support at most 100 cells and 100 runs')
    resources = definition['base_config'].get('resources', {})
    walltime = resources.get('walltime_seconds', 1800) * runs
    disk = resources.get('disk_limit_gib', 20) * runs
    limits = definition['budget']
    if runs > limits['max_runs'] or walltime > limits['max_total_walltime_seconds'] or disk > limits['max_total_disk_gib']:
        raise ServiceError('EXPERIMENT_BUDGET_EXCEEDED', 'The sum of per-run hard limits exceeds the experiment budget')
    return {**limits, 'total_runs': runs, 'total_walltime_seconds': walltime,
            'total_disk_gib': disk, 'peak_memory_limit_gib': max(definition['memory_limit_gib_values'])}


def _plan_hash(plan):
    return content_hash({key: value for key, value in plan.items() if key != 'hash'})


def _parameters(combination):
    _, k, sigma, threads, memory, adaptive = combination
    return {'k': k, 'sigma': sigma, 'threads': threads,
            'memory_limit_gib': memory, 'adaptive_shift': adaptive}


def _cell_config(definition, matrix, parameters):
    """Resolve the exact same base-plus-axis configuration for freeze and replay."""
    config = copy.deepcopy(definition['base_config'])
    config['problem'].update(matrix_id=matrix['id'], storage=matrix['storage'])
    config['target'].update(mode='nearest_k', k=parameters['k'], sigma=parameters['sigma'], interval=None)
    config['resources'].update(threads=parameters['threads'], memory_limit_gib=parameters['memory_limit_gib'])
    config['solver']['adaptive_shift']['enabled'] = parameters['adaptive_shift']
    return SolveConfig.model_validate(config, strict=True).model_dump(mode='json')


def validate_frozen_plan(plan: dict, lookup: Callable[[str, str], dict | None]) -> dict:
    """Check a frozen graph using metadata only, including its replayed definition."""
    try:
        fields = {'id', 'schema_version', 'project_id', 'definition', 'cells', 'budget',
                  'warnings', 'created_at', 'hash'}
        if (not isinstance(plan, dict) or set(plan) != fields
            or not isinstance(plan['id'], str) or not plan['id'].startswith('ep_')
            or not 1 <= len(plan['id']) <= 200 or plan['schema_version'] != '1.0'
            or _plan_hash(plan) != plan['hash']):
            raise ValueError('Experiment plan hash or shape mismatch')
        definition = ExperimentSpec.model_validate(copy.deepcopy(plan['definition'])).model_dump(mode='json')
        definition_shape=copy.deepcopy(definition)
        # New optional solver defaults must not invalidate a correctly hashed
        # older snapshot. All explicitly frozen values and references remain
        # checked; only default expansion is compared semantically below.
        definition_shape['base_config']=plan['definition']['base_config']
        if (content_hash(definition_shape) != content_hash(plan['definition'])
            or plan['project_id'] != definition['project_id']
            or content_hash(_budget(definition)) != content_hash(plan['budget'])):
            raise ValueError('Experiment definition or budget mismatch')
        combinations = list(_combinations(definition))
        if not isinstance(plan['cells'], list) or len(combinations) != len(plan['cells']):
            raise ValueError('Experiment cells mismatch')
        baselines = {}
        for index, (cell, combination) in enumerate(zip(plan['cells'], combinations), 1):
            matrix_id = combination[0]
            parameters = _parameters(combination)
            if (not isinstance(cell, dict)
                or set(cell) != {'id', 'matrix_id', 'matrix_hash', 'plan_id', 'plan_hash',
                                 'config', 'parameters', 'configuration_diff'}
                or cell['id'] != f'c{index:03d}' or cell['matrix_id'] != matrix_id
                or content_hash(cell['parameters']) != content_hash(parameters)):
                raise ValueError('Experiment cell parameters mismatch')
            frozen = lookup('plans', cell['plan_id'])
            config = lookup('configs', frozen['config_id']) if frozen else None
            matrix = lookup('matrices', matrix_id)
            if (not frozen or not config or not matrix or frozen['id'] != cell['plan_id']
                or config['id'] != frozen['config_id'] or matrix['id'] != matrix_id
                or frozen['project_id'] != plan['project_id'] or matrix['project_id'] != plan['project_id']
                or frozen['matrix_id'] != matrix_id or frozen['input_hash'] != cell['matrix_hash']
                or matrix['hash'] != cell['matrix_hash'] or frozen['hash'] != cell['plan_hash']
                or config['hash'] != _digest(config['config'])
                or frozen['hash'] != _digest({'config': config['hash'], 'matrix': cell['matrix_hash'],
                                              'capabilities': frozen['capabilities']})):
                raise ValueError('Frozen cell references mismatch')
            expected = _cell_config(definition, matrix, parameters)
            baseline = baselines.setdefault(matrix_id, cell['config'])
            expected_hash = content_hash(expected)
            raw_hash=content_hash(cell['config'])
            if (any(content_hash(value) != raw_hash for value in (frozen['config'],config['config']))
                or any(content_hash(SolveConfig.model_validate(value,strict=True).model_dump(mode='json')) != expected_hash
                       for value in (cell['config'], frozen['config'], config['config']))
                or content_hash(cell['configuration_diff']) != content_hash(config_diff(baseline, cell['config']))):
                raise ValueError('Frozen cell configuration does not replay its definition')
    except (KeyError, TypeError, ValueError, ServiceError):
        raise ServiceError('EXPERIMENT_PLAN_MISMATCH', 'Frozen experiment data failed integrity validation', 409) from None
    return plan


class ExperimentService:
    def __init__(self, gateway: ExperimentGateway):
        self.gateway = gateway

    def plan(self, spec: dict) -> dict:
        try:
            definition = ExperimentSpec.model_validate(copy.deepcopy(spec)).model_dump(mode='json')
        except (ValidationError, ValueError, TypeError):
            raise ServiceError('INVALID_EXPERIMENT', 'Experiment fields must satisfy the strict finite schema') from None
        budget = _budget(definition)
        gateway = self.gateway
        with gateway._mutex, gateway.store.transaction():
            gateway._project(definition['project_id'], True)
            matrices = {}
            for matrix_id in definition['matrix_ids']:
                matrix = gateway._get('matrices', matrix_id)
                if matrix['project_id'] != definition['project_id']:
                    raise ServiceError('PROJECT_MISMATCH', status=409)
                if matrix.get('archived'):
                    raise ServiceError('MATRIX_ARCHIVED', status=409)
                matrices[matrix_id] = matrix
            cells, baselines = [], {}
            warnings = ['Total walltime and disk budgets sum per-run hard limits; they are not runtime estimates',
                        'One worker runs at a time; peak memory is the maximum configured per-run limit',
                        'Requested thread counts do not guarantee actual CPU utilization']
            for index, combination in enumerate(_combinations(definition), 1):
                matrix_id = combination[0]
                parameters = _parameters(combination)
                config = _cell_config(definition, matrices[matrix_id], parameters)
                frozen = gateway.create_plan(config=config, project_id=definition['project_id'])
                resolved = frozen['config']
                baseline = baselines.setdefault(matrix_id, resolved)
                cells.append({'id': f'c{index:03d}', 'matrix_id': matrix_id,
                              'matrix_hash': matrices[matrix_id]['hash'], 'plan_id': frozen['id'],
                              'plan_hash': frozen['hash'], 'config': copy.deepcopy(resolved),
                              'parameters': parameters,
                              'configuration_diff': config_diff(baseline, resolved)})
                for warning in frozen.get('warnings', []):
                    if warning not in warnings: warnings.append(warning)
            plan = {'id': 'ep_' + uuid.uuid4().hex, 'schema_version': '1.0',
                    'project_id': definition['project_id'], 'definition': definition,
                    'cells': cells, 'budget': budget, 'warnings': warnings, 'created_at': utcnow()}
            plan['hash'] = _plan_hash(plan)
            gateway.store.put('experiment_plans', plan['id'], plan)
            gateway.store.audit('experiment.plan', plan['id'])
            return copy.deepcopy(plan)

    def _validated_plan(self, plan_id):
        gateway = self.gateway
        plan = gateway.store.get('experiment_plans', plan_id)
        if plan is None:
            raise ServiceError('NOT_FOUND', 'The requested experiment plan does not exist', 404)
        if not isinstance(plan, dict) or plan.get('id') != plan_id:
            raise ServiceError('EXPERIMENT_PLAN_MISMATCH', status=409)
        return validate_frozen_plan(plan, gateway.store.get)

    def submit(self, experiment_plan_id: str) -> dict:
        gateway = self.gateway
        with gateway._mutex, gateway.store.transaction():
            plan = self._validated_plan(experiment_plan_id)
            batch_id = 'b_' + experiment_plan_id.removeprefix('ep_')
            existing = gateway.store.get('benchmarks', batch_id)
            if existing is not None:
                if existing.get('experiment_plan_id') != plan['id'] or existing.get('experiment_hash') != plan['hash']:
                    raise ServiceError('EXPERIMENT_PLAN_MISMATCH', status=409)
                return gateway.get_benchmark(batch_id)
            gateway._project(plan['project_id'], True)
            if gateway.jobs.queued_count() + plan['budget']['total_runs'] > 100:
                raise ServiceError('BENCHMARK_QUEUE_LIMIT', 'Batch exceeds the available queue capacity', 429)
            definition = plan['definition']
            batch = {'id': batch_id, 'project_id': plan['project_id'],
                     'experiment_plan_id': plan['id'], 'experiment_hash': plan['hash'],
                     'definition': copy.deepcopy(definition), 'cells': copy.deepcopy(plan['cells']),
                     'budget': copy.deepcopy(plan['budget']), 'matrix_ids': definition['matrix_ids'],
                     'repeats': definition['repeats'], 'plan_hashes': [cell['plan_hash'] for cell in plan['cells']],
                     'created_at': utcnow(), 'jobs': []}
            gateway.store.put('benchmarks', batch_id, batch)
            for cell in batch['cells']:
                for repeat in range(1, definition['repeats'] + 1):
                    job = gateway.submit_job(cell['plan_id'], idempotency_key=f'{batch_id}:{cell["id"]}:{repeat}',
                                             experiment={'batch_id': batch_id, 'cell_id': cell['id'], 'repeat': repeat})
                    batch['jobs'].append(job['id'])
            gateway.store.put('benchmarks', batch_id, batch)
            gateway.store.audit('experiment.submit', batch_id)
            return gateway.get_benchmark(batch_id)


def _summary(jobs):
    def recorded_time(job):
        value = job.get('metrics', {}).get('walltime_seconds')
        return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None

    times = [value for job in jobs if (value := recorded_time(job)) is not None]
    certified = [value for job in jobs if job['status'] == 'SUCCEEDED' and (value := recorded_time(job)) is not None]
    def median(values):
        if not values: return None
        ordered = sorted(values)
        middle = len(ordered) // 2
        if len(ordered) % 2: return ordered[middle]
        lower, upper = ordered[middle - 1:middle + 1]
        # Recorded times are nonnegative, so this midpoint cannot overflow.
        return lower + (upper - lower) / 2

    return {'total': len(jobs), 'terminal': sum(job['status'] in TERMINAL for job in jobs),
            'failed': sum(job['status'] in {'FAILED', 'CANCELLED', 'TIMED_OUT'} for job in jobs),
            'partial': sum(job['status'] == 'PARTIAL' for job in jobs),
            'certified': sum(job['status'] == 'SUCCEEDED' for job in jobs),
            'time_median_seconds': median(times),
            'time_range_seconds': [min(times), max(times)] if times else None,
            'timing_denominator': len(times),
            'aggregation_scope': 'All recorded completed timings including failures; all rows retained',
            'certified_time_median_seconds': median(certified),
            'certified_time_range_seconds': [min(certified), max(certified)] if certified else None,
            'certified_timing_denominator': len(certified)}


def summarize_batch(batch: dict, jobs: list[dict]) -> dict:
    """Retain failed and unfinished rows; report certified timings separately."""
    groups = []
    for cell in batch.get('cells', []):
        members = [job for job in jobs if job.get('experiment', {}).get('cell_id') == cell['id']]
        groups.append({'cell_id': cell['id'], 'parameters': copy.deepcopy(cell['parameters']),
                       'matrix_id': cell['matrix_id'], 'job_ids': [job['id'] for job in members],
                       'summary': _summary(members),
                       'configuration_diff': copy.deepcopy(cell.get('configuration_diff', []))})
    return {'summary': _summary(jobs), 'groups': groups}
