"""SessionState v2 user-action endpoint core (design item 7). [daveey]

One operation for unblock / answer / archive / release of a card shown in a session's object:

1. read the card's current block episode (so the resolution binds to it: a later re-block is a new episode and
   is red again);
2. commit the transition on the card's local board (``unblock_task`` / ``archive_task`` / ``kanban_release.release``;
   ``answer`` goes through the fenced decision handler registered with :func:`register_answer_handler`; without one
   it is refused, never emulated with comment-plus-unblock);
3. record the resolution for **only its own request** (others see it in history and recompute);
4. recompute the session synchronously and return the post-transition object.

A refused transition records nothing and returns the refusal with the current object.
"""
from __future__ import annotations

import time
import uuid
from contextlib import closing
from typing import Any, Callable, Optional

from hermes_session_state_schema import ACTIONS

AnswerHandler = Callable[[dict[str, Any]], dict[str, Any]]
_answer: list[Optional[AnswerHandler]] = [None]


class ActionRefused(Exception):
    def __init__(self, code: str, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def register_answer_handler(fn: Optional[AnswerHandler]) -> None:
    """``fn(request) -> {"ok": bool, "reason"?: str}`` records a decision through the owner's fenced operation."""
    _answer[0] = fn


def _episode(path, task_id: str) -> Optional[str]:
    from hermes_cli.kanban_generation import open_ro
    from hermes_session_state_collect import block_episodes

    with closing(open_ro(path, timeout=1.0)) as conn:
        return block_episodes(conn, {task_id}).get(task_id)


def _commit(action: str, path, task_id: str, request: dict[str, Any]) -> None:
    from hermes_cli import kanban_db
    from hermes_cli.kanban_db_connect import connect

    if action == "answer":
        handler = _answer[0]
        if handler is None:
            raise ActionRefused("answer_unavailable", "no fenced decision handler is registered on this gateway")
        result = handler(request) or {}
        if not result.get("ok"):
            raise ActionRefused("answer_refused", str(result.get("reason") or "decision refused"))
        return
    conn = connect(path)
    try:
        if action == "unblock":
            ok = kanban_db.unblock_task(conn, task_id, event_extra={"via": "session_state"})
        elif action == "archive":
            ok = kanban_db.archive_task(conn, task_id, via="session_state")
        else:
            from hermes_cli.kanban_release import release

            result = release(conn, task_id, note=request.get("text") or "released from the session panel")
            ok = result.get("outcome") in ("released", "already_released", "waiting_on_parents")
            if not ok:
                raise ActionRefused(str(result.get("refusal") or "refused"),
                                    str(result.get("message") or "release refused"))
    finally:
        conn.close()
    if not ok:
        raise ActionRefused(f"{action}_refused", f"{action} was refused for {task_id} (current status disallows it)")


def apply_action(store: Any, request: dict[str, Any], *, clock: Callable[[], float] = time.time) -> dict[str, Any]:
    """See the module doc. ``request``: ``{action, session_id, card: "<board>/<id>", request_id?, choice_id?,
    label?, text?, by?}``. Returns ``{"ok": True, "object", "rev", "resolution"}``; raises :class:`ActionRefused`."""
    from hermes_session_state_collect import board_paths

    action = request.get("action")
    if action not in ACTIONS:
        raise ActionRefused("bad_action", f"action must be one of {', '.join(ACTIONS)}", 400)
    session_id, card = str(request.get("session_id") or ""), str(request.get("card") or "")
    if not session_id or card.count("/") != 1:
        raise ActionRefused("bad_request", "session_id and card '<board>/<id>' are required", 400)
    board, task_id = card.split("/", 1)
    path = board_paths().get(board)
    if path is None:
        raise ActionRefused("not_local", f"board {board!r} is not a local board of this gateway", 404)
    before = store.get(session_id) or store.recompute(session_id, trigger="action")
    if before is None:
        raise ActionRefused("unknown_session", f"unknown session {session_id}", 404)
    if card not in (before.get("cards") or {}):
        raise ActionRefused("not_shown", f"{card} is not shown in this session's state", 404)
    episode = _episode(path, task_id)
    _commit(str(action), path, task_id, request)
    resolution = {"request_id": str(request.get("request_id") or f"action:{uuid.uuid4().hex}"), "card": card,
                  "action": action, "choice_id": request.get("choice_id"), "label": request.get("label"),
                  "by": str(request.get("by") or "user"), "at": clock(), "delivery": "committed",
                  "episode": episode}
    lineage = before["identity"]["lineage"]
    store.add_resolution(lineage, resolution)
    after = store.recompute(session_id, trigger="action")
    return {"ok": True, "object": after, "rev": (after or {}).get("rev"), "resolution": resolution}
