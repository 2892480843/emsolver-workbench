"""Job lifecycle rules and indexed queries, independent of HTTP and processes."""
from __future__ import annotations

import copy
from enum import StrEnum
from typing import Any

from .store import Store, utcnow
from .contracts import TERMINAL_STATES


class JobStatus(StrEnum):
    QUEUED = 'QUEUED'
    RUNNING = 'RUNNING'
    VERIFYING = 'VERIFYING'
    CANCELLING = 'CANCELLING'
    SUCCEEDED = 'SUCCEEDED'
    PARTIAL = 'PARTIAL'
    FAILED = 'FAILED'
    CANCELLED = 'CANCELLED'
    TIMED_OUT = 'TIMED_OUT'


TERMINAL = frozenset(JobStatus(value) for value in TERMINAL_STATES)
ACTIVE = tuple(status for status in JobStatus if status not in TERMINAL)
RECOVERABLE = (JobStatus.RUNNING,JobStatus.VERIFYING,JobStatus.CANCELLING)
_TRANSITIONS = {
    JobStatus.QUEUED: frozenset((JobStatus.RUNNING,JobStatus.FAILED,JobStatus.CANCELLED)),
    JobStatus.RUNNING: TERMINAL | {JobStatus.VERIFYING,JobStatus.CANCELLING},
    JobStatus.VERIFYING: TERMINAL | {JobStatus.CANCELLING},
    JobStatus.CANCELLING: frozenset((JobStatus.CANCELLED,JobStatus.FAILED)),
}


def transition(job: dict[str,Any], destination: JobStatus | str) -> dict[str,Any]:
    """Return a detached state transition; persistence stays in the caller's transaction."""
    try:
        current=JobStatus(job['status']);target=JobStatus(destination)
    except (ValueError,KeyError) as exc:
        raise ValueError('INVALID_JOB_TRANSITION: unknown state') from exc
    if target not in _TRANSITIONS.get(current,()):
        raise ValueError(f'INVALID_JOB_TRANSITION: {current} -> {target}')
    updated=copy.deepcopy(job)
    updated.update(status=target.value,stage=target.value.lower())
    if target in TERMINAL:
        updated['finished_at']=utcnow()
    return updated


class JobRepository:
    """A focused query boundary: no scheduler needs to enumerate completed jobs."""
    def __init__(self,store: Store):
        self.store=store

    def next_queued(self) -> dict[str,Any] | None:
        import json
        with self.store.lock:
            row=self.store.db.execute("SELECT j.body FROM entities j LEFT JOIN entities p ON p.kind='projects' AND p.id=j.project_id WHERE j.kind='jobs' AND j.status='QUEUED' ORDER BY COALESCE(CAST(json_extract(p.body,'$.policy.priority') AS BIGINT),0) DESC,j.seq LIMIT 1").fetchone()
        return json.loads(row[0]) if row else None

    def queued_count(self) -> int:
        return self.store.count('jobs',status=JobStatus.QUEUED)

    def recoverable(self) -> list[dict[str,Any]]:
        return self.store.select('jobs',status=RECOVERABLE,limit=100)

    def page(self,*,limit: int=50,cursor: str | None=None,
             project_id: str | None=None,status: str | None=None) -> dict[str,Any]:
        if status is not None:
            status=JobStatus(status).value
        return self.store.page('jobs',limit=limit,cursor=cursor,project_id=project_id,status=status)

    def summary(self,project_id: str | None=None) -> dict[str,Any]:
        # One read transaction gives counters from the same database snapshot;
        # a write transaction already owned by the caller is reused safely.
        with self.store.read_transaction():
            all_counts=self.store.status_counts('jobs',project_id=project_id)
            counts={status.value:all_counts.get(status.value,0) for status in JobStatus}
            return {'projects':self.store.count('projects'),
                    'matrices':self.store.count('matrices',project_id=project_id),
                    'jobs':sum(all_counts.values()),
                    'active_jobs':sum(counts[status.value] for status in ACTIVE),
                    'certified_jobs':counts[JobStatus.SUCCEEDED],
                    'job_status_counts':counts}
