"""Durable age of a clean not-yet probe, independent of disposable probe leases.

Ready/todo/running/review are the wake → worker → re-park corridor. An episode
survives that corridor only for the identical probe; leaving it resets the wait.
The status trigger covers direct SQL and synced status writers as well as tools.
"""
from __future__ import annotations

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


def reset(conn: sqlite3.Connection, task_id: str) -> None:
    conn.execute("DELETE FROM task_wake_episodes WHERE task_id = ?", (task_id,))


def repark(conn: sqlite3.Connection, task_id: str, cmd: str | None) -> None:
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

    if now - first < interval or (last is not None and now - last < interval):
        return None
    conn.execute("UPDATE task_wake_episodes SET last_stale_at = ? WHERE task_id = ?", (now, task_id))
    note = (f"wake_check has not passed in {(now - first) / 3600:g} h "
            f"({checks} checks since {stamp(first)}): check that its condition can occur, "
            "then change the wait or the acceptance. Re-parking on the identical probe keeps this episode.")
    return {"wake_check": cmd, "first_at": first, "checks": checks, "last_stale_at": now, "note": note}
