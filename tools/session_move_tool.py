"""``session_move``: a chat hands itself over to another gateway after this turn. [daveey]

It starts ``hermes sessions move <this session> --to <to> --reason ... --after-turn`` detached
(:func:`hermes_cli.session_handover.spawn_after_turn_move`): the move waits for this turn to end, copies the chat to
``to``, records the new owner on the Board and archives the copy here (hermes_cli.session_handover). Chats only:
never available to a Kanban worker (``HERMES_KANBAN_TASK``), and only when ``sessions.move.hosts`` lists a target.
"""
from __future__ import annotations

import os
from typing import Any, Optional

from tools.registry import no_cache_check_fn, registry, tool_error, tool_result

SESSION_MOVE_SCHEMA = {
    "name": "session_move",
    "description": (
        "Move THIS chat to another gateway (machine) after the current turn ends. Use it when the work needs "
        "files, devices or tools that only exist there. The chat's whole history is copied, the other gateway becomes "
        "its owner and the copy here is archived; the user reopens the chat on the target. Only targets listed in "
        "sessions.move.hosts are allowed. Say what will happen and why before calling it; the move starts once "
        "this turn is over."),
    "parameters": {
        "type": "object",
        "properties": {
            "to": {"type": "string", "description": "Target gateway name (a key of sessions.move.hosts)"},
            "reason": {"type": "string", "description": "Why this chat must move (recorded with the move)"},
        },
        "required": ["to", "reason"],
    },
}


def _hosts() -> dict[str, str]:
    from hermes_cli.session_handover import move_hosts
    return move_hosts()


@no_cache_check_fn
def check_session_move_requirements() -> bool:
    """Chats only (never a Kanban worker), and only with a configured target."""
    if os.environ.get("HERMES_KANBAN_TASK"):
        return False
    try:
        return bool(_hosts())
    except Exception:  # health: allow BLE001 -- unreadable config: the tool is not offered
        return False


def _current_session_id() -> Optional[str]:
    from gateway.session_context import get_session_env
    for name in ("HERMES_SESSION_KEY", "HERMES_SESSION_ID"):
        value = (get_session_env(name, "") or "").strip()
        if value:
            return value
    return None


def session_move(to: Any, reason: Any) -> str:
    if os.environ.get("HERMES_KANBAN_TASK"):
        return tool_error("session_move is for chats; a Kanban worker moves its card with kanban_move")
    to = str(to or "").strip()
    reason = str(reason or "").strip()
    hosts = _hosts()
    if to not in hosts:
        return tool_error(f"{to!r} is not a configured move target (sessions.move.hosts: "
                          f"{', '.join(sorted(hosts)) or 'none'})", code="unknown_host")
    if not reason:
        return tool_error("reason is required (why this chat must move)", code="reason_required")
    sid = _current_session_id()
    if not sid:
        return tool_error("no current chat session to move", code="no_session")
    from hermes_cli import session_handover as sh
    pid = sh.spawn_after_turn_move(sid, to, reason)
    return tool_result({"ok": True, "session_id": sid, "to": to, "pid": pid,
                        "message": f"moving after this turn; reopen the chat on {to}"})


registry.register(
    name="session_move", toolset="session_move", schema=SESSION_MOVE_SCHEMA,
    check_fn=check_session_move_requirements,
    handler=lambda args, **kw: session_move(args.get("to"), args.get("reason")),
    emoji="🚚")
