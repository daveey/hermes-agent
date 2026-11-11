"""Field-scoped repair of uncaptured local writes."""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from hermes_cli import kanban_db_sync as sync

_log = logging.getLogger(__name__)


def repair_divergence(live: sqlite3.Connection, shadow: sqlite3.Connection, gateway: str) -> dict[str, list[str]]:
    """Queue the local values of this gateway's cards that the service never received.

    A card whose live ``sync_version`` equals the replica's, with no outbox op in flight, has seen every service
    write. Any synced field that still differs was then written locally with no capture trigger, e.g. in the
    window while another process re-installed the triggers. The install heal only finds NULL-uid children;
    ``repair_missing_children`` handles settled missing links. A task row has no missing-row marker, so this
    field comparison is the only place that finds its uncaptured edits.
    Hub incident 2026-10-28: a ``scheduled -> ready`` wake reached the service as its event but not as its
    status, and the service refused every lease as ``not_claimable`` for 3 h. Returns {task id: fields queued}."""
    from hermes_cli.kanban_sync_client import _row_fields, _diverged_runs
    from hermes_cli.kanban_db_connect import write_txn
    from hermes_cli.kanban_sync_omissions import held_fields, repair_holds
    held = repair_holds(live, "tasks")
    seen = {r[0]: r[1] for r in shadow.execute(
        "SELECT id, sync_version FROM tasks WHERE (created_on = ? OR created_on IS NULL) AND sync_version IS NOT NULL",
        (gateway,))}
    repaired: dict[str, list[str]] = {}
    rows = live.execute("SELECT * FROM tasks WHERE sync_version IS NOT NULL").fetchall()
    for row in rows:
        tid = row["id"]
        if tid in held or seen.get(tid) != row["sync_version"]:
            continue
        theirs = shadow.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
        mine, remote = _row_fields(live, "tasks", row), _row_fields(shadow, "tasks", theirs)
        omitted = held_fields(live, "tasks", tid)
        fields = {k: v for k, v in mine.items() if k in remote and remote[k] != v and k not in omitted}
        if not fields:
            continue
        runs = _diverged_runs(live, shadow, tid)
        with write_txn(live):
            # [daveey] The same uncaptured transaction usually ended the card's run too (``complete_task`` writes
            # both). Queue those run rows FIRST: a card arriving ``done`` before its run makes the Board end the
            # lease's run generically (rows.ts ``endLeaseRun``: ``released``, "card left running (done)"), and
            # with no later holder write that row came back over the real ``done/completed`` run. The owner then
            # published the card unverified, and no origin ever released it (hub t_01a185075c59bd52, 10-29).
            for run_uid, run_fields, run_version in runs:
                live.execute(
                    "INSERT INTO kanban_sync_outbox (op_id, table_name, row_uid, op, fields, base_version, hlc, "
                    "created_at) VALUES (?, 'task_runs', ?, 'upsert', ?, ?, '', ?)",
                    (sync.uuid7(), run_uid, json.dumps(run_fields), None, int(time.time() * 1000)))
            live.execute(
                "INSERT INTO kanban_sync_outbox (op_id, table_name, row_uid, op, fields, base_version, hlc, created_at) "
                "VALUES (?, 'tasks', ?, 'upsert', ?, ?, '', ?)",
                (sync.uuid7(), tid, json.dumps(fields), row["sync_version"], int(time.time() * 1000)))
        repaired[tid] = sorted(fields) + [f"task_runs:{uid}" for uid, _f, _v in runs]
        _log.warning("kanban sync: %s %s differed from the service with nothing in flight (an uncaptured write); "
                     "re-queued", tid, ", ".join(repaired[tid]))
    return repaired

