"""Process-local monotonic age for durable claims; SQLite never runs under the clock lock."""
from __future__ import annotations

import threading
import time

_lock = threading.Lock()
_started: dict[tuple, tuple[tuple, float]] = {}


def _database(conn):
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    return path or ("memory", conn)


def _identity(row):
    # Partial acks move inflight_from, but must not renew the oldest outstanding age.
    return (row["inflight_pid"], row["inflight_start"], row["inflight_at"])


def prune(conn):
    """Drop removed/replaced markers without evicting any still-active clock.

    Called by periodic recovery, including idle boards. Snapshot the cache before
    reading SQLite and compare again afterwards, so concurrent observations survive.
    """
    database = _database(conn)
    with _lock:
        snapshot = {k: v for k, v in _started.items() if k[0] == database}
    if not snapshot:
        return
    rows = conn.execute(
        "SELECT task_id, platform, chat_id, thread_id, inflight_pid, inflight_start, inflight_at "
        "FROM kanban_notify_subs WHERE inflight_from IS NOT NULL"
    ).fetchall()
    active = {(database, r["task_id"], r["platform"], r["chat_id"], r["thread_id"]): _identity(r) for r in rows}
    with _lock:
        for key, value in snapshot.items():
            if active.get(key) != value[0] and _started.get(key) == value:
                _started.pop(key, None)


def observe(conn, key, row, *, now=None, initial_age=0.0):
    """Additional ranges and partial acks do not renew the claim's clock."""
    identity = _identity(row)
    cache_key = (_database(conn), *key)
    tick = time.monotonic() if now is None else now
    with _lock:
        prior = _started.get(cache_key)
        if prior is None or prior[0] != identity:
            prior = (identity, tick - max(0.0, initial_age))
            _started[cache_key] = prior
        return max(0.0, tick - prior[1])


def forget(conn, key):
    cache_key = (_database(conn), *key)
    with _lock:
        _started.pop(cache_key, None)
