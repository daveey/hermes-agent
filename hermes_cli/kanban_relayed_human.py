"""A chat relaying David's answer to a card: verified provenance for the no-progress budget. [daveey]

The designed ask-and-answer path is: a card blocks ``needs_input``, David answers in a chat, and the chat
passes the answer on with ``kanban_comment`` (or ``cross_task`` across gateways). Those writers always record
``writer_kind='agent'`` -- correctly, since an agent typed the comment -- so the no-progress budget, which
counts only human comments, saw five answered asks as five idle runs and stopped the card
(t_01a1cc1a8bb39958, defect kanban-d1b921f32e).

A relay may now say so: ``relayed_human = {session, message_id, at}`` on the ``commented`` event. It never
raises ``writer_kind`` (no operator authority is gained); it only tells the budget that a person answered.
The origin (the chat writing the comment) verifies it:

* only an interactive chat session: not a Kanban worker, not a delegated child (the tool rejects those);
* that session's newest real user message is newer than the card's latest ask (its newest ``blocked`` or
  ``scheduled`` event): an answer cannot predate the question;
* one relay per user message per card: the same user turn cannot mark two comments.

Only a row a person typed is a reply: a plain or ``steer`` user row, never a synthetic ``role='user'`` row (the
card's own ``kanban_notice``, hidden scaffolding, process/delegation completions, internal notifications,
compaction summaries, ``[System:`` notices or cross-session deliveries). Untyped synthetic rows are recognized
by their producers' registered openings (runtime scaffolding, the pre-final gate's held-draft notice, auto-continue
notes, the ``/loop`` wakeup and ``/heartbeat`` frames). A slash ``/loop`` tick's expanded prompt is typed
``loop_tick`` (TUI and gateway); an in-flight tick's first untyped row is still skipped for rows written before that
typing. A Kanban notice is always typed ``kanban_notice``, the no-store fallback included.

The card's holder checks every relay before storing it (``admit``, inside ``add_comment``'s write transaction,
so the one-relay rule is atomic), a cross-gateway one included: the card asked, the message is not older than
the latest ask (``REMOTE_CLOCK_SKEW_SECONDS`` allowed for a remote clock), and no ``(session, message_id)`` is
relayed to the card twice.
"""
from __future__ import annotations

import json
import os
import sqlite3
from typing import Any, Optional

RELAY_FIELD = "relayed_human"
SUSPECT_EVENT = "no_progress_relay_suspect"
# A chat unblock this close to the escalation (and no relayed comment) is most likely an unmarked relay.
SUSPECT_WINDOW_SECONDS = 15 * 60
# A cross-gateway relay's ``at`` is the origin's clock; allow this much skew against the card's ask.
REMOTE_CLOCK_SKEW_SECONDS = 120
# Display kinds of a user row a person typed (NULL/'' = a plain prompt). Every other kind is synthetic.
_TYPED_KINDS = ("steer",)
# Untyped role='user' rows that are still not the user's words, beyond core's registries (``_synthetic_prefixes``):
# gateway ``[System:`` / ``[System note:`` notices (auto-continue and interrupted-turn notes) and the
# session-mentions courier.
_EXTRA_SYNTHETIC_PREFIXES = ("[System:", "[System note:", "[Message from another Hermes session")


class RelayRefused(ValueError):
    """The comment cannot be marked as carrying the user's message."""


def _latest_ask(conn: sqlite3.Connection, task_id: str) -> Optional[int]:
    row = conn.execute("SELECT MAX(created_at) FROM task_events WHERE task_id = ? AND kind IN ('blocked', 'scheduled')",
                       (task_id,)).fetchone()
    return int(row[0]) if row and row[0] is not None else None


def _synthetic_prefixes() -> tuple[str, ...]:
    """Openings of untyped synthetic user rows, taken from their producers' registries so a nudge registered
    there is excluded here too: compaction/runtime scaffolding (task-list snapshot, background-process notices,
    ``[System:`` nudges), the pre-final gate's held-draft notice, the ``/loop`` wakeup frame and the
    ``/heartbeat`` tick frame (the TUI submits both untyped)."""
    from agent.conversation_compression_scaffolding import _SYNTHETIC_USER_PREFIXES
    from agent.pre_final_gate import HELD_NOTICE
    from hermes_cli.heartbeat import HEARTBEAT_PROMPT_TEMPLATE
    from hermes_cli.loops import WAKEUP_PROMPT_TEMPLATE, WAKEUP_PROMPT_WITH_UNTIL_TEMPLATE
    frames = {t.split("{", 1)[0] for t in (WAKEUP_PROMPT_TEMPLATE, WAKEUP_PROMPT_WITH_UNTIL_TEMPLATE,
                                           HEARTBEAT_PROMPT_TEMPLATE)}
    return (*_SYNTHETIC_USER_PREFIXES, HELD_NOTICE, *sorted(frames), *_EXTRA_SYNTHETIC_PREFIXES)


