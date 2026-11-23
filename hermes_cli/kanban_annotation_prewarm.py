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

_boot_thread: list[Optional[threading.Thread]] = [None]
# Which step the boot thread is in, so the bind gate's budget-spent line names the slow one.
_boot_phase: list[str] = ["session annotation"]


def boot_thread() -> Optional[threading.Thread]:
    """The boot prewarm thread this process started, if any (``boot_warmup_gate`` waits on it)."""
    return _boot_thread[0]


def boot_phase() -> str:
    """The step the boot thread is in: ``session annotation``, then ``project tree``."""
    return _boot_phase[0]


_annotation_gate = threading.Condition()
_annotation_suspended = False
_annotation_inflight = 0


def suspend_annotation_prewarm(*, timeout: float = 5.0) -> None:
    """Fence new annotation reads and drain admitted ones, across profile contexts.

    Timeout retains the fence. Project-tree warmup belongs to its own consumer;
    joining the combined boot thread here would conflate the two owners.
    """
    global _annotation_suspended
    with _annotation_gate:
        _annotation_suspended = True
        if not _annotation_gate.wait_for(lambda: _annotation_inflight == 0, timeout):
            raise TimeoutError("legacy annotation prewarm did not drain")


def resume_annotation_prewarm() -> None:
    """Re-enable legacy warmup admission; the existing holder owns restart work."""
    global _annotation_suspended
    with _annotation_gate:
        _annotation_suspended = False


def prewarm_annotation() -> bool:
    """Build the annotation only while legacy warmup owns admission."""
    global _annotation_inflight
    with _annotation_gate:
        if _annotation_suspended:
            return False
        _annotation_inflight += 1
    try:
        return _prewarm_annotation()
    finally:
        with _annotation_gate:
            _annotation_inflight -= 1
            _annotation_gate.notify_all()


def _prewarm_annotation() -> bool:
    try:
        from hermes_cli import kanban_session_activity as ksa

        return ksa._annotation_snapshot() is not None
    except Exception:  # health: allow BLE001 -- best-effort warm-up; the first real read reports its own error
        log.debug("kanban annotation prewarm failed", exc_info=True)
        return False


def _boot_prewarm() -> None:
    """The annotation first (every session read waits on it), then the project tree, which reads that annotation."""
    _boot_phase[0] = "session annotation"
    prewarm_annotation()
    from hermes_cli.projects_tree_prewarm import prewarm_project_tree

    _boot_phase[0] = "project tree"
    prewarm_project_tree()


def start_annotation_prewarm() -> Optional[threading.Thread]:
    """Run the annotation and project-tree prewarms on one daemon thread (not under the test harness)."""
    if os.environ.get("HERMES_TEST_ISOLATION"):
        return None
    from agent.memory_provider import spawn_context_thread

    thread = spawn_context_thread(_boot_prewarm, name="kanban-annotation-prewarm")
    thread.start()
    _boot_thread[0] = thread
    return thread
