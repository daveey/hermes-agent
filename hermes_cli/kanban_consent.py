"""Core consent provenance for card-scoped approval stops.

The approval decision is durable in the existing state DB; created events carry
its snapshot to workers. This is authorization state, not a delivery receipt.
Ordinary user turns and elapsed time never remove a stop.

Where stops are enforced: at submission (the requester card's created snapshot
plus the submitting session's own host-local stops) and again at the home
executor (the created snapshot, read from the replica; an unsynced created event
is retried, then refused). Cards created by a session inherit its stops even when
they carry another origin session_id. A session's stops include its
``parent_session_id`` ancestors, so context compression (which rotates the
session id) keeps them. A dispatched worker's ``kanban_comment`` tool checks the
same stops before a direct same-board comment (``comment_refusal``), and
``kanban edit`` cannot add a delivery claim the completion gate would refuse.

Known limits: a stop recorded in a session after a card was created does not
reach that card; the home cannot see a submitting session's host-local stops
(only the submitting host checks them); a chat's own ``kanban_comment`` calls
are not fenced (its user is present to approve or stop the turn); a worker's
terminal ``hermes kanban comment`` goes through command approval, not this check.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shlex
import time

from hermes_constants import get_hermes_home

_TARGET = re.compile(r"\bt_[0-9a-f]{8}(?:[0-9a-f]{8})?\b")
def _comment_targets(command):
    """Parse supported direct CLI invocations, never IDs inside body text.

    This only records stops; it cannot create grants. Shell wrappers and other
    approval transports must supply normalized operation identity separately.
    """
    if not isinstance(command, str):
        return []
    try:
        argv = shlex.split(command)
    except ValueError:
        return []
    if len(argv) < 4 or os.path.basename(argv[0]) != "hermes" or argv[1] != "kanban":
        return []
    args = argv[2:]
    if args[:1] == ["--board"] and len(args) >= 3:
        args = args[2:]
    if len(args) >= 2 and args[0] == "comment" and _TARGET.fullmatch(args[1]):
        return [args[1]]
    return []



_GRANTS = ("once", "session", "always")


def observe_approval(payload):
    """Consume typed human outcomes, never denial-looking tool output.

    Timeout/deny records a stop. Only a later explicit human approval of the
    same action on the same target supersedes it (stop history stays); an
    ordinary chat turn, elapsed time or a different target never does.
    """
    choice = payload.get("choice")
    session = payload.get("session_id") or payload.get("session_key")
    command = payload.get("command") or ""
    if choice not in ("timeout", "deny", *_GRANTS) or not session:
        return
    targets = _comment_targets(command)
    if not targets:
        return
    outcome = {"timeout": "timeout", "deny": "denied"}.get(choice, "approved")
    try:
        _record(session, {"targets": targets, "outcome": outcome, "action": "comment",
                          "at": time.time(), "origin_session": session})
    except Exception:
        if outcome != "approved":
            raise  # a lost stop would let delegation act on a genuine non-consent: fail closed
        # A lost grant only leaves the earlier stop in force; never block the approved command.
        logging.getLogger(__name__).warning("kanban consent: could not record approval for %s", targets,
                                            exc_info=True)


def _record(session, stop):
    from hermes_state import SessionDB
    db = SessionDB(get_hermes_home() / "state.db")
    try:
        def record(conn):
            conn.execute("CREATE TABLE IF NOT EXISTS kanban_consent_stops ("
                         "id INTEGER PRIMARY KEY, origin_session TEXT NOT NULL, payload TEXT NOT NULL)")
            conn.execute("INSERT INTO kanban_consent_stops(origin_session,payload) VALUES (?,?)",
                         (session, json.dumps(stop)))
        db._execute_write(record)
    finally:
        db.close()


def _active(decisions):
    """Stops still in force after explicit same-target approvals, in order."""
    active = []
    for d in decisions:
        if d.get("outcome") == "approved":
            granted = set(d.get("targets") or [])
            active = [{**s, "targets": [t for t in s["targets"] if t not in granted]} for s in active]
            active = [s for s in active if s["targets"]]
        else:
            active.append(d)
    return active


def session_stops(session):
    if not session or not (get_hermes_home() / "state.db").exists():
        return []
    from hermes_state import SessionDB
    db = SessionDB(get_hermes_home() / "state.db", read_only=True)
    try:
        if not db._read_one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='kanban_consent_stops'"):
            return []
        lineage = _lineage(db, session)
        return _active([json.loads(r[0]) for r in db._read_all(
            f"SELECT payload FROM kanban_consent_stops WHERE origin_session IN ({','.join('?' * len(lineage))}) "
            "ORDER BY id", tuple(lineage))])
    finally:
        db.close()


_LINEAGE_MAX = 64


def _lineage(db, session):
    """*session* and its ``parent_session_id`` ancestors (compression rotates the id mid-conversation).

    Decisions of the whole lineage replay in record order, so a stop recorded
    before compression binds the tip and a later same-target approval still
    supersedes it. Bounded and cycle-safe.
    """
    chain = [session]
    if not db._read_one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sessions'"):
        return chain
    while len(chain) < _LINEAGE_MAX:
        row = db._read_one("SELECT parent_session_id FROM sessions WHERE id=?", (chain[-1],))
        parent = row[0] if row else None
        if not parent or parent in chain:
            break
        chain.append(parent)
    return chain


def _created_stops(conn, task_id):
    """The stops stamped on *task_id*'s created event; None when that event is absent."""
    if not task_id:
        return []
    row = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='created' ORDER BY id LIMIT 1",
                       (task_id,)).fetchone()
    return None if row is None else json.loads(row[0] or "{}").get("consent_stops", [])


