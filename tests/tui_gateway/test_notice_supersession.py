"""Supersession settles a notice, not the unanswered turn preceding it."""
import json
import time

import pytest

import hermes_state_outcome as so
from tui_gateway import notice_guarantee as ng
from tests.tui_gateway import test_notice_guarantee as helpers

CHAT = helpers.CHAT
_closeout = helpers._closeout
_notice = helpers._notice
_run = helpers._run
db = helpers.db


@pytest.mark.parametrize("earlier", ["reply", "request", "notice", "failure"])
def test_superseded_tail_preserves_preceding_obligation(db, earlier):
    if earlier == "reply":
        db.append_message(CHAT, "assistant", "The audit finished and all requested checks passed with no work remaining.")
    elif earlier == "request":
        db.append_message(CHAT, "user", "Please inspect another card.")
    elif earlier == "notice":
        old = _notice(db, "An unrelated card needs an agent decision.")
    else:
        db.append_message(CHAT, "assistant", "The turn failed.", finish_reason="error")
    _notice(db, "An outdated remote blocked notice.", stale=True)
    out, calls = _run(db)
    assert calls == [] and out[0]["result"] == "superseded"
    outcome = so.get_outcome(CHAT, db=db)
    if earlier == "reply":
        assert outcome["kind"] == "done"
    else:
        assert outcome["kind"] == "unresolved"
        assert outcome["reason"] == {"request": "interrupted", "notice": "notice_tail", "failure": "turn_failed"}[earlier]
    if earlier == "notice":
        assert ng.candidates(db, time.time())[0]["msg_id"] == old


def test_preceding_deferred_notice_is_retried_beneath_superseded_tail(db):
    old = _notice(db, "An unrelated card finished.")
    _notice(db, "An outdated blocked notice.", closeout={"state": "superseded"})
    _out, calls = _run(db, verdict="busy")
    assert calls == [(CHAT, old)]
    assert _closeout(db, old) == "deferred"
    meta = json.loads(db._read_one("SELECT display_metadata FROM messages WHERE id = ?", (old,))[0])
    _out, calls = _run(db, now=meta["closeout"]["retry_at"] + 1)
    assert calls == [(CHAT, old)]


@pytest.mark.parametrize("case", ["ci", "running", "chat", "composite", "remote", "missing", "wrong_kind", "bad_id",
                                  "missing_park", "missing_awaiting", "malformed_park", "missing_event", "missing_board"])
def test_creator_decision_supersession_requires_current_local_proof(db, case):
    import contextlib

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tui_gateway.session_notifications import _format_kanban_event_text

    db.append_message(CHAT, "assistant", "The requested audit finished and every check passed with no changes required.")
    with contextlib.closing(kbc.connect()) as conn:
        tid = kb.create_task(conn, title="audit delivery", assignee="worker")
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "decision_requested", {"why": "Repair or withdraw?"})
            event = kb.list_events(conn, tid)[-1]
            awaiting = {"kind": "chat" if case == "chat" else "ci", "ref": "test-run"}
            if case != "missing_park":
                kb._append_event(conn, tid, "scheduled", {} if case == "missing_awaiting" else {"awaiting": awaiting})
            if case == "malformed_park":
                conn.execute("UPDATE task_events SET payload = 'null' WHERE task_id = ? AND kind = 'scheduled'", (tid,))
            conn.execute("UPDATE tasks SET status = ? WHERE id = ?", ("running" if case == "running" else "scheduled", tid))
        task = kb.get_task(conn, tid)
        text = _format_kanban_event_text({"task_id": tid}, task, event, "default")
        if case == "wrong_kind":
            with kb.write_txn(conn):
                conn.execute("UPDATE task_events SET kind = 'completed' WHERE id = ?", (event.id,))
    meta = {"board": "default", "task_id": tid, "event_ids": [event.id]}
    if case == "remote":
        meta["gateway"] = "foreign-gateway"
    if case == "missing":
        meta["task_id"] = "t_missing"
    if case == "bad_id":
        meta["event_ids"] = [True]
    if case == "missing_event":
        meta["event_ids"] = [999999]
    if case == "missing_board":
        meta["board"] = "missing-board"
    if case == "composite":
        text += "\nAlso investigate an unrelated user's question."
    old = _notice(db, text, **meta)
    _notice(db, "Already settled blocked notice.", closeout={"state": "superseded"})
    out, calls = _run(db)
    if case in ("ci", "running"):
        assert calls == [] and out[0]["result"] == "superseded"
        assert _closeout(db, old) == "superseded"
        assert so.get_outcome(CHAT, db=db)["kind"] == "done"
    else:
        assert calls == [(CHAT, old)]
        assert _closeout(db, old) == "started"


@pytest.mark.parametrize("state", ["superseded", "claimed", "started", "error", "deferred", "busy", "self", None])
def test_only_final_supersession_is_informational(state):
    row = {"role": "user", "display_kind": "kanban_notice", "display_metadata": {"closeout": {"state": state}}}
    assert (so._completion_note(row) is not None) is (state == "superseded")
    row["role"] = "assistant"
    assert so._completion_note(row) is None
    row["role"], row["display_kind"] = "user", "user_text"
    assert so._completion_note(row) is None


@pytest.mark.parametrize("only", [None, {CHAT}])
@pytest.mark.parametrize("boundary", ["assistant", "user", "tool", "failed_notice", "chain", "too_long"])
def test_history_stops_at_nonsettled_boundaries(db, only, boundary):
    old = _notice(db, "Still owed a close-out.")
    if boundary in ("assistant", "user", "tool"):
        row = db.append_message(CHAT, boundary, "A later turn boundary.")
        # Make the boundary old enough for discovery: a young row's grace check
        # would mask a missing history-stop guard.
        db._write_rowcount("UPDATE messages SET timestamp = ? WHERE id = ?", (time.time() - 600, row))
    elif boundary == "failed_notice":
        _notice(db, "A failed close-out remains visible.", closeout={"state": "error"})
    else:
        for _ in range(2 if boundary == "chain" else so._TAIL_ROWS):
            _notice(db, "Settled intermediate notice.", closeout={"state": "superseded"})
    _notice(db, "Settled physical tail.", closeout={"state": "superseded"})
    found = ng.candidates(db, time.time(), only=only)
    assert [c["msg_id"] for c in found] == ([old] if boundary == "chain" else [])


