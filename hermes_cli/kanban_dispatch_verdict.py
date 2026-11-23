"""Read-only diagnostics for a contended tick; never an admission decision. [daveey]"""
from __future__ import annotations

import sqlite3
import time


def claim_refused(result, task_id, deferral) -> None:
    """Classify an unacquired claim without duplicating a claim-quiet or capsule refusal."""
    if deferral is not None:
        result.claim_deferred.append((task_id, deferral))
    elif not any(tid == task_id for tid, _ in result.quiet_deferred) and not any(
            tid == task_id for tid, _ in result.capsule_refused):  # record_claim_refusal already reported it
        result.claim_deferred.append((task_id, 'claim_not_acquired'))


def print_historical(diagnostic) -> None:
    """Render optional historical evidence separately from live admission."""
    import json
    if diagnostic:
        print('Historical dispatch evidence: ' + json.dumps(diagnostic, sort_keys=True))


def locked(conn) -> dict:
    """Expose the last *completed* pass, explicitly historical and bounded.

    Observer connections opened during tick contention may read evidence, but
    must not run policy, acquire a lease, checkpoint WAL or invoke tick hooks.
    Reading both tables in one SELECT pins the pass and its rows together.
    """
    out = dict(reason='dispatch_lock_contended', historical=True,
               observed_at_ms=int(time.time() * 1000), source='unavailable',
               last_pass_unavailable='no_completed_pass', receipts=[])
    from hermes_cli.kanban_candidate_receipts import PASSES, TABLE, PER_PASS
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (PASSES,)).fetchone():
            return out
        rows = conn.execute(
            f'SELECT p.pass_id, p.worker, p.pass_time_ms, p.dropped, r.task_id, r.gate, r.reason, r.stage '
            f'FROM {PASSES} p LEFT JOIN {TABLE} r ON r.pass_id=p.pass_id '
            f'WHERE p.rowid=(SELECT MAX(rowid) FROM {PASSES}) ORDER BY r.id LIMIT ?', (PER_PASS,)).fetchall()
        if not rows:
            return out
        first = rows[0]
        out.update(source='last_completed_pass', pass_id=first[0], worker=first[1],
                   pass_time_ms=first[2], dropped=first[3])
        out.pop('last_pass_unavailable')
        out['receipts'] = [dict(task_id=r[4], gate=r[5], reason=r[6], stage=r[7]) for r in rows if r[4]]
        return out
    except sqlite3.Error as exc:
        out['last_pass_unavailable'] = 'evidence_read_failed'
        out['error_type'] = type(exc).__name__
        return out
