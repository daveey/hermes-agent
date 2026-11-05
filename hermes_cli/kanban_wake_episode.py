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

STALE_EVENT = "wake_check_stale"
DEFAULT_STALE_HOURS = 48


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Installed after legacy table rebuilds; local-only, like task_wake_checks."""
    conn.execute("""CREATE TABLE IF NOT EXISTS task_wake_episodes (
        task_id TEXT PRIMARY KEY, cmd TEXT NOT NULL, first_at INTEGER NOT NULL,
        checks INTEGER NOT NULL, last_stale_at INTEGER)""")
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


_SNAPSHOT = "_wake_episode_v1"
_CARRY_EVENTS = frozenset({"scheduled", "continued", "continuation_woken", "wake_episode_started",
                           "wake_episode_reset", "condition_met"})
_CORRIDOR = frozenset({"scheduled", "todo", "ready", "running", "review"})


def consumed(conn: sqlite3.Connection, task_id: str, event_id: int) -> None:
    conn.execute("INSERT INTO task_wake_episode_seen VALUES (?, ?) ON CONFLICT(task_id) "
                 "DO UPDATE SET event_id = excluded.event_id", (task_id, event_id))


def reconcile(conn: sqlite3.Connection, task_id: str) -> None:
    """Consume portable snapshots once, using only this DB's event ids as a cursor."""
    seen = conn.execute("SELECT event_id FROM task_wake_episode_seen WHERE task_id = ?", (task_id,)).fetchone()
    row = conn.execute("SELECT id, payload FROM task_events WHERE task_id = ? AND id > ? "
                       "AND payload LIKE ? ORDER BY id DESC LIMIT 1",
                       (task_id, seen[0] if seen else 0, '%"_wake_episode_v1"%')).fetchone()
    if row is None:
        return
    consumed(conn, task_id, row[0])
    try:
        payload = json.loads(row[1])
    except (ValueError, TypeError):
        return
    if not isinstance(payload, dict) or _SNAPSHOT not in payload:
        return
    data = payload[_SNAPSHOT]
    if data is not None and not _valid_snapshot(data):
        return
    if data is not None:
        local = conn.execute("SELECT cmd, first_at, checks, last_stale_at FROM task_wake_episodes "
                             "WHERE task_id = ?", (task_id,)).fetchone()
        if local and local[0] == data["cmd"] and local[1] == data["first_at"]:
            data["checks"] = max(local[2], data["checks"])
            stamps = [stamp for stamp in (local[3], data["last_stale_at"]) if stamp is not None]
            data["last_stale_at"] = max(stamps) if stamps else None
    reset(conn, task_id)
    if data is not None:
        conn.execute("INSERT INTO task_wake_episodes VALUES (?, ?, ?, ?, ?)",
                     (task_id, data["cmd"], data["first_at"], data["checks"], data["last_stale_at"]))


def _valid_snapshot(data: object) -> bool:
    return (isinstance(data, dict) and isinstance(data.get("cmd"), str) and bool(data["cmd"])
            and type(data.get("first_at")) is int and data["first_at"] >= 0
            and type(data.get("checks")) is int and data["checks"] > 0
            and "last_stale_at" in data
            and (data["last_stale_at"] is None or
                 (type(data["last_stale_at"]) is int and data["last_stale_at"] >= data["first_at"])))


def snapshot_event(conn: sqlite3.Connection, task_id: str, kind: str, payload: dict | None) -> dict | None:
    """Carry at existing lifecycle/remote-alive events, never append a per-check journal."""
    status = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    outside = status is not None and status[0] not in _CORRIDOR
    if not outside and kind not in _CARRY_EVENTS and not kind.startswith("wake_"):
        return payload
    reconcile(conn, task_id)
    if outside:
        reset(conn, task_id)
    elif kind in {"scheduled", "continued"}:
        cmd = (payload or {}).get("wake_check")
        conn.execute("DELETE FROM task_wake_episodes WHERE task_id = ? AND cmd IS NOT ?", (task_id, cmd))
    row = conn.execute("SELECT cmd, first_at, checks, last_stale_at FROM task_wake_episodes "
                       "WHERE task_id = ?", (task_id,)).fetchone()
    data = dict(zip(("cmd", "first_at", "checks", "last_stale_at"), row)) if row else None
    return {**(payload or {}), _SNAPSHOT: data}


def reset(conn: sqlite3.Connection, task_id: str) -> None:
    conn.execute("DELETE FROM task_wake_episodes WHERE task_id = ?", (task_id,))


def repark(conn: sqlite3.Connection, task_id: str, cmd: str | None) -> None:
    reconcile(conn, task_id)
    conn.execute("DELETE FROM task_wake_episodes WHERE task_id = ? AND cmd IS NOT ?", (task_id, cmd))


def stale_seconds() -> float:
    from hermes_cli.config_backend import ConfigBackendUnavailable
    from hermes_cli.config import load_config

    try:
        section = load_config().get("kanban")
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


def not_yet(conn: sqlite3.Connection, task_id: str, cmd: str, now: int, interval: float) -> dict | None:
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
    if now - first < interval or (last is not None and now - last < interval):
        return None
    conn.execute("UPDATE task_wake_episodes SET last_stale_at = ? WHERE task_id = ?", (now, task_id))
    note = (f"wake_check has not passed in {(now - first) / 3600:g} h "
            f"({checks} checks since {stamp(first)}): check that its condition can occur, "
            "then change the wait or the acceptance. Re-parking on the identical probe keeps this episode.")
    return {"wake_check": cmd, "first_at": first, "checks": checks, "last_stale_at": now, "note": note}