def _slash_loop_tick_since(sdb: sqlite3.Connection, session_id: str) -> Optional[float]:
    """When this session's slash-command ``/loop`` tick (``/loop 10m /recap``) fired, while that tick is in flight
    (``awaiting_response``): it submits the command's prompt as an untyped row, which is the loop's, not a reply.
    A wakeup-framed loop's row is caught by its frame instead. ``None`` otherwise.

    ``awaiting_response`` outlives an interrupted or errored tick (it is never completed), so only the FIRST
    untyped row at/after the fire is the tick's; every later one is the person's."""
    try:
        row = sdb.execute("SELECT value FROM state_meta WHERE key = ?", (f"loop:{session_id}",)).fetchone()
        state = json.loads(row[0]) if row and row[0] else None
        if not isinstance(state, dict) or not state.get("awaiting_response"):
            return None
        if not str(state.get("prompt") or "").lstrip().startswith("/"):
            return None
        return float(state.get("last_fired_at") or 0) or None
    except (sqlite3.Error, TypeError, ValueError):
        return None


def _latest_user_message(session_id: str, db_path: Optional[str] = None) -> Optional[tuple[int, float]]:
    """``(message_id, timestamp)`` of the newest row in ``state.db`` the session's person typed (the user turn is
    persisted before the first model call, so it is there while the turn's tools run)."""
    if db_path is None:
        from hermes_constants import get_hermes_home
        db_path = os.path.join(str(get_hermes_home()), "state.db")
    if not os.path.exists(db_path):
        return None
    uri = f"file:{db_path}?mode=ro"
    kinds = ", ".join("?" for _ in _TYPED_KINDS)
    sql = ("SELECT id, timestamp, content, COALESCE(display_kind, '') FROM messages WHERE session_id = ? "
           f"AND role = 'user' AND (COALESCE(display_kind, '') = '' OR display_kind IN ({kinds})) "
           "AND COALESCE(_compressed_summary, 0) = 0 ORDER BY id DESC")
    prefixes = _synthetic_prefixes()
    try:
        with sqlite3.connect(uri, uri=True, timeout=5) as sdb:  # noqa: kanban-lockguard (state.db, read-only)
            tick_id = None
            if (loop_since := _slash_loop_tick_since(sdb, session_id)) is not None and sdb.execute(
                    "SELECT 1 FROM messages WHERE session_id = ? AND role = 'user' AND display_kind = 'loop_tick' "
                    "AND timestamp >= ? LIMIT 1", (session_id, loop_since - 1)).fetchone() is None:
                # a tick written before loop_tick typing (a typed one leaves every untyped row to the person):
                # the oldest untyped, uncompressed, non-synthetic row at/after the fire is the tick's own prompt
                for row_id, row_content in sdb.execute(
                        "SELECT id, content FROM messages WHERE session_id = ? AND role = 'user' "
                        "AND COALESCE(display_kind, '') = '' AND COALESCE(_compressed_summary, 0) = 0 "
                        "AND timestamp >= ? ORDER BY id ASC", (session_id, loop_since - 1)):
                    if not str(row_content or "").lstrip().startswith(prefixes):
                        tick_id = int(row_id)
                        break
            for message_id, at, content, _kind in sdb.execute(sql, (session_id, *_TYPED_KINDS)):
                if str(content or "").lstrip().startswith(prefixes):
                    continue
                if tick_id is not None and int(message_id) == tick_id:
                    continue
                return int(message_id), float(at)
    except sqlite3.Error:
        return None
    return None


def _already_relayed(conn: sqlite3.Connection, task_id: str, session: str, message_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'commented' "
        "AND json_extract(payload, '$.relayed_human.session') = ? "
        "AND json_extract(payload, '$.relayed_human.message_id') = ? LIMIT 1",
        (task_id, session, int(message_id))).fetchone() is not None


def admit(conn: sqlite3.Connection, task_id: str, relay: dict) -> None:
    """The card holder's checks on any relay before it is stored (``add_comment`` calls this inside its write
    transaction): the card asked, the message is not older than its latest ask, and this ``(session,
    message_id)`` was not relayed to the card before. :class:`RelayRefused` otherwise."""
    sid, message_id, at = str(relay["session"]), int(relay["message_id"]), int(relay["at"])
    ask = _latest_ask(conn, task_id)
    if ask is None:
        raise RelayRefused(f"{task_id} never asked (no blocked or scheduled park): nothing to answer")
    skew = REMOTE_CLOCK_SKEW_SECONDS if relay.get("via") == "remote" else 0
    if at < ask - skew:
        raise RelayRefused(f"the relayed user message (at {at}) predates {task_id}'s latest ask (at {ask}); "
                           "relay only a reply given after the card asked")
    if _already_relayed(conn, task_id, sid, message_id):
        raise RelayRefused(f"user message {message_id} was already relayed to {task_id}; one relay per user turn")


