"""Incremental worker input and bounded replay of the canonical event journal."""
from __future__ import annotations

import json
from pathlib import Path
import re
import tempfile

from .store import Store, encode


_WORKER_FIELDS = frozenset({
    'stage', 'event', 'elapsed_seconds', 'iteration', 'residual', 'residuals',
    'sigma_work', 'n_accepted', 'error_code', 'attempt', 'metrics',
    'sigma_target','residual_max','numerical_status','target_certification','status',
    'internal_k','subspace','n','nnz','requested_modes',
    'locked_indices','stalled_checks','stagnation_threshold','restart_reason',
})


def _reject_constant(value):
    raise ValueError('Non-finite JSON value: ' + value)


def _limit(value):
    if type(value) is not int or not 1 <= value <= 1000:
        raise ValueError('Event limit must be an integer between 1 and 1000')
    return value


class WorkerEventReader:
    """Consume complete UTF-8 JSONL records, retaining partial lines for later."""

    def __init__(self, path: Path, offset=0):
        self.path = Path(path)
        if type(offset) is not int or offset<0:raise ValueError('Invalid worker event offset')
        self._offset = offset

    @property
    def offset(self):return self._offset

    def rewind(self,offset):
        if type(offset) is not int or not 0<=offset<=self._offset:raise ValueError('Invalid worker event rollback')
        self._offset=offset

    def drain(self) -> list[dict]:
        events = []
        try:
            handle = self.path.open('rb')
        except FileNotFoundError:
            return events
        with handle:
            handle.seek(self._offset)
            while True:
                line = handle.readline()
                if not line.endswith(b'\n'):
                    break
                self._offset = handle.tell()
                try:
                    event = json.loads(line.decode('utf-8'), parse_constant=_reject_constant)
                except (UnicodeDecodeError, ValueError):
                    continue
                if isinstance(event, dict):
                    events.append({key: value for key, value in event.items() if key in _WORKER_FIELDS})
        return events


class EventJournal:
    """Keep SQLite authoritative; materialize download files only on demand."""

    def __init__(self, root: Path, store: Store):
        self.root = Path(root).resolve()
        self.store = store

    def append(self, job_id, event) -> dict:
        return self.store.append_event(job_id, event)

    def page(self, job_id, after=0, limit=200) -> dict:
        if type(after) is not int or after < 0:
            raise ValueError('Event sequence must be a non-negative integer')
        limit = _limit(limit)
        rows = self.store.events(job_id, after, limit=limit + 1)
        items = rows[:limit]
        return {
            'items': items,
            'next_after': items[-1]['seq'] if items else after,
            'has_more': len(rows) > limit,
        }

    def tail(self, job_id, limit=1000) -> list[dict]:
        rows = self.store.events(job_id, limit=_limit(limit), descending=True)
        return list(reversed(rows))

    def export(self, job_id) -> Path:
        if not isinstance(job_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', job_id):
            raise ValueError('Invalid job identity for event export')
        directory = self.root / 'runs' / job_id
        if not directory.resolve().is_relative_to(self.root):
            raise ValueError('Event export must remain inside the data root')
        upper = self.store.event_max_seq(job_id)
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / 'events.jsonl'
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode='w', encoding='utf-8', dir=directory,
                prefix='.events-', suffix='.tmp', delete=False,
            ) as handle:
                temporary = Path(handle.name)
                after = 0
                while after < upper:
                    rows = self.store.events(job_id, after, limit=500, until=upper)
                    if not rows:
                        break
                    for event in rows:
                        handle.write(encode(event) + '\n')
                    after = rows[-1]['seq']
            temporary.replace(destination)
            return destination
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
