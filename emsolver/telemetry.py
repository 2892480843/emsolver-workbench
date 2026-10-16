"""Bounded best-effort secondary samples; canonical events live elsewhere."""
from __future__ import annotations

import math
import queue
import re
import shutil
import threading

from .jobs import JobStatus


NUMERIC_FIELDS = frozenset({'elapsed_seconds', 'iteration', 'residual', 'sigma_work', 'n_accepted', 'attempt',
                            'rss_tree_bytes', 'peak_rss_tree_bytes', 'disk_bytes', 'factorizations',
                            'linear_solves', 'outer_iterations', 'linear_iterations', 'matrix_vector_products'})
CRITICAL_EVENTS = frozenset({'QUEUED', 'RUNNING', 'VERIFYING', 'CANCELLING', 'SUCCEEDED', 'PARTIAL', 'FAILED',
                             'CANCELLED', 'TIMED_OUT', 'independent_validation_completed'})


class SecondaryTelemetry:
    def __init__(self, exporter=None, capacity=256, start_worker=True):
        if type(capacity) is not int or not 1 <= capacity <= 10000:
            raise ValueError('Telemetry capacity must be between 1 and 10000')
        self.capacity = capacity
        self.exporter = exporter
        self._queue = queue.Queue(maxsize=capacity)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._values = dict(accepted=0, exported=0, dropped=0, exporter_failures=0, ignored_critical=0, in_flight=0)
        self._thread = None
        if exporter is not None and start_worker:
            self._thread = threading.Thread(target=self._loop, name='emsolver-telemetry', daemon=True)
            self._thread.start()

    def emit(self, sample):
        """Never invoke an exporter or wait for queue capacity on this path."""
        clean = {}
        if isinstance(sample, dict) and sample.get('event') in CRITICAL_EVENTS:
            with self._lock: self._values['ignored_critical'] += 1
            return False
        if isinstance(sample, dict) and sample.get('event') not in CRITICAL_EVENTS:
            for key in NUMERIC_FIELDS:
                value = sample.get(key)
                if type(value) in (int, float):
                    try:
                        if math.isfinite(value): clean[key] = value
                    except OverflowError:
                        pass
            for key in ('stage', 'event'):
                value = sample.get(key)
                if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', value):
                    clean[key] = value
        with self._lock:
            if not clean or self._stop.is_set():
                self._values['dropped'] += 1
                return False
            try:
                self._queue.put_nowait(clean)
            except queue.Full:
                self._values['dropped'] += 1
                return False
            self._values['accepted'] += 1
            return True

    def counters(self):
        with self._lock:
            return {**self._values, 'queued': self._queue.qsize(), 'capacity': self.capacity,
                    'closed': self._stop.is_set()}

    def drain(self, limit=None):
        limit = self.capacity if limit is None else limit
        if type(limit) is not int or not 1 <= limit <= 10000: raise ValueError('Invalid telemetry drain limit')
        rows = []
        for _ in range(limit):
            try: rows.append(self._queue.get_nowait())
            except queue.Empty: break
        return rows

    def _loop(self):
        while not self._stop.is_set():
            try: sample = self._queue.get(timeout=.1)
            except queue.Empty: continue
            with self._lock: self._values['in_flight'] = 1
            try:
                self.exporter(sample)
            except BaseException:
                with self._lock:
                    self._values['exporter_failures'] += 1
                    self._values['dropped'] += 1
            else:
                with self._lock: self._values['exported'] += 1
            finally:
                with self._lock: self._values['in_flight'] = 0

    def close(self, timeout=.1):
        if type(timeout) not in (int, float) or not 0 <= timeout <= 5:
            raise ValueError('Telemetry close timeout must be zero to five seconds')
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)


def render_metrics(store, root, telemetry=None):
    """Fixed status labels only; SQL projection avoids decoding job documents."""
    statuses = [status.value for status in JobStatus] + ['UNKNOWN']
    counts = dict.fromkeys(statuses, 0)
    for status, count in store.status_counts('jobs').items():
        counts[status if status in counts else 'UNKNOWN'] += count
    lines = ['# HELP emsolver_jobs Jobs by canonical lifecycle state.', '# TYPE emsolver_jobs gauge']
    lines.extend(f'emsolver_jobs{{status="{status}"}} {counts[status]}' for status in statuses)
    try:
        free = shutil.disk_usage(root).free
    except OSError:
        lines.extend(['# TYPE emsolver_disk_available gauge', 'emsolver_disk_available 0'])
    else:
        lines.extend(['# TYPE emsolver_disk_available gauge', 'emsolver_disk_available 1',
                      '# HELP emsolver_free_disk_bytes Free bytes on the data filesystem.',
                      '# TYPE emsolver_free_disk_bytes gauge', '# UNIT emsolver_free_disk_bytes bytes',
                      f'emsolver_free_disk_bytes {free}'])
    if telemetry is not None:
        values = telemetry.counters()
        for name in ('accepted', 'exported', 'dropped', 'exporter_failures', 'ignored_critical'):
            metric = f'emsolver_telemetry_{name}_total'
            lines.extend([f'# TYPE {metric} counter', f'{metric} {values[name]}'])
        for name in ('queued', 'in_flight', 'capacity'):
            metric = f'emsolver_telemetry_{name}'
            lines.extend([f'# TYPE {metric} gauge', f'{metric} {values[name]}'])
    return '\n'.join(lines) + '\n# EOF\n'
