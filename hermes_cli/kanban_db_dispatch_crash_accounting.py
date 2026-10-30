"""``_account_crashes``: the dead-worker sweep's breaker accounting (moved out of kanban_db_dispatch). [daveey]

Dispatch-module helpers are read through the module at call time, so tests that patch
``kanban_db_dispatch._record_task_failure`` (and friends) still steer it.
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from hermes_cli import kanban_systemic_crash as _systemic


def _account_crashes(conn: sqlite3.Connection, crash_details: list) -> list[str]:
    """Count each crash against the breaker; returns the task ids it tripped.

    Protocol violations get a BOUNDED violation-only budget independent of
    ``consecutive_failures`` (per-task ``max_retries`` takes precedence);
    systemic same-error crashes (>= 3 identical fingerprints this tick) and
    terminal provider errors (credential revoked, model gone — a retry cannot
    heal them) trip immediately.
    """
    from hermes_cli import kanban_db_dispatch as _d
    auto_blocked: list[str] = []
    fp_counts: dict[str, int] = {}
    fp_tasks: dict[str, list[str]] = {}
    for tid, _, _, dead in crash_details:
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
            streak = _d._protocol_violation_streak(conn, tid)
            trow = conn.execute("SELECT max_retries FROM tasks WHERE id = ?", (tid,)).fetchone()
            if trow is None:
                continue  # task deleted mid-loop
            task_override = _d._kb._row_get(trow, "max_retries")
            violation_limit = (
                int(task_override) if task_override is not None else _d._PROTOCOL_VIOLATION_FAILURE_LIMIT
            )
            if streak < violation_limit:
                # Below budget: already back at ``ready`` with the error stamped.
                # No ``_record_task_failure`` — must not consume the unified budget.
                continue
            # ``force_trip``: the decision (incl. per-task ``max_retries``) was
            # already made against the violation streak above.
            tripped = _d._record_task_failure(
                conn, tid,
                error=error_text,
                outcome="crashed",
                failure_limit=violation_limit,
                force_trip=True,
                release_claim=False,
                end_run=False,
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
                conn, tid,
                error=error_text,
                outcome="crashed",
                force_trip=True,
                release_claim=False,
                end_run=False,
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
                conn, tid,
                error=error_text,
                outcome="crashed",
                failure_limit=1 if is_systemic else None,
                release_claim=False,
                end_run=False,
                event_payload_extra=extra,
            )
            if tripped and fix_id and not _systemic.attach(conn, fix_id, tid):
                with _d._kb.write_txn(conn):  # unlinked: never let recompute_ready respawn it into the same wall
                    _d._kb._append_event(conn, tid, "gave_up", {**extra, "sticky": True, "fix_card_link_failed": True})
        if tripped:
            auto_blocked.append(tid)
    return auto_blocked
