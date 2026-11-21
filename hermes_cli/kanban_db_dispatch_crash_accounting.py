"""``_account_crashes``: the dead-worker sweep's breaker accounting (moved out of kanban_db_dispatch). [daveey]

Dispatch-module helpers are read through the module at call time, so tests that patch
``kanban_db_dispatch._record_task_failure`` (and friends) still steer it.
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from hermes_cli import kanban_systemic_crash as _systemic


def _account_crashes(conn: sqlite3.Connection, crash_details: list, *, allow_nested: bool = False) -> list[str]:
    """Count each crash against the breaker; returns the task ids it tripped.

    Protocol violations get a BOUNDED violation-only budget independent of
    ``consecutive_failures`` (per-task ``max_retries`` takes precedence);
    systemic same-error crashes (>= 3 identical fingerprints this tick) and
    terminal provider errors (credential revoked, model gone — a retry cannot
    heal them) trip immediately.
    """
    from hermes_cli import kanban_db_dispatch as _d
    from hermes_cli import kanban_failure_summary as summary
    auto_blocked: list[str] = []
    fp_counts: dict[str, int] = {}
    fp_tasks: dict[str, list[str]] = {}
    for tid, _, _, dead in crash_details:
        if summary.classify_failure("crashed", {"error": dead.error_text, **dead.event_payload}) == "provider_refusal":
            continue  # A refusal is not a fleet crash and must not spawn an automatic fix card.
        if dead.event_payload.get("owner_death_pid") is not None:
            continue  # an owner-death repeat (crash-loop guard) is charged normally, never systemic [daveey]
        fp = _d._error_fingerprint(dead.error_text)
        fp_counts[fp] = fp_counts.get(fp, 0) + 1
        if not (dead.protocol_violation or dead.terminal_provider or dead.unknown_skill or dead.stdlib_shadow
                or dead.reboot_culprit):
            fp_tasks.setdefault(fp, []).append(tid)
    # One visible owner per systemic fingerprint: a fix card the tripped cards wait on. [daveey]
    fix_cards: dict[str, Optional[str]] = {}
    for fp, tids in fp_tasks.items():
        if fp_counts.get(fp, 0) >= 3:
            sample = next(d.error_text for t, _, _, d in crash_details if t == tids[0])
            arow = conn.execute("SELECT assignee FROM tasks WHERE id = ?", (tids[0],)).fetchone()
            fix_cards[fp] = _systemic.ensure_fix_card(conn, fp, tids, sample, arow["assignee"] if arow else None)
    for tid, pid, claimer, dead in crash_details:
        error_text = dead.error_text
        if dead.protocol_violation:
            from hermes_cli.kanban_failure_release import queue
            if queue(conn, tid, dead.run_id, outcome="crashed", error=error_text,
                     protocol_violation=True, failure_limit=_d._PROTOCOL_VIOLATION_FAILURE_LIMIT,
                     diagnostic=dict(dead.event_payload)):
                continue  # Board computes the streak from authoritative closed runs.
            streak = _d._protocol_violation_streak(conn, tid)
            trow = conn.execute("SELECT max_retries FROM tasks WHERE id = ?", (tid,)).fetchone()
            if trow is None:
                continue  # task deleted mid-loop
            task_override = _d._kb._row_get(trow, "max_retries")
            violation_limit = (
                int(task_override) if task_override is not None else _d._PROTOCOL_VIOLATION_FAILURE_LIMIT
            )
            if streak < violation_limit:
                # Finalize the exempt run too, so a late reclaim cannot charge it.
                from hermes_cli import kanban_local_failure as local
                if local.admit(conn, tid, dead.run_id):
                    failures = conn.execute("SELECT consecutive_failures FROM tasks WHERE id=?", (tid,)).fetchone()[0]
                    local.finalized(conn, tid, dead.run_id, failures=failures, effective_limit=violation_limit,
                                    error=error_text, trigger_outcome="crashed", protocol_violations=streak,
                                    protocol_violation_limit=violation_limit, tripped=False)
                continue
            # ``force_trip``: the decision (incl. per-task ``max_retries``) was
            # already made against the violation streak above.
            tripped = _d._record_task_failure(
                conn, tid, run_id=dead.run_id, diagnostic=dict(dead.event_payload),
                error=error_text,
                outcome="crashed",
                failure_limit=violation_limit,
                force_trip=True,
                release_claim=False,
                end_run=False,
                allow_nested=allow_nested,
                event_payload_extra={
                    "pid": pid,
                    "claimer": claimer,
                    "protocol_violations": streak,
                    "protocol_violation_limit": violation_limit,
                },
            )
        elif dead.terminal_provider or dead.unknown_skill or dead.stdlib_shadow or dead.reboot_culprit:
            # No retry heals a revoked credential, a gone model, a missing skill, a stdlib shadow or this
            # card's own sudo shutdown of the host (a retry reboots it again): block now.
            flag = "unknown_skill" if dead.unknown_skill else "stdlib_shadow" if dead.stdlib_shadow else \
                "reboot_culprit" if dead.reboot_culprit else "terminal_provider"
            tripped = _d._record_task_failure(
                conn, tid, run_id=dead.run_id, diagnostic=dict(dead.event_payload),
                error=error_text,
                outcome="crashed",
                force_trip=True,
                release_claim=False,
                end_run=False,
                allow_nested=allow_nested,
                event_payload_extra={"pid": pid, "claimer": claimer, flag: dead.reboot_culprit or True},
                block_kind="transient" if dead.terminal_provider else "capability" if dead.reboot_culprit else "assignment",
            )
            if dead.reboot_culprit:
                _d._host_reboot.comment_culprit(conn, tid, dead.reboot_culprit)
        else:
            fp = _d._error_fingerprint(error_text)
            is_systemic = dead.event_payload.get("owner_death_pid") is None and fp_counts.get(fp, 0) >= 3
            fix_id = fix_cards.get(fp) if is_systemic else None
            if fix_id == tid:
                fix_id = None  # the fix card itself died of it: hold it for an operator
            extra = {"pid": pid, "claimer": claimer}
            if fix_id:
                # Trips at 1 and waits on the fingerprint's fix card (linked below), not a silent hold.
                extra["fix_card"] = fix_id
            elif is_systemic:
                # No fix card could be opened: trips at 1, held for an operator.
                extra["sticky"] = True
            tripped = _d._record_task_failure(
                conn, tid, run_id=dead.run_id, diagnostic=dict(dead.event_payload),
                error=error_text,
                outcome="crashed",
                failure_limit=1 if is_systemic else None,
                release_claim=False,
                end_run=False,
                allow_nested=allow_nested,
                event_payload_extra=extra,
            )
        if tripped:
            auto_blocked.append(tid)
    return auto_blocked
