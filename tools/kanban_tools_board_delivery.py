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

# A create placed on another gateway for this chat: the card is made there, but this chat's link row reaches the
# Board only through this gateway's sync (01:09 PT: four cards placed on workers said "shows in this chat's tasks";
# the Board had no task_chat_links row for any of them).
_SHOWS = " and shows in this chat's tasks"
_SHOWS_QUEUED = "(this chat's tasks, "
_LATER = " once this gateway's Board sync recovers"


def _placed_for_this_chat(payload: dict) -> bool:
    placed = payload.get("placed")
    return isinstance(placed, dict) and bool(placed.get("origin_session"))


def _chat_link_queued(payload: dict, board_sync: str) -> None:
    """The placed create's card exists on the target; only its link to this chat waits in the outbox."""
    from hermes_cli.kanban_board_delivery import QUEUED_LOCAL
    text = str(payload.get("note") or "")
    text = text.replace(_SHOWS, _SHOWS + _LATER, 1).replace(_SHOWS_QUEUED, "(this chat's tasks" + _LATER + ", ", 1)
    payload["note"] = f"{text} This chat's link to the card is {board_sync}."
    payload["chat_link"] = QUEUED_LOCAL
    payload["board_sync"] = board_sync


def annotate(board: Callable[..., Any], tool_name: str, args: dict, out: str) -> str:
    """``out`` plus ``board_delivery: queued_local`` when the write stayed local; a comment on a card another
    gateway runs loses its live-run ``delivery`` promise, and a create placed elsewhere for this chat loses its
    "shows in this chat's tasks" promise (``chat_link: queued_local``). Unchanged while the sync runs (or on any
    read error)."""
    if (tool_name not in BOARD_WRITE_TOOLS and tool_name != "kanban_create") or not out.startswith('{"ok": true'):
        return out
    from hermes_cli import kanban_sync_runstate as runstate
    if not runstate.down():  # the common case: one config read and one stat, no board connection
        return out
    try:
        payload = json.loads(out)
        if tool_name == "kanban_create" and not _placed_for_this_chat(payload):
            return out  # a local create: the card and this chat's task list read this file
        tid = (payload.get("task_id") or args.get("task_id")) if tool_name == "kanban_comment" else None
        from hermes_cli.kanban_board_delivery import queued_local
        with board(args.get("board"), quiet_close=True) as (_kb, conn):
            note = queued_local(conn, str(tid) if tid else None)
    except Exception:  # health: allow BLE001 -- a diagnostic must never turn a committed write into an error
        logger.debug("%s: board delivery check failed", tool_name, exc_info=True)
        return out
    if not note:
        return out
    if tool_name == "kanban_create":  # the card itself is on the target, not queued here: only the link is
        _chat_link_queued(payload, note["board_sync"])
        return json.dumps(payload)
    if note.get("live_run_host"):
        payload["delivery"] = f"{note['board_sync']}; {note['route']}"
    payload.update(note)
    return json.dumps(payload)
