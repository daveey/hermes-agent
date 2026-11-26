"""Send a card's park evidence ahead of the status change it explains. [daveey]

One writer transaction leaves as separate row operations: the status row first,
its event last. The Board applies each operation on its own and a replica applies
a feed prefix, so a reader could see ``scheduled`` with no park event yet (census:
``escalation_deadline_missing``), and the Board answered a bound status whose
origin event it had not seen with a merged row that rolled the writer back.

The capture transaction marks the status op and the ops that explain it with one
group id, the uid of the transaction's last event for the card
(``kanban_sync_outbox.evidence_uid``, local only, never sent). Selection then
promotes the group's other members ahead of the status op, as ``parent_first``
promotes an unacknowledged parent. No envelope, clock or FIFO timestamp is
rewritten, and nothing is inferred after the transaction.

Group members, all written after the status op in the same transaction, for the
same card: its events, a run row born there, and task patches that touch no
lifecycle column (a block's ``human_request``). Run ends and patches of
``status``/``current_run_uid``/``result``/``started_at``/``completed_at`` keep
their place: sending them early would show the old status half released.

Left on the legacy order: a card the Board has not acknowledged (its full row
must precede its children), terminal statuses (the Board's terminal ingress owns
that pair), a claim (its event names a run born in the transaction that is still
the card's current run; the lease order is untouched), and a transaction with
more than one status capture for the card.
"""
from __future__ import annotations

import json
import logging
import sqlite3

_log = logging.getLogger(__name__)
COLUMN = "evidence_uid"
_OPS = "kanban_evidence_ops"
_RUNS = "kanban_run_births"
_NOT_APPLYING = "COALESCE((SELECT applying FROM main.kanban_sync_state LIMIT 1), 0) = 0"
# Mirrors the Board's fenced task columns (hermes-hub worker/src/rows.ts FENCED), local names included.
_LIFECYCLE = ("status", "current_run_uid", "current_run_id", "result", "started_at", "completed_at")


def arm(conn: sqlite3.Connection) -> bool:
    """Record this connection's card ops and run births; the caller holds the write transaction."""
    from hermes_cli.kanban_sync_apply_schema import table_columns
    try:
        required = {"kanban_sync_outbox": {"op_id", "row_uid", "table_name", "op", "fields", COLUMN},
                    "task_runs": {"id", "uid", "task_id"}, "kanban_sync_state": {"applying"}}
        if any(not needed <= set(table_columns(conn, table)) for table, needed in required.items()):
            return False
        conn.execute(f"CREATE TEMP TABLE IF NOT EXISTS {_OPS} "
                     "(op_id TEXT PRIMARY KEY, tbl TEXT, task_id TEXT, uid TEXT, run TEXT)")
        conn.execute(f"DELETE FROM temp.{_OPS}")
        conn.execute(f"CREATE TEMP TABLE IF NOT EXISTS {_RUNS} (id INTEGER PRIMARY KEY)")
        conn.execute(f"DELETE FROM temp.{_RUNS}")
        conn.execute(
            "CREATE TEMP TRIGGER IF NOT EXISTS kanban_evidence_ops_trg AFTER INSERT ON main.kanban_sync_outbox "
            "WHEN NEW.op='upsert' AND NEW.table_name IN ('tasks','task_events','task_runs') BEGIN "
            f"INSERT OR REPLACE INTO {_OPS} VALUES (NEW.op_id, NEW.table_name, CASE NEW.table_name "
            "WHEN 'tasks' THEN NEW.row_uid "
            "WHEN 'task_runs' THEN (SELECT task_id FROM main.task_runs WHERE uid=NEW.row_uid) "
            "ELSE json_extract(NEW.fields,'$.task_id') END, NEW.row_uid, json_extract(NEW.fields,'$.run_uid')); END")
        conn.execute("CREATE TEMP TRIGGER IF NOT EXISTS kanban_run_birth_trg AFTER INSERT ON main.task_runs "
                     f"WHEN {_NOT_APPLYING} BEGIN INSERT OR IGNORE INTO {_RUNS} VALUES (NEW.id); END")
        return True
    except sqlite3.Error:  # never fail the writer's transaction for an ordering hint
        return False