def creation_stops(conn, task_id):
    """Local-board read: a card with no created event here carries no stops (the home fails closed)."""
    return _created_stops(conn, task_id) or []


def current_session():
    """The request-scoped session (what ``_create_session_id`` binds), never another chat's process env."""
    from gateway.session_context import get_session_env
    return get_session_env("HERMES_SESSION_ID") or None


def snapshot(conn, task_id, creator_task_id):
    """Creator stops + the card's origin session + the creating session (a worker run's own stops).

    Children of a worker card inherit the parent's origin-chat session_id, so the
    worker run's session is added explicitly: a stop it recorded binds them too.
    """
    row = conn.execute("SELECT session_id FROM tasks WHERE id=?", (task_id,)).fetchone()
    current = current_session()
    sessions = dict.fromkeys(s for s in ((row[0] if row else None) or current, current) if s)
    stops = list(creation_stops(conn, creator_task_id))
    for session in sessions:
        stops += session_stops(session)
    return list({json.dumps(s, sort_keys=True): s for s in stops}.values())


def comment_refusal(conn, task_id, session, target):
    """Refusal text when a direct board comment on *target* would bypass a stop, else None.

    The ``kanban_comment`` tool of a dispatched worker checks its own card's
    creation stops and its run session's stops (lineage included): a same-board
    target needs no operator call, so this is that path's fence.
    """
    from hermes_cli.kanban_supervisor_requests import SupervisorRequestError
    try:
        _check(list(creation_stops(conn, task_id)) + session_stops(session),
               [{"op": "operator", "action": "comment", "task_id": target}])
    except SupervisorRequestError as exc:
        return str(exc)
    return None


def _check(stops, ops):
    from hermes_cli.kanban_supervisor_requests import SupervisorRequestError
    for stop in stops:
        for op in ops:
            if (op.get("op") == "operator" and op.get("action") == "comment"
                    and op.get("task_id") in stop.get("targets", [])):
                raise SupervisorRequestError("consent_stop", "the originating approval did not consent to this "
                                             "target; delegation does not authorize it")


def enforce(conn, requester, ops, session=None):
    """Submission check: the requester's creation stops plus the submitting session's own stops.

    The home re-checks the creation stops (``home_refusal``) and fails closed when
    it cannot read them; this local check reads the caller's own board.
    """
    _check(list(creation_stops(conn, requester)) + session_stops(session), ops)


# How long the home waits for a caller's created event to reach its replica before refusing.
CREATION_SYNC_GRACE_S = 600


def home_refusal(rep, payload, target):
    """``(refusal, retry)`` for the home executor, from the caller's created event in the replica.

    A missing created event (sync lag) is retried like an unreachable Board, then
    refused after ``CREATION_SYNC_GRACE_S``: the stops cannot be checked, so the
    call is never applied on an empty read. Known limit: the submitting session's
    host-local stops are checked only at submission (``enforce``), never here.
    """
    from hermes_cli.kanban_supervisor_requests import SupervisorRequestError
    if payload.get("action") != "comment":
        return None, False  # stops only bind comments; other actions need no provenance read
    caller = (payload.get("caller") or {}).get("task_id")
    stops = _created_stops(rep, caller)
    if stops is None:
        at = payload.get("requested_at")
        if isinstance(at, (int, float)) and time.time() - at <= CREATION_SYNC_GRACE_S:
            return None, True
        return (f"consent_unproven: the created event of {caller} never reached this replica, so its "
                "approval stops cannot be checked"), False
    try:
        _check(stops, [{"op": "operator", "action": payload.get("action"), "task_id": target}])
    except SupervisorRequestError as exc:
        return str(exc), False
    return None, False
