"""Core consent provenance for card-scoped approval stops.

The approval decision is durable in the existing state DB; created events carry
its snapshot to workers. This is authorization state, not a delivery receipt.
Ordinary user turns and elapsed time never remove a stop.
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
        return _active([json.loads(r[0]) for r in db._read_all(
            "SELECT payload FROM kanban_consent_stops WHERE origin_session=? ORDER BY id", (session,))])
    finally:
        db.close()


def creation_stops(conn, task_id):
    if not task_id:
        return []
    row = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='created' ORDER BY id LIMIT 1",
                       (task_id,)).fetchone()
    return json.loads(row[0] or "{}").get("consent_stops", []) if row else []


def snapshot(conn, task_id, creator_task_id):
    row = conn.execute("SELECT session_id FROM tasks WHERE id=?", (task_id,)).fetchone()
    session = (row[0] if row else None) or os.environ.get("HERMES_SESSION_ID")
    stops = creation_stops(conn, creator_task_id) + session_stops(session)
    return list({json.dumps(s, sort_keys=True): s for s in stops}.values())


def enforce(conn, requester, ops):
    from hermes_cli.kanban_supervisor_requests import SupervisorRequestError
    for stop in creation_stops(conn, requester):
        for op in ops:
            if (op.get("op") == "operator" and op.get("action") == "comment"
                    and op.get("task_id") in stop.get("targets", [])):
                raise SupervisorRequestError("consent_stop", "the originating approval did not consent to this "
                                             "target; delegation does not authorize it")
