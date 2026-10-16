"""Append-only advice adoption and observed effects, with protected conditions."""
from __future__ import annotations

import copy
import math
from typing import Callable, Protocol
import uuid

from .advice import ALLOWED_PATHS
from .contracts import content_hash, resolve_config
from .errors import ServiceError
from .experiments import config_diff
from .store import utcnow


KIND = 'advice_effects'
PROTECTED = ('problem', 'target', 'validation', 'resources', 'observability', 'output', 'schema_version')


class EffectsStore(Protocol):
    def transaction(self): ...
    def put(self, kind: str, identity: str, body: dict): ...
    def get(self, kind: str, identity: str): ...
    def list(self, kind: str): ...


def _configs(before, after):
    try:
        before, after = resolve_config(before), resolve_config(after)
    except (TypeError, ValueError):
        raise ServiceError('INVALID_ADVICE_CONFIG') from None
    differences = config_diff(before, after)
    if any(row['path'] not in ALLOWED_PATHS for row in differences):
        raise ServiceError('ADVICE_FIELD_PROTECTED', 'Only approved solver settings may differ')
    return before, after, differences


def _metric(job, names):
    metrics = {**((job.get('result') or {}).get('metrics') or {}), **(job.get('metrics') or {})}
    for name in names:
        value = metrics.get(name)
        if type(value) in (int, float) and math.isfinite(value) and value >= 0:
            return value
    return None


def _snapshot(job):
    validation = (job.get('result') or {}).get('validation') or {}
    residuals = validation.get('residuals') or []
    finite_residuals = [value for value in residuals if type(value) in (int, float) and math.isfinite(value)]
    return {'job_id': job['id'], 'status': job.get('status'),
            'numerical_status': validation.get('numerical_status'),
            'target_certification': validation.get('target_certification'),
            'error_code': (job.get('error') or {}).get('code') or (job.get('result') or {}).get('error_code'),
            'walltime_seconds': _metric(job, ('walltime_seconds',)),
            'peak_memory_bytes': _metric(job, ('peak_rss_tree_bytes', 'peak_memory_bytes')),
            'residual_max': max(finite_residuals) if finite_residuals else validation.get('residual_max'),
            'outer_iterations': _metric(job, ('outer_iterations',)),
            'linear_iterations': _metric(job, ('linear_iterations',))}


