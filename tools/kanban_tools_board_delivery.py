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

# A create placed on another gateway for this chat: the card's owner writes this chat's ``created`` link to the Board
# (``kanban_origin_creation._link_origin``), so the link no longer waits in this gateway's outbox. What still depends
# on this gateway is its replica of the Board, which this chat's task list reads and the stopped sync no longer pulls.
# kanban_tools builds its placed-create notes from these fragments, so the rewrites below always find them. The
# queued note is CHAT_TASKS_HEAD + CHAT_TASKS_SEP; the rewrites insert after the exact head, whatever the separator.
CHAT_TASKS_SHOWS = " and shows in this chat's tasks"
CHAT_TASKS_HEAD = "(this chat's tasks"
CHAT_TASKS_SEP = ", "
_LATER = " once this gateway's copy of the Board refreshes"
OWNER_WRITTEN = "owner_written"
_QUEUED_LATER = " once this gateway's Board sync recovers"


def _placed_for_this_chat(payload: dict) -> bool:
    placed = payload.get("placed")
    return isinstance(placed, dict) and bool(placed.get("origin_session"))


def _owner_links(payload: dict) -> bool:
    """False for a receipt-shaped chat id (``local-<sha256>``): the owner cannot tell it from a worker's own hash,
    so it writes no link (``_link_origin``) and the link still waits for this gateway's ledger reconcile."""
    from hermes_cli.kanban_session_provenance import _HEADLESS_SESSION
    session = str(payload["placed"]["origin_session"]).rsplit("/", 1)[-1]
    return not _HEADLESS_SESSION.fullmatch(session)


def _chat_link_queued(payload: dict, board_sync: str) -> None:
    """The placed create's card exists on the target; only its link to this chat waits in the outbox."""
    from hermes_cli.kanban_board_delivery import QUEUED_LOCAL
    text = str(payload.get("note") or "")
    text = text.replace(CHAT_TASKS_SHOWS, CHAT_TASKS_SHOWS + _QUEUED_LATER, 1).replace(
        CHAT_TASKS_HEAD + CHAT_TASKS_SEP, CHAT_TASKS_HEAD + _QUEUED_LATER + CHAT_TASKS_SEP, 1)
    payload["note"] = f"{text} This chat's link to the card is {board_sync}."
    payload["chat_link"] = QUEUED_LOCAL
    payload["board_sync"] = board_sync


def _chat_link_by_owner(payload: dict) -> None:
    """The placed card and its link to this chat are on the Board; only this gateway's replica lags."""
    from hermes_cli import kanban_sync_runstate as runstate
    record = runstate.down() or {}
    owner = payload.get("runs_where") or "the card's home gateway"
    text = str(payload.get("note") or "")
    text = text.replace(CHAT_TASKS_SHOWS, CHAT_TASKS_SHOWS + _LATER, 1).replace(
        CHAT_TASKS_HEAD + CHAT_TASKS_SEP, CHAT_TASKS_HEAD + _LATER + CHAT_TASKS_SEP, 1)
    sync_note = (f"{owner} writes this chat's link to the Board; this gateway's Board sync has been stopped since "
                 f"{runstate.when(record.get('since'))} ({record.get('reason') or 'unknown reason'}), so its copy "
                 "of the Board may not refresh until it recovers")
    payload["note"] = f"{text} {sync_note[0].upper()}{sync_note[1:]}."
    payload["chat_link"] = OWNER_WRITTEN
    payload["board_sync"] = sync_note


def annotate(board: Callable[..., Any], tool_name: str, args: dict, out: str) -> str:
    """``out`` plus ``board_delivery: queued_local`` when the write stayed local; a comment on a card another
    gateway runs loses its live-run ``delivery`` promise, and a create placed elsewhere for this chat says its link
    is on the Board but this gateway's copy of it may lag (``chat_link: owner_written``), or, for a ``local-<sha256>``
    chat the owner writes no link for, that the link waits here (``chat_link: queued_local``). Unchanged while the
    sync runs (or on any read error)."""
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
    if tool_name == "kanban_create":  # the card is on the Board; its owner writes this chat's link unless headless
        if _owner_links(payload):
            _chat_link_by_owner(payload)
        else:
            _chat_link_queued(payload, note["board_sync"])
        return json.dumps(payload)
    if note.get("live_run_host"):
        payload["delivery"] = f"{note['board_sync']}; {note['route']}"
    payload.update(note)
    return json.dumps(payload)
