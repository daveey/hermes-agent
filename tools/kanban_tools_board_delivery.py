"""Kanban tool results while this gateway's Board sync is stopped (``hermes_cli.kanban_board_delivery``). [daveey]"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable

# The owner's answer is defined once, where the owner writes it (PR #2157 review). [daveey]
from hermes_cli.kanban_origin_creation import OWNER_WRITTEN
from hermes_cli.kanban_creation_wait import queued_wait as queued_wait  # noqa: PLC0414 -- public result helper reexport

logger = logging.getLogger(__name__)

# Tools whose success writes a row other hosts must see (pushed through this gateway's Board sync outbox).
BOARD_WRITE_TOOLS = frozenset({
    "kanban_complete", "kanban_block", "kanban_unblock", "kanban_schedule", "kanban_continue",
    "kanban_request_review", "kanban_request_changes", "kanban_comment", "kanban_attach", "kanban_attach_url",
    "kanban_link", "kanban_unlink", "kanban_archive",
})

# A create placed on another gateway for this chat: when the owner answers that it wrote this chat's ``created`` link
# (``placed.chat_link: owner_written``, from ``kanban_origin_creation.origin_link_fields``), the link no longer waits
# in this gateway's outbox; what still depends on this gateway is its replica of the Board, which this chat's task
# list reads and the stopped sync no longer pulls. Only an owner on this core or newer whose Board sync is on (it has
# a task_chat_links table) and not recorded stopped gives that answer. Without it the link waits here: no answer yet
# (a queued request, whose owner_written would only be the expected outcome, not a fact, so it is never claimed), an
# owner on older core (it writes no link), an owner with its sync off, or a ``local-<sha256>`` chat it does not link.
# kanban_tools builds its placed-create notes from these fragments, so the rewrites below always find them. The
# queued note is CHAT_TASKS_HEAD + CHAT_TASKS_SEP; the rewrites insert after the exact head, whatever the separator.
CHAT_TASKS_SHOWS = " and shows in this chat's tasks"
CHAT_TASKS_HEAD = "(this chat's tasks"
CHAT_TASKS_SEP = ", "
_LATER = " once this gateway's copy of the Board refreshes"
_QUEUED_LATER = " once this gateway's Board sync recovers"


def _placed_for_this_chat(payload: dict) -> bool:
    placed = payload.get("placed")
    return isinstance(placed, dict) and bool(placed.get("origin_session"))


def _owner_links(payload: dict) -> bool:
    """True only when the owner itself said it wrote this chat's link (``placed.chat_link``). The chat id's shape
    cannot decide it: the owner writes one for a ``local-<sha256>`` chat an ancestor's ``created`` link proves
    (``proven_foreign_chats``), and none at all on older core or with its Board sync off."""
    return payload["placed"].get("chat_link") == OWNER_WRITTEN


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
    owner = f"owner gateway {payload['runs_where']}" if payload.get("runs_where") else "the card's home gateway"
    text = str(payload.get("note") or "")
    text = text.replace(CHAT_TASKS_SHOWS, CHAT_TASKS_SHOWS + _LATER, 1).replace(
        CHAT_TASKS_HEAD + CHAT_TASKS_SEP, CHAT_TASKS_HEAD + _LATER + CHAT_TASKS_SEP, 1)
    sync_note = (f"{owner} writes this chat's link to the Board; this gateway's Board sync has been stopped since "
                 f"{runstate.when(record.get('since'))} ({record.get('reason') or 'unknown reason'}), so its copy "
                 "of the Board may not refresh until it recovers")
    # [daveey] Capitalize only the fixed lead word: a gateway id stays verbatim ("worker-4", never "Worker-4").
    payload["note"] = f"{text} {sync_note[0].upper()}{sync_note[1:]}."
    payload["chat_link"] = OWNER_WRITTEN
    payload["board_sync"] = sync_note


def annotate(board: Callable[..., Any], tool_name: str, args: dict, out: str) -> str:
    """``out`` plus ``board_delivery: queued_local`` when the write stayed local; a comment on a card another
    gateway runs loses its live-run ``delivery`` promise, and a create placed elsewhere for this chat says its link
    is on the Board but this gateway's copy of it may lag (``chat_link: owner_written``) when the owner answered that
    it wrote that link, else that the link waits here (``chat_link: queued_local``). Unchanged while the sync runs
    (or on any read error)."""
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
    if tool_name == "kanban_create":  # the card is on the Board; its link is too only if the owner says it wrote it
        if _owner_links(payload):
            _chat_link_by_owner(payload)
        else:
            _chat_link_queued(payload, note["board_sync"])
        return json.dumps(payload)
    if note.get("live_run_host"):
        payload["delivery"] = f"{note['board_sync']}; {note['route']}"
    payload.update(note)
    return json.dumps(payload)