class AdviceEffects:
    """Callbacks keep this ledger independent from the application service.

    No method adopts a suggestion or edits jobs/configs. Derived, linked and
    observed records receive distinct IDs; historical records are never put twice.
    """
    def __init__(self, store: EffectsStore, get_job: Callable[[str], dict], get_config: Callable[[str], dict]):
        self.store, self.get_job, self.get_config = store, get_job, get_config

    def _append(self, record_type, effect_id=None, **fields):
        identity = 'ae_' + uuid.uuid4().hex
        item = {'id': identity, 'effect_id': effect_id or identity, 'record_type': record_type,
                'schema_version': '1.0', 'created_at': utcnow(), **copy.deepcopy(fields)}
        if self.store.get(KIND, identity) is not None:
            raise ServiceError('EFFECT_ID_CONFLICT', status=409)
        self.store.put(KIND, identity, item)
        return item

    def _effect(self, effect_id):
        effect = self.store.get(KIND, effect_id)
        if not effect or effect.get('record_type') != 'derived':
            raise ServiceError('NOT_FOUND', 'Advice effect does not exist', 404)
        return effect

    def create(self, source_job_id, derived_config_id):
        with self.store.transaction():
            source, derived = self.get_job(source_job_id), self.get_config(derived_config_id)
            before, after, differences = _configs(source['config'], derived['config'])
            derivation = derived.get('derivation') or {}
            if (derivation.get('source_job_id') != source_job_id or
                    derivation.get('source_config_hash') != content_hash(before)):
                raise ServiceError('ADVICE_SOURCE_MISMATCH', 'The derived configuration does not match its source job', 409)
            if derived.get('project_id') is not None and derived['project_id'] != source.get('project_id'):
                raise ServiceError('PROJECT_MISMATCH', status=409)
            return self._append('derived', source_job_id=source_job_id, derived_config_id=derived_config_id,
                                project_id=source.get('project_id'), matrix_hash=source.get('matrix_hash'),
                                before_config=before, after_config=after, before_config_hash=content_hash(before),
                                after_config_hash=content_hash(after), configuration_diff=differences,
                                protected_conditions={key: copy.deepcopy(before[key]) for key in PROTECTED},
                                automatic_adoption=False, source_snapshot=_snapshot(source))

    def list(self, source_job_id=None):
        return [item for item in self.store.list(KIND) if source_job_id is None or item.get('source_job_id') == source_job_id]

    def get(self, effect_id):
        effect = self._effect(effect_id)
        records = [item for item in self.list(effect['source_job_id']) if item.get('effect_id') == effect_id]
        return {**effect, 'records': records}

    def link(self, effect_id, new_job_id):
        with self.store.transaction():
            effect = self._effect(effect_id)
            new = self.get_job(new_job_id)
            if new_job_id == effect['source_job_id'] or new.get('project_id') != effect.get('project_id'):
                raise ServiceError('ADVICE_JOB_MISMATCH', status=409)
            if (new.get('matrix_hash') != effect.get('matrix_hash') or not effect.get('matrix_hash') or
                    content_hash(resolve_config(new['config'])) != effect['after_config_hash']):
                raise ServiceError('ADVICE_JOB_MISMATCH', 'New job must run the frozen derived configuration and input', 409)
            links = [item for item in self.list(effect['source_job_id']) if item.get('effect_id') == effect_id and item['record_type'] == 'linked']
            if links:
                if links[0]['new_job_id'] != new_job_id:
                    raise ServiceError('EFFECT_ALREADY_LINKED', status=409)
                return links[0]
            return self._append('linked', effect_id, source_job_id=effect['source_job_id'],
                                derived_config_id=effect['derived_config_id'], new_job_id=new_job_id,
                                project_id=effect.get('project_id'), after_config_hash=effect['after_config_hash'])

    def evaluate(self, effect_id, *, persist=True):
        """Append current observed outcomes; report speedup only for fair passes."""
        with self.store.transaction():
            effect = self._effect(effect_id)
            links = [item for item in self.list(effect['source_job_id']) if item.get('effect_id') == effect_id and item['record_type'] == 'linked']
            if not links:
                raise ServiceError('EFFECT_NOT_LINKED', status=409)
            source, new = self.get_job(effect['source_job_id']), self.get_job(links[0]['new_job_id'])
            before, after = _snapshot(source), _snapshot(new)
            same_config = (content_hash(resolve_config(source['config'])) == effect['before_config_hash'] and
                           content_hash(resolve_config(new['config'])) == effect['after_config_hash'])
            protected = all(source['config'].get(key) == new['config'].get(key) == effect['protected_conditions'][key] for key in PROTECTED)
            matrix = bool(source.get('matrix_hash')) and source.get('matrix_hash') == new.get('matrix_hash') == effect.get('matrix_hash')
            runtime = bool(source.get('runtime', {}).get('hardware_id')) and source.get('runtime') == new.get('runtime')
            certified = all(row['status'] == 'SUCCEEDED' and row['numerical_status'] == 'PASSED' and
                            row['target_certification'] == 'CERTIFIED' for row in (before, after))
            timings = all(type(row['walltime_seconds']) in (float, int) and row['walltime_seconds'] > 0 for row in (before, after))
            comparable = all((same_config, protected, matrix, runtime, certified, timings))
            conditions = {'frozen_configs_match': same_config, 'protected_conditions_match': protected,
                          'matrix_hash_match': matrix, 'runtime_match': runtime,
                          'both_certified': certified, 'timings_available': timings}
            reason = '同输入、目标、精度、资源和环境的认证运行；使用实际运行耗时。' if comparable else '不满足公平比较条件：' + ', '.join(key for key, value in conditions.items() if not value)
            observation=dict(source_job_id=effect['source_job_id'],derived_config_id=effect['derived_config_id'],
                             new_job_id=new['id'],project_id=effect.get('project_id'),before=before,after=after,
                             comparable=comparable,speedup=before['walltime_seconds']/after['walltime_seconds'] if comparable else None,
                             conditions=conditions,reason=reason,automatic_adoption=False)
            if not persist:return {'record_type':'preview','effect_id':effect_id,**observation}
            fingerprint=content_hash(observation)
            for item in self.list(effect['source_job_id']):
                if item.get('effect_id')==effect_id and item.get('record_type')=='observed' and item.get('observation_hash')==fingerprint:
                    return item
            return self._append('observed',effect_id,observation_hash=fingerprint,**observation)