def verify(conn: sqlite3.Connection, task_id: str, *, session_id: Optional[str],
           state_db: Optional[str] = None, latest_user: Optional[tuple[int, float]] = None) -> dict:
    """The ``relayed_human`` payload for a relay from ``session_id`` to ``task_id``; :class:`RelayRefused` if
    it cannot be proven. ``latest_user`` overrides the ``state.db`` read (cross-gateway origin, tests)."""
    if os.environ.get("HERMES_KANBAN_TASK"):
        raise RelayRefused("relays_user_message is for a chat passing on the user's reply; a Kanban worker "
                           "has no user turn to relay")
    sid = str(session_id or "").strip()
    if not sid:
        raise RelayRefused("relays_user_message needs an interactive chat session (no session id here)")
    msg = latest_user if latest_user is not None else _latest_user_message(sid, state_db)
    if msg is None:
        raise RelayRefused(f"session {sid} has no user message to relay")
    message_id, at = int(msg[0]), float(msg[1])
    ask = _latest_ask(conn, task_id)
    if ask is None:
        raise RelayRefused(f"{task_id} never asked (no blocked or scheduled park): nothing to answer")
    if at <= ask:
        raise RelayRefused(f"the newest user message in session {sid} predates {task_id}'s latest ask; "
                           "relay only a reply given after the card asked")
    if _already_relayed(conn, task_id, sid, message_id):
        raise RelayRefused(f"user message {message_id} was already relayed to {task_id}; one relay per user turn")
    return {"session": sid, "message_id": message_id, "at": int(at)}


def from_tool_args(conn: sqlite3.Connection, task_id: str, args: dict, session_id: Optional[str], *,
                   reject: type[Exception]) -> Optional[dict]:
    """``kanban_comment``'s ``relays_user_message``: the verified ``relayed_human`` when the flag is set,
    ``None`` when not; a refusal is raised as ``reject`` (the tool's rejection type)."""
    flag = args.get("relays_user_message")
    if not (flag is True or str(flag).strip().lower() == "true"):
        return None
    try:
        return verify(conn, task_id, session_id=session_id)
    except RelayRefused as refused:
        raise reject(f"relays_user_message refused: {refused}. Post it without the flag if it is not the "
                     "user's reply.") from None


def from_remote(raw: Any) -> Optional[dict]:
    """A cross-gateway relay's field, normalized (shape only: ``admit`` checks it against the card when it is
    stored); ``None`` when absent/malformed."""
    if not isinstance(raw, dict):
        return None
    sid = str(raw.get("session") or "").strip()[:200]
    try:
        message_id, at = int(str(raw.get("message_id"))), int(str(raw.get("at")))
    except (TypeError, ValueError):
        return None
    if not sid:
        return None
    return {"session": sid, "message_id": message_id, "at": at, "via": "remote"}


def relay_suspect(conn: sqlite3.Connection, task_id: str, *, now: int) -> Optional[dict]:
    """Detection for a relay that was not marked: the card's last park was a ``needs_input`` block that a
    chat (not the card's own run, not a human shell) unblocked within ``SUSPECT_WINDOW_SECONDS``, with no
    relayed comment since. ``{"blocked_at", "unblocked_at"}`` or ``None``."""
    blocked = conn.execute("SELECT payload, created_at FROM task_events WHERE task_id = ? AND kind = 'blocked' "
                           "ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    if not blocked:
        return None
    try:
        payload = json.loads(blocked[0] or "{}")
    except ValueError:
        return None
    if not isinstance(payload, dict) or payload.get("kind") != "needs_input":
        return None
    unblocked = conn.execute("SELECT payload, created_at FROM task_events WHERE task_id = ? AND kind = 'unblocked' "
                             "AND created_at >= ? ORDER BY id DESC LIMIT 1", (task_id, blocked[1])).fetchone()
    if not unblocked or now - int(unblocked[1]) > SUSPECT_WINDOW_SECONDS:
        return None
    try:
        up = json.loads(unblocked[0] or "{}")
    except ValueError:
        up = {}
    if isinstance(up, dict) and str(up.get("actor") or "") == "user":
        return None  # a human shell unblocked it
    relayed = conn.execute("SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'commented' AND created_at >= ? "
                           "AND json_extract(payload, '$.relayed_human') IS NOT NULL LIMIT 1",
                           (task_id, blocked[1])).fetchone()
    if relayed:
        return None
    return {"blocked_at": int(blocked[1]), "unblocked_at": int(unblocked[1])}
