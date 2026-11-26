"""One-time rebuild of replicas that froze stopped cards against the feed. [daveey]

Before ``kanban_stop_sync.frozen_change`` skipped replicas, a pulled copy (``sync-shadow/<board>.db``) dropped every
feed change for a card holding an ``administrative_stop_restricted`` event, while its cursor advanced. The feed
never resends those changes, so such a replica keeps the stopped card's status as of the stop (paintbot-tools M5
stayed ``scheduled`` after its home archived it). A snapshot resync re-delivers every row; it runs once per replica
that holds a stop event, and a marker beside the file records it.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)
SUFFIX = ".stop-unfrozen"


def _marker(path: Path) -> Path:
    return path.with_name(path.name + SUFFIX)


def needed(replica: sqlite3.Connection, path: Path) -> bool:
    from hermes_cli.kanban_stop_ended import KIND
    if _marker(path).exists():
        return False
    return replica.execute("SELECT 1 FROM task_events WHERE kind = ? LIMIT 1", (KIND,)).fetchone() is not None


def unfreeze(replica: sqlite3.Connection, hub: Any, slug: str, path: Path) -> int:
    """Resync ``replica`` from the Board snapshot once when it may hold frozen cards; returns rows read."""
    from hermes_cli import kanban_sync_client as ksc
    if not needed(replica, path):
        return 0
    count = ksc.resync(replica, hub, slug, observers=False)
    _marker(path).write_text("done\n", encoding="utf-8")
    _log.warning("kanban sync: %s replica held stopped cards frozen against the feed; resynced %d rows", slug, count)
    return count
