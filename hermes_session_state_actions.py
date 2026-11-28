"""SessionState v2 user-action endpoint core (design item 7). [daveey]

One operation for unblock / answer / archive / release of a card shown in a session's object:

1. the caller names the block episode it was shown (``episode``: the card's ``block_episode``, null when it was
   not blocked; it carries the stored ask's digest); every transition is fenced to it inside its write
   transaction, so a click on an ask that was answered and re-blocked since, or whose ask was filed or replaced
   after the shown state (a new episode), is refused ``stale_episode`` and never resolves an ask the user did not
   see;
2. commit the transition on the card's local board (``unblock_task`` / ``archive_task``; ``release`` only on the
   home-filed ``release_target`` offer, as an explicit human writer, like ``kanban_card_answer._release``;
   ``answer`` goes through the fenced decision handler registered with :func:`register_answer_handler`; without
   one it is refused, never emulated with comment-plus-unblock);
3. record the resolution for **only its own request**, bound to the shown episode (others see it in history and
   recompute);
4. recompute the session synchronously and return the post-transition object.

A refused transition records nothing and returns the refusal with the current object.
"""
from __future__ import annotations

import json
import time
import uuid
from contextlib import closing
from typing import Any, Callable, Optional

from hermes_session_state_schema import ACTIONS

AnswerHandler = Callable[[dict[str, Any]], dict[str, Any]]
Fence = Callable[[Any], Optional[str]]
_answer: list[Optional[AnswerHandler]] = [None]
_DONE_RELEASE = ("released", "already_released", "waiting_on_parents")


class ActionRefused(Exception):
    def __init__(self, code: str, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def register_answer_handler(fn: Optional[AnswerHandler]) -> None:
    """``fn(request) -> {"ok": bool, "reason"?: str}`` records a decision through the owner's fenced operation."""
    _answer[0] = fn


def current_episode(conn, task_id: str) -> Optional[str]:
    from hermes_session_state_collect import block_episodes

    return block_episodes(conn, {task_id}).get(task_id)


def _fence(task_id: str, shown: Optional[str]) -> Fence:
    """The precondition every transition asks inside its write transaction: the card's current block episode
    must still be the one the user was shown."""
    def check(conn) -> Optional[str]:
        now = current_episode(conn, task_id)
        if now != shown:
            return f"stale_episode: shown {shown or 'unblocked'}, now {now or 'unblocked'}"
        return None
    return check


def _release(conn, task_id: str, request: dict[str, Any], fence: Fence) -> None:
    """Release a held card as the user's authenticated act, as ``kanban_card_answer._release`` does: only when the
    card's stored ask carries the home-filed ``release_target`` offer that is still true
    (``kanban_release.offered``), with an explicit human writer (never the serving process's ancestry walk) and
    the shown-episode fence asked before anything is written and again inside the deciding transaction."""
    from hermes_cli import kanban_release

    row = conn.execute("SELECT human_request FROM tasks WHERE id = ?", (task_id,)).fetchone()
    try:
        ask = json.loads(row[0] or "null") if row is not None else None
    except (TypeError, ValueError):
        ask = None
    if kanban_release.offered(conn, task_id, ask) is None:
        raise ActionRefused("not_offered", f"{task_id} has no standing release offer; use unblock instead")
    error = fence(conn)
    if error:
        raise ActionRefused("stale_episode", error)
    operation = str(request["request_id"])
    result = kanban_release.release(
        conn, task_id, writer=kanban_release.human_writer("session-state-action:" + operation),
        author="session-state", note=str(request.get("text") or f"Session panel release, request {operation}"),
        precondition=fence)
    if result.get("outcome") in _DONE_RELEASE:
        return
    refusal, message = str(result.get("refusal") or "refused"), str(result.get("message") or "release refused")
    if refusal == "ask_changed" or message.startswith("stale_episode"):
        raise ActionRefused("stale_episode", message)
    raise ActionRefused(refusal, message)


def _commit(action: str, path, task_id: str, request: dict[str, Any], shown: Optional[str]) -> None:
    from hermes_cli import kanban_db
    from hermes_cli.kanban_db_connect import connect

    fence = _fence(task_id, shown)
    if action == "answer":
        handler = _answer[0]
        if handler is None:
            raise ActionRefused("answer_unavailable", "no fenced decision handler is registered on this gateway")
        with closing(connect(path)) as conn:
            error = fence(conn)
        if error:
            raise ActionRefused("stale_episode", error)
        result = handler({**request, "episode": shown}) or {}
        if not result.get("ok"):
            raise ActionRefused("answer_refused", str(result.get("reason") or "decision refused"))
        return
    with closing(connect(path)) as conn:
        if action == "release":
            _release(conn, task_id, request, fence)
            return
        if action == "unblock":
            ok = kanban_db.unblock_task(conn, task_id, event_extra={"via": "session_state"}, precondition=fence)
        else:
            ok = kanban_db.archive_task(conn, task_id, via="session_state", precondition=fence)
        stale = None if ok else fence(conn)
    if stale:
        raise ActionRefused("stale_episode", stale)
    if not ok:
        raise ActionRefused(f"{action}_refused", f"{action} was refused for {task_id} (current status disallows it)")


def apply_action(store: Any, request: dict[str, Any], *, clock: Callable[[], float] = time.time) -> dict[str, Any]:
    """See the module doc. ``request``: ``{action, session_id, card: "<board>/<id>", episode, request_id?,
    choice_id?, label?, text?, by?}``; ``episode`` is required (the ``block_episode`` shown, or null). Returns
    ``{"ok": True, "object", "rev", "resolution"}``; raises :class:`ActionRefused`."""
    from hermes_session_state_collect import board_paths

    action = request.get("action")
    if action not in ACTIONS:
        raise ActionRefused("bad_action", f"action must be one of {', '.join(ACTIONS)}", 400)
    session_id, card = str(request.get("session_id") or ""), str(request.get("card") or "")
    if not session_id or card.count("/") != 1:
        raise ActionRefused("bad_request", "session_id and card '<board>/<id>' are required", 400)
    shown = request.get("episode")
    if "episode" not in request or not (shown is None or (isinstance(shown, str) and shown)):
        raise ActionRefused("episode_required", "episode (the block_episode shown, or null) is required", 400)
    board, task_id = card.split("/", 1)
    path = board_paths().get(board)
    if path is None:
        raise ActionRefused("not_local", f"board {board!r} is not a local board of this gateway", 404)
    before = store.get(session_id) or store.recompute(session_id, trigger="action")
    if before is None:
        raise ActionRefused("unknown_session", f"unknown session {session_id}", 404)
    if card not in (before.get("cards") or {}):
        raise ActionRefused("not_shown", f"{card} is not shown in this session's state", 404)
    request = {**request, "request_id": str(request.get("request_id") or f"action:{uuid.uuid4().hex}")}
    _commit(str(action), path, task_id, request, shown)
    resolution = {"request_id": request["request_id"], "card": card, "action": action,
                  "choice_id": request.get("choice_id"), "label": request.get("label"),
                  "by": str(request.get("by") or "user"), "at": clock(), "delivery": "committed", "episode": shown}
    lineage = before["identity"]["lineage"]
    store.add_resolution(lineage, resolution)
    after = store.recompute(session_id, trigger="action")
    return {"ok": True, "object": after, "rev": (after or {}).get("rev"), "resolution": resolution}
