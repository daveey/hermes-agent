"""Durable age of a clean not-yet probe, independent of disposable probe leases.

Ready/todo/running/review are the wake → worker → re-park corridor. An episode
survives that corridor only for the identical probe; leaving it resets the wait.
The status trigger covers direct SQL and synced status writers as well as tools.
"""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from hermes_cli import kanban_wake_episode_barriers as barriers

STALE_EVENT = "wake_check_stale"
DEFAULT_STALE_HOURS = 48


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Installed after legacy table rebuilds; local-only, like task_wake_checks."""
    conn.execute("""CREATE TABLE IF NOT EXISTS task_wake_episodes (
        task_id TEXT PRIMARY KEY, cmd TEXT NOT NULL, first_at INTEGER NOT NULL,
        checks INTEGER NOT NULL, last_stale_at INTEGER)""")
    barriers.ensure_schema(conn)
    conn.execute("CREATE TABLE IF NOT EXISTS task_wake_episode_cmd (task_id TEXT PRIMARY KEY, cmd TEXT)")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS task_wake_episode_cmd_delete AFTER DELETE ON tasks
        BEGIN DELETE FROM task_wake_episode_cmd WHERE task_id = OLD.id; END""")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS task_wake_episode_reset
        AFTER UPDATE OF status ON tasks
        WHEN NEW.status NOT IN ('scheduled', 'todo', 'ready', 'running', 'review')
        BEGIN DELETE FROM task_wake_episodes WHERE task_id = NEW.id; END""")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS task_wake_episode_delete
        AFTER DELETE ON tasks
        BEGIN DELETE FROM task_wake_episodes WHERE task_id = OLD.id; END""")
    conn.execute("DELETE FROM task_wake_episodes WHERE task_id NOT IN (SELECT id FROM tasks)")
    conn.execute("""CREATE TABLE IF NOT EXISTS task_wake_episode_seen (
        task_id TEXT PRIMARY KEY, event_id INTEGER NOT NULL)""")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS task_wake_episode_seen_delete AFTER DELETE ON tasks
        BEGIN DELETE FROM task_wake_episode_seen WHERE task_id = OLD.id; END""")
    # One reset is carried per corridor exit; re-entering the corridor re-arms it.
    conn.execute("CREATE TABLE IF NOT EXISTS task_wake_episode_exit (task_id TEXT PRIMARY KEY)")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS task_wake_episode_exit_rearm
        AFTER UPDATE OF status ON tasks
        WHEN NEW.status IN ('scheduled', 'todo', 'ready', 'running', 'review')
        BEGIN DELETE FROM task_wake_episode_exit WHERE task_id = NEW.id; END""")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS task_wake_episode_exit_delete AFTER DELETE ON tasks
        BEGIN DELETE FROM task_wake_episode_exit WHERE task_id = OLD.id; END""")
    # Fresh local triage creation: the card's direct triage -> scheduled park. On an
    # UNSYNCED board no peer can have parked it on a probe first, so that park has nothing
    # to clear and adds no evidence. On a synced board a peer may promote/claim/park (or
    # directly park) the triage card while this home still sees triage, so the park
    # carries the reset inline in its own event (atomic park: still one event); durable
    # sync state is re-checked at the park and enabling sync clears every marker
    # (``forget_fresh``). Set only by the local ``created`` event of a triage card
    # (sync-imported rows never append it; a runnable card may reach a peer). Any other
    # status transition, a probe park, or the first probe-less park drops it.
    conn.execute("CREATE TABLE IF NOT EXISTS task_wake_episode_fresh (task_id TEXT PRIMARY KEY)")
    conn.execute("DROP TRIGGER IF EXISTS task_wake_episode_fresh_insert")
    conn.execute("DROP TRIGGER IF EXISTS task_wake_episode_fresh_entry")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS task_wake_episode_fresh_transition AFTER UPDATE OF status ON tasks
        WHEN NOT (OLD.status = 'triage' AND NEW.status = 'scheduled')
        BEGIN DELETE FROM task_wake_episode_fresh WHERE task_id = NEW.id; END""")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS task_wake_episode_fresh_delete AFTER DELETE ON tasks
        BEGIN DELETE FROM task_wake_episode_fresh WHERE task_id = OLD.id; END""")


_SNAPSHOT = "_wake_episode_v1"
_CARRY_EVENTS = frozenset({"scheduled", "continued", "continuation_woken", "wake_episode_started",
                           "wake_episode_reset", "condition_met"})
_CORRIDOR = frozenset({"scheduled", "todo", "ready", "running", "review"})
# Status-transition kinds whose own payload carries the corridor-exit reset.
_EXIT_EVENTS = frozenset({"blocked", "gave_up", "timed_out", "completed", "archived"})
EVIDENCE_EVENT = "wake_episode_evidence"


def consumed(conn: sqlite3.Connection, task_id: str, event_id: int) -> None:
    conn.execute("INSERT INTO task_wake_episode_seen VALUES (?, ?) ON CONFLICT(task_id) "
                 "DO UPDATE SET event_id = excluded.event_id", (task_id, event_id))


def reconcile(conn: sqlite3.Connection, task_id: str) -> None:
    """Consume portable snapshots once, using only this DB's event ids as a cursor."""
    seen = conn.execute("SELECT event_id FROM task_wake_episode_seen WHERE task_id = ?", (task_id,)).fetchone()
    rows = conn.execute("SELECT id, payload FROM task_events WHERE task_id = ? AND id > ? "
                        "AND payload LIKE ? ORDER BY id",
                        (task_id, seen[0] if seen else 0, '%"_wake_episode_v1"%')).fetchall()
    for row in rows:
        consumed(conn, task_id, row[0])
        _apply_snapshot(conn, task_id, row[1])