def bind(conn: sqlite3.Connection, status_ops: list) -> None:
    """Before the outer COMMIT: mark each lone status op and the later ops of its card that explain it."""
    from hermes_cli.kanban_terminal_guard import TERMINAL_STATUSES
    try:
        if not conn.execute(f"SELECT 1 FROM temp.{_OPS} WHERE tbl='task_events' LIMIT 1").fetchone():
            return
        born = {r[0]: r[1] for r in conn.execute(
            f"SELECT r.uid, t.current_run_id=r.id FROM temp.{_RUNS} b JOIN task_runs r ON r.id=b.id "
            "LEFT JOIN tasks t ON t.id=r.task_id")}
        tasks = [task for _op, task in status_ops]
        for op, task in status_ops:
            if tasks.count(task) != 1:
                continue
            row = conn.execute(
                "SELECT o.rowid, json_extract(o.fields,'$.status'), t.sync_version FROM kanban_sync_outbox o "
                "JOIN tasks t ON t.id=o.row_uid WHERE o.op_id=? AND o.wire_json IS NULL", (op,)).fetchone()
            if not row or row[2] is None or row[1] in TERMINAL_STATUSES:
                continue
            later = conn.execute(
                f"SELECT c.op_id, c.tbl, c.uid, c.run, o.fields FROM temp.{_OPS} c "
                "JOIN kanban_sync_outbox o ON o.op_id=c.op_id WHERE c.task_id=? AND o.rowid>? ORDER BY o.rowid",
                (task, row[0])).fetchall()
            events = [r for r in later if r[1] == "task_events"]
            # A run born here that is still the card's current run is a claim: its lease order stays as it was.
            if not events or any(born.get(r[3]) for r in events):
                continue
            group = [op] + [r[0] for r in later if r[1] == "task_events" or (r[1] == "task_runs" and r[2] in born)
                            or (r[1] == "tasks" and not any(f'"{name}"' in (r[4] or "") for name in _LIFECYCLE))]
            conn.executemany(f"UPDATE kanban_sync_outbox SET {COLUMN}=? WHERE op_id=? AND wire_json IS NULL",
                             [(events[-1][2], member) for member in group])
    except sqlite3.Error:  # an unmigrated or unsynced file keeps the legacy order
        _log.warning("kanban evidence order unavailable; retaining FIFO order", exc_info=True)
    finally:
        try:
            conn.execute(f"DELETE FROM temp.{_OPS}")
            conn.execute(f"DELETE FROM temp.{_RUNS}")
        except sqlite3.Error:
            pass


def firsts(conn: sqlite3.Connection, row: dict, *, reassessment: bool = False) -> list[dict]:
    """Pending ops of this status op's group that go before it, in send order; [] keeps the legacy order.

    Once the named event is acknowledged, rejected or gone the status op travels alone.
    ``reassessment`` (Board advertises owner_reassessment_v1): only the exact bound certified park event
    is not promoted; the Board inserts and seals it atomically from the status op, so it must follow.
    """
    uid = row.get(COLUMN)
    fields = row.get("fields")
    if row.get("table_name") != "tasks" or not uid or not ("status" in fields if isinstance(fields, dict)
                                                         else '"status"' in (fields or "")):
        return []
    seen = conn.execute("SELECT sync_version FROM tasks WHERE id=?", (row["row_uid"],)).fetchone()
    if not seen or seen[0] is None:
        return []
    group = [dict(r) for r in conn.execute(
        f"SELECT o.* FROM kanban_sync_outbox o WHERE o.{COLUMN}=? AND o.state='pending' AND o.op_id!=? "
        "ORDER BY o.table_name='tasks', o.rowid", (uid, row["op_id"]))]
    if not any(r["table_name"] == "task_events" and r["row_uid"] == uid for r in group):
        return []
    frozen = row.get('wire_json')
    frozen_fields = json.loads(frozen).get('fields', {}) if frozen else {}
    if reassessment or 'owner_reassessment_v1' in frozen_fields:
        from hermes_cli.kanban_owner_reassessment_transport import bound_park_uid
        park = bound_park_uid(conn, row)
        if park is not None:
            group = [r for r in group if not (r["table_name"] == "task_events" and r["row_uid"] == park)]
    return group
