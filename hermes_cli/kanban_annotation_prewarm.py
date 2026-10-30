"""Boot-time read of the session-list kanban annotation, so the Desktop's first poll burst after a backend
start finds it built instead of every session read waiting on one cold pass under the burst's GIL contention.

A cold annotation (no snapshot served yet for the scope) makes every caller of ``annotate_kanban_activity`` wait on
the single in-flight read: ``GET /api/sessions``, ``/api/profiles/sessions``, the sidebar and the one-row
``GET /api/sessions/<id>``. On hermes-hub that pass (origin-ledger remote-links projection over the board replicas
plus the board counts) is ~0.7 s CPU alone and 5-10 s inside a reconnect burst; the session-detail request waited
behind it for 4-5 s. Mirrors ``plugins/kanban/dashboard/board_prewarm.py`` for the board snapshot.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Optional

log = logging.getLogger(__name__)


def prewarm_annotation() -> bool:
    """Build this process's annotation snapshot (and the remote-links projection under it). Never raises."""
    try:
        from hermes_cli import kanban_session_activity as ksa

        return ksa._annotation_snapshot() is not None
    except Exception:  # health: allow BLE001 -- best-effort warm-up; the first real read reports its own error
        log.debug("kanban annotation prewarm failed", exc_info=True)
        return False


def start_annotation_prewarm() -> Optional[threading.Thread]:
    """Run ``prewarm_annotation`` on a daemon thread (not under the test harness)."""
    if os.environ.get("HERMES_TEST_ISOLATION"):
        return None
    from agent.memory_provider import spawn_context_thread

    thread = spawn_context_thread(prewarm_annotation, name="kanban-annotation-prewarm")
    thread.start()
    return thread
