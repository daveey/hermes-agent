"""Book a clean exit whose Kanban calls a host guard refused as a host refusal, not a protocol violation. [daveey]

2026-11-12 09:13-09:29 PST, David-MBP: one slow ``pre_tool_call`` plugin callback timed out; core then refused
every tool call for 60 s (``plugins_dispatch``: fail-closed guard, suppression window). The worker's
``kanban_continue`` / ``kanban_comment`` / ``kanban_heartbeat`` calls came back
``pre_tool_call plugin callback timed out or is still running``; the worker ended its turn with a text
summary (rc=0), and the sweep booked a protocol violation: counted toward the breaker, ✖ notice to David.
Two runs of t_01a1ce9070eee14c were lost that way.

The worker did try to report; its host would not let it. When a clean-exit run's last tool results (its
registered worker session in ``state.db``, ``kanban_worker_recovery``) include that refusal on a ``kanban_*``
call, the run is rebooked like a run lost to its owner's restart: requeued, no failure counted, no ✖ notice
(the ``owner_restart`` bookkeeping, which the Board's neutral release already knows), with
``guard_refusal`` in the run metadata and the refused tools in the error text. A run that never attempted a
Kanban call, or whose refusals are older than its last few tool results, stays a protocol violation.
(``kanban_host_refusal`` is the unrelated startup/owner-storage proof.)
"""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Optional

from hermes_cli.plugins_dispatch import _PRE_TOOL_CALL_TIMEOUT_BLOCK_MESSAGE as REFUSAL_MARKER

logger = logging.getLogger(__name__)
PAYLOAD_KEY = "guard_refusal"
# The worker's last tool results that count as "how the run ended".
TAIL_TOOL_RESULTS = 6
# Consecutive uncounted guard-refusal runs before the next one is charged as a protocol violation again.
STREAK_LIMIT = 3


def _prior_refusal_streak(conn: sqlite3.Connection, task_id: str, run_id) -> int:
    """Closed runs before *run_id*, newest first, booked as guard refusals without a break."""
    streak = 0
    for (metadata,) in conn.execute(
            "SELECT metadata FROM task_runs WHERE task_id=? AND id<? AND ended_at IS NOT NULL "
            "ORDER BY id DESC LIMIT ?", (task_id, int(run_id), STREAK_LIMIT)):
        try:
            meta = json.loads(metadata or "{}")
        except ValueError:
            meta = None
        if not (isinstance(meta, dict) and meta.get(PAYLOAD_KEY)):
            break
        streak += 1
    return streak


def refused_kanban_calls(conn: Optional[sqlite3.Connection], task_id: str, run_id) -> list[str]:
    """Kanban tools among *run_id*'s last ``TAIL_TOOL_RESULTS`` tool results that a skipped
    ``pre_tool_call`` guard refused, oldest first; ``[]`` when none or the session can't be read."""
    if conn is None or not task_id or run_id is None:
        return []
    try:
        row = conn.execute(
            "SELECT state_db, session_id FROM worker_recovery_sessions WHERE task_id=? AND run_id=? "
            "ORDER BY registered_at DESC LIMIT 1", (task_id, int(run_id))).fetchone()
    except (sqlite3.Error, TypeError, ValueError):
        return []  # an old board has no session registry: no evidence
    if not row:
        return []
    state_db, session_id = row[0], row[1]
    try:
        with closing(sqlite3.connect(Path(state_db).as_uri() + "?mode=ro", uri=True, timeout=2)) as state:
            rows = state.execute(
                "SELECT tool_name, substr(content, 1, 2000) FROM messages "
                "WHERE session_id=? AND role='tool' ORDER BY id DESC LIMIT ?",
                (session_id, TAIL_TOOL_RESULTS)).fetchall()
    except (OSError, ValueError, sqlite3.Error):
        logger.debug("guard refusal: session %s unreadable", session_id, exc_info=True)
        return []
    return [str(name) for name, content in reversed(rows)
            if str(name or "").startswith("kanban_") and REFUSAL_MARKER in (content or "")]


def note(dead, conn: Optional[sqlite3.Connection], task_id: Optional[str], run_id) -> None:
    """Rebook a clean-exit protocol violation (``_DeadWorker``) as a host refusal when the run's last
    tool results show a host guard refusing its Kanban calls. No-op for any other death."""
    if conn is None or not dead.protocol_violation or not task_id:
        return
    refused = refused_kanban_calls(conn, task_id, run_id)
    if not refused:
        return
    streak = _prior_refusal_streak(conn, task_id, run_id)
    if streak >= STREAK_LIMIT:
        # A guard that stays hung would requeue the card uncounted forever: charge it from here on.
        dead.event_payload[PAYLOAD_KEY + "_streak"] = streak
        return
    original = dead.error_text
    dead.protocol_violation = False
    dead.kind, dead.owner_restart, dead.event_kind = "owner_restart", True, "owner_restart_lost"
    payload = dead.event_payload
    payload.pop("protocol_violation", None)
    payload.update(exit_kind="guard_refusal", owner_restart=True, infrastructure=True,
                   **{PAYLOAD_KEY: {"refused_tools": refused}})
    dead.error_text = (
        f"pid {payload.get('pid')} exited without a terminal kanban call: the host refused its kanban calls "
        f"({', '.join(dict.fromkeys(refused))}: {REFUSAL_MARKER}) — host refusal, requeued without counting "
        f"a failure; was: {original[:100]}")
