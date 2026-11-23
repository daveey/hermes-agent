"""Bounded native goal/cron membership capture on the content feed. [daveey]

Authorized startup supplies ONE profile's exact path; there is no discovery or
request I/O. Native hints accelerate a 30s recapture deadline. Read failure is
unavailable, never a fresh empty inventory. Other annotation families are NOT
covered by these two sources.
"""
from contextlib import closing
from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
import sqlite3
import time

from hermes_cli.fact_capture import CaptureBudgetExceeded
from hermes_cli.fact_contract import Fact, FactKey
from hermes_cli.fact_source_watch import NativeSourceWatch


@dataclass(frozen=True)
class MembershipCapture:
    value: object
    after: str
    observed_at: float
    next_deadline: float


def _goals(path, budget):
    from hermes_state import _connect_tracked_db
    from hermes_state_holders import read_only_db_uri
    deadline = time.monotonic() + budget.max_seconds
    with closing(_connect_tracked_db(read_only_db_uri(path), tracking_path=path,
                                     uri=True, isolation_level=None, timeout=0)) as conn:
        conn.execute('PRAGMA query_only=ON')
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, budget.max_bytes)
        conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        conn.execute('BEGIN')
        rows, size = [], 0
        cursor = conn.execute('SELECT key,value FROM state_meta WHERE key>=? AND key<? LIMIT ?',
                              ('goal:', 'goal;', budget.max_keys + 1))
        for key, value in cursor:
            size += len(str(key).encode()) + len(str(value).encode())
            if len(rows) >= budget.max_keys or size > budget.max_bytes or time.monotonic() >= deadline:
                raise CaptureBudgetExceeded('goal inventory exceeds capture budget')
            rows.append((key, value))
    active = []
    for key, raw in rows:
        try:
            data = json.loads(raw or '')
        except (TypeError, ValueError):
            continue  # Matches canonical malformed-record semantics, not I/O failure.
        if isinstance(data, dict) and data.get('status') == 'active' and str(data.get('goal') or '').strip():
            active.append(key[len('goal:'):])
    if time.monotonic() >= deadline:
        raise CaptureBudgetExceeded('goal capture deadline exceeded')
    return {'active': tuple(sorted(active))}


def _cron(path, budget):
    from hermes_cli.cron_session_activity import owners_from_jobs
    try:
        with path.open('rb') as stream:
            raw = stream.read(budget.max_bytes + 1)
    except FileNotFoundError:
        # Jobs inventory is optional until the first job is created. Other
        # filesystem failures (permissions/I/O) remain unavailable.
        raw = b'[]'
    if len(raw) > budget.max_bytes:
        raise CaptureBudgetExceeded('cron inventory exceeds capture budget')
    data = json.loads(raw.decode('utf-8-sig') or '[]')
    jobs = data.get('jobs', []) if isinstance(data, dict) else data
    if not isinstance(jobs, list):
        raise ValueError('invalid cron inventory')
    if len(jobs) > budget.max_keys:
        raise CaptureBudgetExceeded('cron inventory exceeds key budget')
    return {'owners': tuple(sorted(owners_from_jobs(jobs)))}


class AnnotationMembershipSource:
    def __init__(self, engine, source, path):
        if source.scope != engine.scope or source.kind not in ('annotations.goal', 'annotations.cron'):
            raise ValueError('invalid annotation source scope or kind')
        self.engine, self.source = engine, source
        if 'AnnotationInput' in engine._specs:
            raise ValueError('annotation input cannot be a derived kind')
        self.family = source.kind.removeprefix('annotations.')
        self.key = FactKey(engine.scope, 'AnnotationInput', self.family)
        self.path = Path(path).absolute()
        self._watch = None

    def watch(self, wake):
        if self._watch is not None:
            raise RuntimeError('annotation source already watched')
        self._watch = NativeSourceWatch((self.path,), wake)
        return self._watch

    def capture(self, cursor, budget):
        started = time.monotonic()
        if self._watch is not None:
            self._watch.check()
        value = (_goals if self.family == 'goal' else _cron)(self.path, budget)
        token = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
        if time.monotonic() - started >= budget.max_seconds:
            raise CaptureBudgetExceeded('annotation capture deadline exceeded')
        if self._watch is not None:
            self._watch.check()
        return MembershipCapture(value, token, time.time(), time.monotonic() + 30)

    def commit(self, capture):
        old = self.engine.store.snapshot(self.engine.scope).get(self.key)
        fact = Fact(self.key, capture.value, source_vector=((self.source, capture.after),),
                    observed_at=capture.observed_at, valid_until=capture.observed_at + 90, health='fresh')
        # Content-free hints do not churn facts; actual periodic capture renews
        # evidence before expiry. Never renew by simply re-running a serializer.
        if (old is not None and old.health == 'fresh' and old.value == fact.value
                and old.valid_until is not None and old.valid_until > capture.observed_at + 60):
            return
        self.engine.publish([fact], now=capture.observed_at)

    def failure(self):
        old = self.engine.store.snapshot(self.engine.scope).get(self.key)
        fact = (replace(old, health='stale', error_code='annotation_capture_failed') if old is not None
                and old.health != 'unknown' else Fact(self.key, {}, error_code='annotation_capture_failed'))
        self.engine.publish([fact], now=time.time())
