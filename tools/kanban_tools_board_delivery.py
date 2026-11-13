"""Kanban tool results while this gateway's Board sync is stopped (``hermes_cli.kanban_board_delivery``). [daveey]"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Tools whose success writes a row other hosts must see (pushed through this gateway's Board sync outbox).
BOARD_WRITE_TOOLS = frozenset({
    "kanban_complete", "kanban_block", "kanban_unblock", "kanban_schedule", "kanban_continue",
    "kanban_request_review", "kanban_request_changes", "kanban_comment", "kanban_attach", "kanban_attach_url",
    "kanban_link", "kanban_unlink", "kanban_archive",
})


def annotate(board: Callable[..., Any], tool_name: str, args: dict, out: str) -> str:
    """``out`` plus ``board_delivery: queued_local`` when the write stayed local; a comment on a card another
    gateway runs loses its live-run ``delivery`` promise. Unchanged while the sync runs (or on any read error)."""
    if tool_name not in BOARD_WRITE_TOOLS or not out.startswith('{"ok": true'):
        return out
    from hermes_cli import kanban_sync_runstate as runstate
    if not runstate.down():  # the common case: one config read and one stat, no board connection
        return out
    try:
        payload = json.loads(out)
        tid = (payload.get("task_id") or args.get("task_id")) if tool_name == "kanban_comment" else None
        from hermes_cli.kanban_board_delivery import queued_local
        with board(args.get("board"), quiet_close=True) as (_kb, conn):
            note = queued_local(conn, str(tid) if tid else None)
    except Exception:  # health: allow BLE001 -- a diagnostic must never turn a committed write into an error
        logger.debug("%s: board delivery check failed", tool_name, exc_info=True)
        return out
    if not note:
        return out
    if note.get("live_run_host"):
        payload["delivery"] = f"{note['board_sync']}; {note['route']}"
    payload.update(note)
    return json.dumps(payload)
