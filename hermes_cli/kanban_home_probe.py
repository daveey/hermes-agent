"""Probe execution stays with its filing gateway across an administrative move.

This is routing evidence, never permission to run or release a held card. The
capture epoch is mirrored only by authenticated feed/source finalization; local
manifest receipts are installed only by the authenticated handoff caller.
"""
from __future__ import annotations

import json
import sqlite3

UNKNOWN = "home-probe-custody-unknown"


def host(conn: sqlite3.Connection, task_id: str, event_id: int, explicit: str | None) -> str | None:
    """Preserve explicit custody, or recover the exact hostless event's old host.

    Ordinary unmoved boards retain legacy behavior. During a transferred task's
    early feed (before full destination import), an unknown event origin refuses
    restoration instead of guessing the new home. A later filing cannot borrow
    the former source from an unrelated historical event.
    """
    if explicit:
        return explicit
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN "
              "('kanban_home_generations1','kanban_home_materialized1','kanban_home_finalized1')")}
    if "kanban_home_generations1" not in tables or not conn.execute(
            "SELECT 1 FROM kanban_home_generations1 WHERE task_id=?", (task_id,)).fetchone():
        return None
    event = conn.execute("SELECT uid,origin_gateway FROM task_events WHERE task_id=? AND id=?", (task_id,event_id)).fetchone()
    if event is None:
        return UNKNOWN
    if event[1]:
        return str(event[1])  # authenticated remote origin, or the native filing gateway
    for table, column in (("kanban_home_materialized1", "manifest"), ("kanban_home_finalized1", "receipt")):
        if table not in tables:
            continue
        row = conn.execute(f"SELECT {column} FROM {table} WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            continue
        try:
            record = json.loads(row[0])
            if (record["task"] == task_id and isinstance(record["source"], str)
                    and any(r["table"] == "task_events" and r["uid"] == event[0] for r in record["rows"])):
                return record["source"]
        except (ValueError, TypeError, KeyError):
            continue
    return UNKNOWN


def remote_baseline(conn, task_id, event_id, source, explicit, now):
    """Start a bounded, durable liveness window for a formerly local probe.

    The old local probe had no obligation to publish remote ALIVE events. Only
    the exact hostless park in this epoch's authenticated destination manifest
    gets a new observation baseline. Repeated sweeps/restarts never renew it;
    ordinary explicit remote probes retain their existing silence accounting.
    This is not an ALIVE attestation and never grants readiness or execution.
    """
    from hermes_cli import kanban_db_connect as dbc
    from hermes_cli.kanban_home_transfer import frozen

    if explicit:
        return 0
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'kanban_home_materialized1', 'kanban_home_generations1'} <= tables:
        return 0
    row = conn.execute('SELECT m.manifest,g.nonce FROM kanban_home_materialized1 m '
                       'JOIN kanban_home_generations1 g ON g.task_id=m.task_id WHERE m.task_id=?',
                       (task_id,)).fetchone()
    event = conn.execute('SELECT uid FROM task_events WHERE task_id=? AND id=?', (task_id, event_id)).fetchone()
    if row is None or event is None:
        return 0
    try:
        record = json.loads(row[0])
        matches = (record['task'] == task_id and record['source'] == source and record['nonce'] == row[1]
                   and any(r['table'] == 'task_events' and r['uid'] == event[0] for r in record['rows']))
    except (ValueError, TypeError, KeyError):
        return 0
    if not matches:
        return 0
    key = (task_id, event[0], row[1])
    read = 'SELECT first_at FROM kanban_home_probe_watch1 WHERE task_id=? AND event_uid=? AND nonce=?'
    if 'kanban_home_probe_watch1' in tables:
        saved = conn.execute(read, key).fetchone()
        if saved is not None:
            return saved[0]
    with dbc.write_txn(conn):
        if frozen(conn, task_id):
            return now  # concurrent preparation still owns custody; no probe writes
        conn.execute('CREATE TABLE IF NOT EXISTS kanban_home_probe_watch1 '
                     '(task_id TEXT NOT NULL,event_uid TEXT NOT NULL,nonce TEXT NOT NULL,first_at INTEGER NOT NULL,'
                     'PRIMARY KEY(task_id,event_uid,nonce))')
        conn.execute('INSERT OR IGNORE INTO kanban_home_probe_watch1 VALUES (?,?,?,?)', (*key, now))
        return conn.execute(read, key).fetchone()[0]


def remote_last(conn, task_id, event_id, source, explicit, now):
    from hermes_cli.kanban_wake_check import ALIVE_EVENT

    last = conn.execute('SELECT MAX(created_at) FROM task_events WHERE task_id=? AND id>? AND kind=?',
                        (task_id, event_id, ALIVE_EVENT)).fetchone()[0]
    return max(int(last or 0), remote_baseline(conn, task_id, event_id, source, explicit, now))


def claim_wake(conn, task_id, cmd, every, generation, purpose, status, ts, claim):
    """Check handoff custody under the same writer lock as the probe claim."""
    from hermes_cli import kanban_db_connect as dbc
    from hermes_cli.kanban_home_transfer import frozen

    with dbc.write_txn(conn):
        if frozen(conn, task_id):
            return False
        return conn.execute(
            "UPDATE task_wake_checks SET next_at = ?, generation = ? "
            "WHERE task_id = ? AND cmd = ? AND generation IS ? AND next_at <= ? AND purpose IS ? "
            "AND EXISTS (SELECT 1 FROM tasks WHERE id = ? AND status = ?)",
            (ts + int(every), claim, task_id, cmd, generation, ts, purpose, task_id, status)).rowcount == 1


def require_source_probe(conn: sqlite3.Connection, task_id: str, source: str) -> None:
    """Refuse preparation until an active logical source probe is registered.

    Run ordinary restoration before preparing, then drain its published events.
    Never reset an exhausted probe or restore inside the transfer transaction.
    Explicit foreign-host custody stays foreign.
    """
    from hermes_cli import kanban_ask_resolved as ask, kanban_wake_check as wake
    from hermes_cli import kanban_continuation as continuation

    task = conn.execute("SELECT status,human_request FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not task or task[0] not in ("scheduled", "blocked"):
        return
    purpose = ask.PURPOSE if task[0] == "blocked" else None
    kinds = ("blocked",) if purpose else continuation.SCHEDULED_ENTRY_EVENTS
    marks = ",".join("?" for _ in kinds)
    event = conn.execute(f"SELECT id,payload FROM task_events WHERE task_id=? AND kind IN ({marks}) "
                         "ORDER BY id DESC LIMIT 1", (task_id,*kinds)).fetchone()
    payload = json.loads(event[1]) if event else {}
    cmd = ask.resolved_check(task[1])[0] if purpose else payload.get("wake_check")
    if not cmd:
        return
    probe_host = host(conn, task_id, event[0], payload.get("probe_host")) if event else None
    if probe_host and probe_host != source:
        if probe_host == UNKNOWN:
            raise ValueError("source probe host is unknown")
        return
    state = wake.get_state(conn, task_id)
    if state is None or state["cmd"] != cmd or (state.get("purpose") == ask.PURPOSE) != bool(purpose):
        raise ValueError("source probe must be restored and drained before preparation")