def _apply_snapshot(conn: sqlite3.Connection, task_id: str, raw: str) -> None:
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return
    if not isinstance(payload, dict) or _SNAPSHOT not in payload:
        return
    data = payload[_SNAPSHOT]
    context = payload.get(barriers.KEY, {})
    if not barriers.valid(context):
        return
    park = isinstance(data, dict) and set(data) == {"park_cmd"} and (
        data["park_cmd"] is None or isinstance(data["park_cmd"], str))
    if data is not None and not park and not _valid_snapshot(data):
        return
    if not barriers.accept(conn, task_id, context, empty=data is None):
        return
    if park:
        _remember_probe(conn, task_id, data["park_cmd"])
        return
    if data is not None:
        local = conn.execute("SELECT cmd, first_at, checks, last_stale_at FROM task_wake_episodes "
                             "WHERE task_id = ?", (task_id,)).fetchone()
        if local and local[0] == data["cmd"] and local[1] == data["first_at"]:
            data["checks"] = max(local[2], data["checks"])
            stamps = [stamp for stamp in (local[3], data["last_stale_at"]) if stamp is not None]
            data["last_stale_at"] = max(stamps) if stamps else None
    conn.execute("DELETE FROM task_wake_episodes WHERE task_id = ?", (task_id,))
    if data is not None:
        _remember_probe(conn, task_id, data["cmd"])
        conn.execute("INSERT INTO task_wake_episodes VALUES (?, ?, ?, ?, ?)",
                     (task_id, data["cmd"], data["first_at"], data["checks"], data["last_stale_at"]))


def _valid_snapshot(data: object) -> bool:
    return (isinstance(data, dict) and isinstance(data.get("cmd"), str) and bool(data["cmd"])
            and type(data.get("first_at")) is int and data["first_at"] >= 0
            and type(data.get("checks")) is int and data["checks"] > 0
            and "last_stale_at" in data
            and (data["last_stale_at"] is None or
                 (type(data["last_stale_at"]) is int and data["last_stale_at"] >= data["first_at"])))


