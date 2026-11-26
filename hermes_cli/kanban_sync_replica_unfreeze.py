"""One-time full rebuild of replicas that consumed changes while stop-frozen. [daveey]

A snapshot upserts live rows but cannot recover consumed deletions in a populated
replica. Use the retention-rebuild path, preserving quarantine and invalidating
pooled descriptors. Persist intent before discarding the old replica so failures
retry even when the new database has not yet received its stop event.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SUFFIX = ".stop-unfrozen"


def _marker(path: Path) -> Path:
    return path.with_name(path.name + SUFFIX)


def _pending(path: Path) -> Path:
    return path.with_name(path.name + SUFFIX + ".pending")


def needed(replica: sqlite3.Connection, path: Path) -> bool:
    from hermes_cli.kanban_stop_ended import KIND
    if _marker(path).exists():
        return False
    return _pending(path).exists() or replica.execute(
        "SELECT 1 FROM task_events WHERE kind = ? LIMIT 1", (KIND,)).fetchone() is not None


def begin(path: Path) -> None:
    _pending(path).write_text("pending\n", encoding="utf-8")


def finish(path: Path) -> None:
    _marker(path).write_text("done\n", encoding="utf-8")
    _pending(path).unlink(missing_ok=True)
