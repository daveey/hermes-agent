"""Publish this machine's worker pickup mode to the Board once per sync round, only when it changed. [daveey]

Moved out of ``kanban_sync_client`` (re-exported there) to keep that file under its size cap.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

_log = logging.getLogger("hermes_cli.kanban_sync_client")  # the sync loop's own logger, as before the move

_UNPUBLISHED = object()
_PICKUP_STATE: dict[str, Any] = {"value": _UNPUBLISHED, "retry_at": 0.0}
_PICKUP_UNSUPPORTED_S = 600.0


def reset_published_pickup() -> None:
    """Tests: forget the last posted pickup so the next round posts again."""
    _PICKUP_STATE["value"] = _UNPUBLISHED
    _PICKUP_STATE["retry_at"] = 0.0


def publish_pickup_if_changed(hub: Any, *, now: Optional[float] = None) -> bool:
    """Post :func:`hermes_cli.kanban_worker_node.pickup_mode` (None → null) once per sync round, and only when it
    changed. A failed post is retried next round. An older Board (404/405) stays quiet for 10 minutes. Never
    raises into the sync loop. A hub without ``report_pickup`` (test fakes) is a no-op."""
    from hermes_cli.kanban_sync_client import HubError
    from hermes_cli.kanban_worker_node import pickup_mode
    mode = pickup_mode()
    if _PICKUP_STATE["value"] == mode:
        return False
    clock = time.monotonic() if now is None else now
    if clock < float(_PICKUP_STATE["retry_at"]):
        return False
    poster = getattr(hub, "report_pickup", None)
    if poster is None:
        return False
    try:
        poster(mode)
    except HubError as exc:
        if exc.status in (404, 405):
            _PICKUP_STATE["retry_at"] = clock + _PICKUP_UNSUPPORTED_S
            _log.info("kanban sync: board has no pickup route yet (%s); retrying later", exc.status)
        else:
            _log.warning("kanban sync: pickup publish failed (%s)", exc)
        return False
    except OSError as exc:
        _log.warning("kanban sync: pickup publish failed (%s)", exc)
        return False
    _PICKUP_STATE["value"] = mode
    _PICKUP_STATE["retry_at"] = 0.0
    return True