def snapshot_event(conn: sqlite3.Connection, task_id: str, kind: str,
                   payload: dict | None) -> tuple[dict | None, dict | None]:
    """Return ``(payload, evidence)``; steady exit-1 checks do not append events.

    Lifecycle events carry the snapshot in their own payload. Unrelated events and
    probe-less parks keep their payload untouched: their evidence (a corridor-exit
    reset or a cleared probe, still needed by peers whose episode this home never
    observed) is returned separately for a ``wake_episode_evidence`` event. A
    probe-less park inside the corridor on a task with no delivered probe or
    episode adds nothing, so atomic parks still append only their own event.
    """
    status = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    outside = status is not None and status[0] not in _CORRIDOR
    if kind == "created" and status is not None and status[0] == "triage":  # local triage creation only
        conn.execute("INSERT OR IGNORE INTO task_wake_episode_fresh VALUES (?)", (task_id,))
    if kind in {"scheduled", "continued"} and (payload or {}).get("wake_check"):
        conn.execute("DELETE FROM task_wake_episode_fresh WHERE task_id = ?", (task_id,))  # probe park
    if not outside and kind not in _CARRY_EVENTS and not kind.startswith("wake_"):
        return payload, None
    if outside and conn.execute("SELECT 1 FROM task_wake_episode_exit WHERE task_id = ?", (task_id,)).fetchone() \
            and not conn.execute("SELECT 1 FROM task_wake_episodes WHERE task_id = ?", (task_id,)).fetchone():
        return payload, None  # this exit's reset was already carried
    probe_less = kind in {"scheduled", "continued"} and not (payload or {}).get("wake_check")
    if probe_less and not outside:
        reconcile(conn, task_id)  # judge against every delivered park, then idempotent in _snapshot
        fresh = conn.execute("DELETE FROM task_wake_episode_fresh WHERE task_id = ?", (task_id,)).rowcount > 0
        if fresh and not _probe_known(conn, task_id):
            # Direct park of a locally created triage card, no probe delivered. Unsynced:
            # nothing to clear, no evidence. Synced: a peer may have parked it first, so
            # carry the reset in the park's own payload (keeps atomic park cardinality).
            # Every later park may follow an undelivered foreign episode: reset below.
            if not _synced(conn):
                return payload, None
            reset(conn, task_id)
            return {**(payload or {}), **_snapshot(conn, task_id, kind, payload, outside)}, None
        reset(conn, task_id)  # home state may be unknown: make the clear an explicit reset barrier
    snapshot = _snapshot(conn, task_id, kind, payload, outside)
    separate = probe_less or (
        outside and kind not in _CARRY_EVENTS and kind not in _EXIT_EVENTS and not kind.startswith("wake_"))
    if separate:
        return payload, snapshot
    return {**(payload or {}), **snapshot}, None


def _snapshot(conn: sqlite3.Connection, task_id: str, kind: str, payload: dict | None, outside: bool) -> dict:
    reconcile(conn, task_id)
    if outside:
        reset(conn, task_id)
        conn.execute("INSERT OR IGNORE INTO task_wake_episode_exit VALUES (?)", (task_id,))
    elif kind in {"scheduled", "continued"}:
        cmd = (payload or {}).get("wake_check")
        _change_probe(conn, task_id, cmd)
    row = conn.execute("SELECT cmd, first_at, checks, last_stale_at FROM task_wake_episodes "
                       "WHERE task_id = ?", (task_id,)).fetchone()
    data = dict(zip(("cmd", "first_at", "checks", "last_stale_at"), row)) if row else None
    if data is None and kind in {"scheduled", "continued"}:
        data = {"park_cmd": (payload or {}).get("wake_check")}
    # A null snapshot only resets a peer when it carries an unseen reset barrier;
    # an unobserved episode on the home is otherwise unknown, not evidence of success.
    return {_SNAPSHOT: data, barriers.KEY: barriers.current(conn, task_id)}


def _synced(conn: sqlite3.Connection) -> bool:
    """Durable local sync state exists: the board was enabled for sync at some point (any
    mode, including a later detach), so peers may hold copies. Read from SQLite inside the
    caller's transaction, never from config, so a concurrent enable is serialized."""
    from hermes_cli import kanban_db_sync

    return kanban_db_sync.sync_state(conn) is not None


