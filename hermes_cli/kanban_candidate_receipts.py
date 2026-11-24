"""Bounded machine-local dispatcher evidence, never an admission input.

Observe actual short-circuit decisions and scores already computed by ranking.
No graph queries, policy replays, task events or sync-feed writes. A missing
receipt/score is unknown, not evidence of eligibility. Times are epoch ms.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import logging
import sqlite3
import time
import uuid

MAX_ROWS = 20000
MAX_PASSES = 2000
RETENTION_MS = 7 * 86400 * 1000
PER_PASS = 512
TABLE = 'kanban_candidate_receipts'
PASSES = 'kanban_candidate_passes'
_LOG = logging.getLogger(__name__)


@dataclass
class Capture:
    worker: str
    pass_time_ms: int
    limit: int = PER_PASS
    pass_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    records: dict = field(default_factory=dict)
    ranked: dict = field(default_factory=dict)
    claimed_ids: set = field(default_factory=set)
    dropped: int = 0


_current: ContextVar[Capture | None] = ContextVar('candidate_receipts', default=None)


@contextmanager
def collect(*, worker: str, now_ms=None, limit=PER_PASS):
    capture = Capture(worker, int(time.time() * 1000) if now_ms is None else now_ms, limit)
    token = _current.set(capture)
    try:
        yield capture
    finally:
        _current.reset(token)


def scores(ranked) -> None:
    """Tap an existing rank result; do not call rank again for diagnostics."""
    capture = _current.get()
    if capture is not None:
        for row, parts in ranked:
            tid = row['id']
            if tid in capture.ranked or len(capture.ranked) < capture.limit:
                capture.ranked[tid] = parts.total


def note(task_id, gate, reason, *, stage, status='ready') -> None:
    capture = _current.get()
    if capture is None or status != 'ready' or task_id in capture.records or task_id in capture.claimed_ids:
        return
    if len(capture.records) >= capture.limit:
        capture.dropped += 1
        return
    capture.records[task_id] = dict(task_id=task_id, gate=gate, reason=str(reason)[:400], stage=stage)


def claimed(task_id) -> None:
    capture = _current.get()
    if capture is not None:
        capture.records.pop(task_id, None)
        capture.ranked.pop(task_id, None)
        # Only candidates within the bounded observation window need suppression.
        if len(capture.claimed_ids) < capture.limit:
            capture.claimed_ids.add(task_id)


def pickup_facts(task_id, facts, *, status) -> None:
    """The first acquired refusing fact, not a guessed gate from English prose."""
    stages = (('already_local', 'prefilter'), ('default_placement', 'prefilter'),
              ('review_disabled', 'prefilter'), ('placement_refusal', 'labels'),
              ('missing_profile', 'prefilter'), ('capability_refusal', 'capability'),
              ('source_refusal', 'provenance'))
    for name, gate in stages:
        if name not in facts:
            continue
        value = facts[name]
        if (name in ('already_local', 'default_placement', 'review_disabled') and value is True) or (
                name not in ('already_local', 'default_placement', 'review_disabled') and value is not None):
            if name == 'already_local':
                return  # copied here already; local claim gates own its disposition
            if name == 'placement_refusal' and value != 'labels_mismatch':
                gate = 'capacity' if value == 'held' else 'prefilter'
            note(task_id, gate, f'{name}: {value}', stage='pickup', status=status)
            return


def pickup_skip(task_id, reason, *, status='ready') -> None:
    code, detail = reason
    if not code:
        return  # suppression/already-local is not a refusal
    gate = 'prefilter'
    if code in ('quota_hold',) or str(detail).startswith('llm_paced:'):
        gate = 'budget/pacing'
    elif code in ('budget_exhausted', 'hold'):
        gate = 'capacity'
    elif 'containment' in str(detail):
        gate = 'containment'
    elif code == 'guard_held':
        gate = 'prefilter'
    note(task_id, gate, f'{code}: {detail}', stage='pickup', status=status)


# Result buckets contain actual decisions; no hypothetical gate is re-evaluated.
_BUCKETS = (('skipped_nonspawnable', 'prefilter'), ('capsule_refused', 'capability'),
            ('no_progress_held', 'budget/pacing'), ('pinned_quota_held', 'budget/pacing'),
            ('llm_paced', 'budget/pacing'), ('skipped_per_profile_capped', 'capacity'),
            ('respawn_guarded', 'prefilter'), ('owner_down', 'prefilter'),
            ('quiet_deferred', 'capacity'), ('claim_deferred', 'prefilter'))


def lane_refused(task_id, result, *, status):
    for bucket, gate in _BUCKETS:
        for entry in getattr(result, bucket, ()):
            tid = entry if isinstance(entry, str) else entry[0]
            if tid != task_id:
                continue
            reason = bucket if isinstance(entry, str) else str(entry[1:])
            if bucket == 'claim_deferred':
                code = entry[1]
                gate = ('capacity' if code in ('capacity', 'lease_held') else
                        'labels' if code == 'labels_mismatch' else
                        'containment' if 'containment' in code else
                        'provenance' if 'source' in code or 'provenance' in code else
                        'capability' if 'capability' in code else 'prefilter')
            note(task_id, gate, f'{bucket}: {reason}', stage='claim', status=status)
            return
    note(task_id, 'unknown', 'claim path refused without a classified result', stage='claim', status=status)


def global_hold(conn, reason, *, gate='capacity'):
    """Bounded indexed ready-row census, not a scoring/graph pass."""
    if _current.get() is None:
        return
    for row in conn.execute("SELECT id FROM tasks WHERE status='ready' AND claim_lock IS NULL LIMIT ?",
                            (PER_PASS + 1,)):
        note(row[0], gate, reason, stage='global')


def capacity_tail(rows, lane_by_id, reason):
    from hermes_cli.kanban_governors import tail
    tail(rows, reason)  # the governor snapshot names the cap that set the budget
    for row in rows:
        note(row['id'], 'capacity', reason, stage='claim', status=lane_by_id.get(row['id'], 'ready'))


def persist(conn, capture):
    """One bounded write per pass. Local tables deliberately have no task FKs."""
    from hermes_cli.kanban_db_connect import write_txn
    with write_txn(conn):
        conn.execute(f'CREATE TABLE IF NOT EXISTS {PASSES} (pass_id TEXT PRIMARY KEY, worker TEXT NOT NULL, '
                     'pass_time_ms INTEGER NOT NULL, dropped INTEGER NOT NULL)')
        conn.execute(f'CREATE INDEX IF NOT EXISTS candidate_pass_time ON {PASSES}(pass_time_ms)')
        conn.execute(f'CREATE TABLE IF NOT EXISTS {TABLE} (id INTEGER PRIMARY KEY, pass_id TEXT NOT NULL, '
                     'worker TEXT NOT NULL, pass_time_ms INTEGER NOT NULL, task_id TEXT NOT NULL, '
                     'score REAL, score_status TEXT NOT NULL, gate TEXT NOT NULL, reason TEXT NOT NULL, '
                     'stage TEXT NOT NULL)')
        conn.execute(f'CREATE INDEX IF NOT EXISTS candidate_receipt_task ON {TABLE}(task_id, id)')
        conn.execute(f'CREATE INDEX IF NOT EXISTS candidate_receipt_time ON {TABLE}(pass_time_ms)')
        conn.execute(f'INSERT INTO {PASSES} VALUES (?, ?, ?, ?)',
                     (capture.pass_id, capture.worker, capture.pass_time_ms, capture.dropped))
        conn.executemany(f'INSERT INTO {TABLE} (pass_id, worker, pass_time_ms, task_id, score, score_status, '
                         'gate, reason, stage) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                         [(capture.pass_id, capture.worker, capture.pass_time_ms, tid,
                           capture.ranked.get(tid), 'observed' if tid in capture.ranked else 'not_observed',
                           row['gate'], row['reason'], row['stage']) for tid, row in capture.records.items()])
        cutoff = capture.pass_time_ms - RETENTION_MS
        for table in (TABLE, PASSES):
            conn.execute(f'DELETE FROM {table} WHERE pass_time_ms < ?', (cutoff,))
        conn.execute(f'DELETE FROM {TABLE} WHERE id IN (SELECT id FROM {TABLE} ORDER BY id DESC LIMIT -1 OFFSET ?)',
                     (MAX_ROWS,))
        conn.execute(f'DELETE FROM {PASSES} WHERE rowid IN '
                     f'(SELECT rowid FROM {PASSES} ORDER BY rowid DESC LIMIT -1 OFFSET ?)', (MAX_PASSES,))


def read(conn, *, task_id=None, limit=1000):
    """Phase B JSON-compatible read API; absence and truncation remain explicit."""
    out = dict(version=1, receipts=[], passes=[])
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table, key in ((TABLE, 'receipts'), (PASSES, 'passes')):
        if table not in tables:
            continue
        where, args = (' WHERE task_id = ?', [task_id]) if task_id is not None and table == TABLE else ('', [])
        cursor = conn.execute(f'SELECT * FROM {table}{where} ORDER BY rowid DESC LIMIT ?',
                              (*args, max(0, min(int(limit), MAX_ROWS))))
        columns = [d[0] for d in cursor.description]
        out[key] = [dict(zip(columns, row)) for row in cursor]
    return out


@contextmanager
def dispatch_pass(conn, *, dry_run=False, caps=None):
    """``caps``: the tick's concurrency settings, for the governor snapshot (``kanban_governors.tick``)."""
    import socket
    from hermes_cli.kanban_db_sync import sync_state
    from hermes_cli.kanban_governors import tick as governor_tick
    state = sync_state(conn)
    worker = str(state['gateway_id']) if state else socket.gethostname()
    with collect(worker=worker) as capture, governor_tick(conn, caps):
        yield
    if not dry_run:
        try:
            persist(conn, capture)
        except (sqlite3.Error, OSError):
            _LOG.warning('could not persist dispatcher candidate receipts', exc_info=True)


def main():
    """Read-only export for acceptance collectors; never tick the live dispatcher."""
    import argparse
    import json
    from pathlib import Path
    from hermes_cli.kanban_lockguard import connect
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True)
    parser.add_argument('--task-id')
    parser.add_argument('--limit', type=int, default=1000)
    args = parser.parse_args()
    conn = connect(Path(args.db).expanduser().resolve().as_uri() + '?mode=ro', uri=True)
    try:
        print(json.dumps(read(conn, task_id=args.task_id, limit=args.limit), sort_keys=True))
    finally:
        conn.close()


if __name__ == '__main__':
    main()
