"""What a remote (other-gateway) card waits on, split from ``kanban_session_activity``. [daveey]

Both read only the card row: ``_park_of`` picks the park fields a replicated card carries, ``_remote_wait`` turns a
card row into wait info at read time (``kanban_wait_info.describe_replica``). Display-only: never raise.
"""
from __future__ import annotations

from typing import Any


def _park_of(task: dict[str, Any]) -> dict[str, Any] | None:
    """[daveey] The bounded park fields a replicated card's wait info needs: the park its owner published
    (``kanban_wait_replica.published_park``), else its last run's (``kanban_wait_info.park_fields``)."""
    try:
        from hermes_cli import kanban_wait_info as kwi
        if isinstance(task.get("park"), dict):
            return task["park"]
        return kwi.park_fields(task.get("run"))
    except Exception:  # display-only
        return None


def _remote_wait(card: dict[str, Any]) -> dict[str, Any] | None:
    """Wait info of a remote card row, computed now from its fields (never a stored copy, which would freeze the
    age). Stale (last-known) rows get none: they prove nothing about how long a wait has run."""
    if card.get("stale"):
        return None
    try:
        from hermes_cli import kanban_wait_info as kwi
        return kwi.describe_replica(card)
    except Exception:
        return None
