"""Reset causality for probe episodes transported over unordered gateway feeds.

A per-store sequence is a reset barrier, not a wall clock or local event id.
An episode must acknowledge every known reset before its state can be adopted.
Only reset writers occupy vector entries; steady checks do not grow the vector.
"""
from __future__ import annotations

import sqlite3
import uuid

KEY = "_wake_episode_barriers"


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS wake_episode_identity (id INTEGER PRIMARY KEY, source TEXT NOT NULL)")
    conn.execute("INSERT OR IGNORE INTO wake_episode_identity VALUES (1, ?)", (str(uuid.uuid4()),))
    conn.execute("""CREATE TABLE IF NOT EXISTS task_wake_episode_barriers (
        task_id TEXT NOT NULL, source TEXT NOT NULL, sequence INTEGER NOT NULL,
        PRIMARY KEY (task_id, source))""")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS task_wake_episode_barriers_delete AFTER DELETE ON tasks
        BEGIN DELETE FROM task_wake_episode_barriers WHERE task_id = OLD.id; END""")


def current(conn: sqlite3.Connection, task_id: str) -> dict:
    return dict(conn.execute("SELECT source, sequence FROM task_wake_episode_barriers WHERE task_id = ?", (task_id,)))


def advance(conn: sqlite3.Connection, task_id: str) -> None:
    conn.execute("""INSERT INTO task_wake_episode_barriers(task_id, source, sequence)
        SELECT ?, source, 1 FROM wake_episode_identity WHERE id = 1
        ON CONFLICT(task_id, source) DO UPDATE SET sequence = sequence + 1""", (task_id,))


def valid(value: object) -> bool:
    return isinstance(value, dict) and all(
        isinstance(source, str) and bool(source) and type(seq) is int and seq > 0
        for source, seq in value.items())


def accept(conn: sqlite3.Connection, task_id: str, remote: dict, *, empty: bool) -> bool:
    """Join reset knowledge; never resurrect state concurrent with a new reset.

    A duplicate reset receipt must not clear an episode begun after that reset.
    Concurrent resets join, clear both episodes, and require a fresh observation.
    """
    local = current(conn, task_id)
    new_reset = any(seq > local.get(source, 0) for source, seq in remote.items())
    missing_reset = any(seq > remote.get(source, 0) for source, seq in local.items())
    for source, seq in remote.items():
        conn.execute("""INSERT INTO task_wake_episode_barriers VALUES (?, ?, ?)
            ON CONFLICT(task_id, source) DO UPDATE SET sequence = max(sequence, excluded.sequence)""",
                     (task_id, source, seq))
    if new_reset:
        conn.execute("DELETE FROM task_wake_episodes WHERE task_id = ?", (task_id,))
    return not missing_reset and (not empty or new_reset)
