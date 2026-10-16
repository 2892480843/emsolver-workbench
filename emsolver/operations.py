"""Quota-only operational views; never grant a project business role."""
from collections import Counter
import re

from .access import require_operations
from .errors import ServiceError
from .governance import validated_policy


class Operations:
    def __init__(self, gateway):
        self.gateway = gateway

    def _row(self, project):
        usage = self.gateway.governance._usage_for_project(project)
        return {'project_id': project['id'], 'policy': usage['policy'],
                'usage': {key: value for key, value in usage.items() if key not in {'project_id', 'policy'}}}

    def policies(self, *, limit=50, cursor=None):
        require_operations()
        with self.gateway.store.transaction():
            try:
                page = self.gateway.store.page('projects', limit=limit, cursor=cursor)
            except ValueError:
                raise ServiceError('INVALID_CURSOR_OR_FILTER') from None
            return {**page, 'items': [self._row(project) for project in page['items']]}

    def update_policy(self, project_id, policy):
        require_operations()
        service = self.gateway
        with service._mutex, service.store.transaction():
            project = service.store.get('projects', project_id)
            if project is None:
                raise ServiceError('NOT_FOUND', status=404)
            project['policy'] = validated_policy({**project.get('policy', {}), **policy})
            project['version'] = project.get('version', 1) + 1
            service.store.put('projects', project_id, project)
            service.store.audit('operations.policy.update', project_id)
            return self._row(project)

    def summary(self):
        require_operations()
        service = self.gateway
        summary = service.jobs.summary()
        health = service.health()
        rows = service.store.audit()
        # The audit window is intentionally aggregate-only: no subjects, object
        # identifiers, free-form messages, paths or input/result values.
        actions = Counter(row.get('action') if isinstance(row.get('action'), str) and re.fullmatch(r'[a-z_.]{1,80}', row['action']) else 'other' for row in rows)
        results = Counter(row.get('result') if row.get('result') in {'ok', 'denied', 'failed'} else 'other' for row in rows)
        return {**summary,
                'health': {key: health[key] for key in ('service_alive', 'queue_available', 'backend_runnable', 'storage_writable', 'storage_free_bytes', 'version')},
                'audit': {'scope': 'latest_1000_aggregate_only', 'records_counted': len(rows), 'action_counts': dict(actions), 'result_counts': dict(results)}}