def forget_fresh(conn: sqlite3.Connection) -> None:
    """Drop every freshness marker; called in the transaction that enables sync (after
    ``ensure_schema``, which connect runs first)."""
    conn.execute("DELETE FROM task_wake_episode_fresh")


def _probe_known(conn: sqlite3.Connection, task_id: str) -> bool:
    row = conn.execute("SELECT cmd FROM task_wake_episode_cmd WHERE task_id = ?", (task_id,)).fetchone()
    return bool(row and row[0]) or conn.execute(
        "SELECT 1 FROM task_wake_episodes WHERE task_id = ?", (task_id,)).fetchone() is not None


def reset(conn: sqlite3.Connection, task_id: str) -> None:
    conn.execute("DELETE FROM task_wake_episodes WHERE task_id = ?", (task_id,))
    barriers.advance(conn, task_id)


def _remember_probe(conn: sqlite3.Connection, task_id: str, cmd: str | None) -> None:
    conn.execute("INSERT INTO task_wake_episode_cmd VALUES (?, ?) ON CONFLICT(task_id) "
                 "DO UPDATE SET cmd = excluded.cmd", (task_id, cmd))
    conn.execute("DELETE FROM task_wake_episodes WHERE task_id = ? AND cmd IS NOT ?", (task_id, cmd))


def _change_probe(conn: sqlite3.Connection, task_id: str, cmd: str | None) -> None:
    row = conn.execute("SELECT cmd FROM task_wake_episode_cmd WHERE task_id = ?", (task_id,)).fetchone()
    if row and row[0] != cmd:
        reset(conn, task_id)
    _remember_probe(conn, task_id, cmd)


def repark(conn: sqlite3.Connection, task_id: str, cmd: str | None) -> None:
    reconcile(conn, task_id)
    _change_probe(conn, task_id, cmd)


def stale_seconds() -> float:
    from hermes_cli.config_backend import ConfigBackendUnavailable
    from hermes_cli.config import load_config_readonly

    try:
        section = load_config_readonly().get("kanban")  # read-only: no per-tick deepcopy (G2)
    except (ConfigBackendUnavailable, OSError, ValueError):
        section = None
    value = section.get("wake_check_stale_hours") if isinstance(section, dict) else None
    try:
        hours = float(value)
    except (TypeError, ValueError):
        hours = DEFAULT_STALE_HOURS
    if not math.isfinite(hours) or hours <= 0:
        hours = DEFAULT_STALE_HOURS
    return hours * 3600


def stamp(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d %H:%M PT")


def not_yet(conn: sqlite3.Connection, task_id: str, cmd: str, now: int, interval: float,
            *, passive_policy=None) -> dict | None:
    """Count a validated clean exit 1 and return a due stale-wake payload."""
    repark(conn, task_id, cmd)
    conn.execute(
        "INSERT INTO task_wake_episodes(task_id, cmd, first_at, checks) VALUES (?, ?, ?, 1) "
        "ON CONFLICT(task_id) DO UPDATE SET checks = checks + 1", (task_id, cmd, now))
    first, checks, last = conn.execute(
        "SELECT first_at, checks, last_stale_at FROM task_wake_episodes WHERE task_id = ?", (task_id,)).fetchone()

    if checks == 1:
        from hermes_cli import kanban_db as kb
        kb._append_event(conn, task_id, "wake_episode_started")
    from hermes_cli import kanban_passive_intake as passive
    if passive.current(conn, task_id, cmd, policy=passive_policy):
        return None  # keep age/count, not a renewed stale-wake lease
    if now - first < interval or (last is not None and now - last < interval):
        return None
    conn.execute("UPDATE task_wake_episodes SET last_stale_at = ? WHERE task_id = ?", (now, task_id))
    note = (f"wake_check has not passed in {(now - first) / 3600:g} h "
            f"({checks} checks since {stamp(first)}): check that its condition can occur, "
            "then change the wait or the acceptance. Re-parking on the identical probe keeps this episode.")
    return {"wake_check": cmd, "first_at": first, "checks": checks, "last_stale_at": now, "note": note}
