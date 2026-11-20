"""Supersession settles a notice, not the unanswered turn preceding it."""
import json
import time

import pytest

import hermes_state_outcome as so
from tui_gateway import notice_guarantee as ng
from tui_gateway import notice_history as nh
from tests.tui_gateway import test_notice_guarantee as helpers

CHAT = helpers.CHAT
_closeout = helpers._closeout
_notice = helpers._notice
_run = helpers._run
db = helpers.db


def _settled(db, text):
    return _notice(db, text, closeout={"state": "superseded", "settled_content_sha256": nh.content_digest(text)})


def _retired_block(db, *, extra="", replay=False):
    import contextlib
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tui_gateway.session_notifications import _format_kanban_event_text

    with contextlib.closing(kbc.connect()) as conn:
        tid = kb.create_task(conn, title="retired ask", assignee="worker")
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "blocked", {"reason": "old failure", "kind": "dependency"})
            event = kb.list_events(conn, tid)[-1]
            conn.execute("UPDATE tasks SET status='blocked', block_kind='dependency' WHERE id=?", (tid,))
        task = kb.get_task(conn, tid)
        text = _format_kanban_event_text({"task_id": tid}, task, event, "default")
        keys = [f"default:{event.id}"]
        if extra == "batch":
            other = kb.create_task(conn, title="unrelated work", assignee="worker")
            with kb.write_txn(conn):
                kb._append_event(conn, other, "completed", {"summary": "Needs an independent verification."})
            e2 = kb.list_events(conn, other)[-1]
            text += "\n" + _format_kanban_event_text({"task_id": other}, kb.get_task(conn, other), e2, "default")
            keys.append(f"default:{e2.id}")
        elif extra:
            text += "\nPlease answer an unrelated human request."
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='running', block_kind=NULL WHERE id=?", (tid,))
    meta = {"board": "default", "task_id": tid, "kanban_event_keys": keys, "stale": True}
    if replay:
        from types import SimpleNamespace
        from tui_gateway.automation_replay import replay_payload
        items = [SimpleNamespace(text=text, display_kind="kanban_notice", display_metadata=meta,
                                 failed_at=time.time() - 600, cause="test failure", attempt=0),
                 SimpleNamespace(text="An unrelated process needs inspection.", display_kind="process_complete",
                                 display_metadata={}, failed_at=time.time() - 600, cause="test failure", attempt=0)]
        text, kind, meta = replay_payload(items)
        assert kind == "kanban_notice"
    return _notice(db, text, **meta)


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
    _retired_block(db)
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
    _settled(db, "An outdated blocked notice.")
    _out, calls = _run(db, verdict="busy")
    assert calls == [(CHAT, old)]
    assert _closeout(db, old) == "deferred"
    meta = json.loads(db._read_one("SELECT display_metadata FROM messages WHERE id = ?", (old,))[0])
    _out, calls = _run(db, now=meta["closeout"]["retry_at"] + 1)
    assert calls == [(CHAT, old)]


@pytest.mark.parametrize("only", [None, {CHAT}])
@pytest.mark.parametrize("settled_tail", [False, True])
def test_long_deferred_retry_survives_discovery_lookback(db, only, settled_tail):
    now = time.time()
    retry_at = now + ng.LOOKBACK_S + 3600
    old = _notice(db, "A durable close-out awaits quota reset.",
                  closeout={"state": "deferred", "retry_at": retry_at, "attempts": 1})
    if settled_tail:
        _retired_block(db)
        out, calls = _run(db, now=now)
        assert calls == [] and out[0]["result"] == "superseded"
    assert ng.candidates(db, retry_at - 1, only=only) == []
    assert [c["msg_id"] for c in ng.candidates(db, retry_at + 1, only=only)] == [old]


@pytest.mark.parametrize("case", ["ci", "running", "chat", "composite", "remote", "missing", "wrong_kind", "bad_id",
                                  "missing_park", "missing_awaiting", "malformed_park", "missing_event", "missing_board", "old_park"])
def test_creator_decision_supersession_requires_current_local_proof(db, case):
    import contextlib

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tui_gateway.session_notifications import _format_kanban_event_text

    db.append_message(CHAT, "assistant", "The requested audit finished and every check passed with no changes required.")
    with contextlib.closing(kbc.connect()) as conn:
        tid = kb.create_task(conn, title="audit delivery", assignee="worker")
        with kb.write_txn(conn):
            if case == "old_park":
                kb._append_event(conn, tid, "scheduled", {"awaiting": {"kind": "ci", "ref": "obsolete"}})
            kb._append_event(conn, tid, "decision_requested", {"why": "Repair or withdraw?"})
            event = kb.list_events(conn, tid)[-1]
            awaiting = {"kind": "chat" if case == "chat" else "ci", "ref": "test-run"}
            if case not in ("missing_park", "old_park"):
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
    _settled(db, "Already settled blocked notice.")
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
    row = {"role": "user", "content": "settled", "display_kind": "kanban_notice",
           "display_metadata": {"closeout": {"state": state, "settled_content_sha256": nh.content_digest("settled")}}}
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
            _settled(db, "Settled intermediate notice.")
    _settled(db, "Settled physical tail.")
    found = ng.candidates(db, time.time(), only=only)
    assert [c["msg_id"] for c in found] == ([old] if boundary == "chain" else [])


@pytest.mark.parametrize("shape", ["batch", "folded", "replay", "legacy", "changed_after_receipt"])
def test_unproven_supersession_never_hides_whole_row(db, shape):
    old = _notice(db, "Older unrelated ask.")
    row = _retired_block(db, extra="batch" if shape == "legacy" else shape if shape in ("batch", "folded") else "",
                         replay=shape == "replay")
    if shape == "legacy":
        ng._stamp(db, row, "superseded")
    else:
        _run(db)
    if shape == "changed_after_receipt":
        db._write_rowcount("UPDATE messages SET content = content || ? WHERE id = ?",
                           ("\nUnrelated human request folded later.", row))
    stored = dict(db._read_one("SELECT role, content, display_kind, display_metadata FROM messages WHERE id=?", (row,)))
    assert so._completion_note(stored) is None
    assert nh.settled_notice(stored) is False
    assert old not in [c["msg_id"] for c in ng.candidates(db, time.time())]
    assert so.refresh_outcomes_quietly([CHAT], "notice", db=db) == 1
    assert so.get_outcome(CHAT, db=db)["reason"] == "notice_tail"


@pytest.mark.parametrize("failure", ["event_read", "task_read", "park_read"])
def test_creator_source_read_failure_remains_retryable(db, monkeypatch, failure):
    import contextlib
    import sqlite3
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tui_gateway.session_notifications import _format_kanban_event_text

    with contextlib.closing(kbc.connect()) as conn:
        tid = kb.create_task(conn, title="creator question", assignee="worker")
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "decision_requested", {"why": "Repair or withdraw?"})
            event = kb.list_events(conn, tid)[-1]
            kb._append_event(conn, tid, "scheduled", {"awaiting": {"kind": "ci", "ref": "test-run"}})
            conn.execute("UPDATE tasks SET status='scheduled' WHERE id=?", (tid,))
        text = _format_kanban_event_text({"task_id": tid}, kb.get_task(conn, tid), event, "default")
    row = _notice(db, text, board="default", task_id=tid, event_ids=[event.id])
    original_connect, original_get = kbc.connect, kb.get_task
    failed = []

    class Source:
        def __init__(self, conn):
            self.conn = conn

        def __getattr__(self, key):
            return getattr(self.conn, key)

        def execute(self, sql, args=()):
            target = "SELECT * FROM task_events" if failure == "event_read" else "SELECT payload FROM task_events"
            if failure != "task_read" and target in sql and not failed:
                failed.append(failure)
                raise sqlite3.OperationalError("injected source read failure")
            return self.conn.execute(sql, args)

    def get_task(conn, task_id):
        if failure == "task_read" and not failed:
            failed.append(failure)
            raise sqlite3.OperationalError("injected task read failure")
        return original_get(conn, task_id)

    monkeypatch.setattr(kbc, "connect", lambda *args, **kwargs: Source(original_connect(*args, **kwargs)))
    monkeypatch.setattr(kb, "get_task", get_task)
    _out, calls = _run(db, verdict="busy")
    assert failed == [failure]
    assert calls == [(CHAT, row)]
    assert _closeout(db, row) == "deferred"  # not the guarantee's terminal error stamp
    retry = json.loads(db._read_one("SELECT display_metadata FROM messages WHERE id=?", (row,))[0])["closeout"]["retry_at"]
    out, calls = _run(db, now=retry + 1)
    assert calls == [] and out[0]["result"] == "superseded"


def test_provable_legacy_supersession_is_lazily_upgraded(db):
    older = _notice(db, "Still awaiting another answer.")
    row = _retired_block(db)
    ng._stamp(db, row, "superseded")
    out, calls = _run(db)
    assert calls == [] and out[0]["msg_id"] == row and out[0]["result"] == "superseded"
    assert ng.candidates(db, time.time())[0]["msg_id"] == older
    assert so.get_outcome(CHAT, db=db)["reason"] == "notice_tail"


@pytest.mark.parametrize("failure", [OSError, TypeError, AttributeError, RuntimeError])
def test_creator_unavailable_source_cannot_become_final_error(db, monkeypatch, failure):
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli.kanban_decision_ask import notice_text

    cand = {"sid": CHAT, "content": notice_text({}),
            "meta": {"board": "default", "task_id": "t_example", "event_ids": [1]}}

    def unavailable(**kwargs):
        raise failure("injected unavailable source")

    monkeypatch.setattr(kbc, "connect", unavailable)
    assert nh.creator_superseded(cand) is False


@pytest.mark.parametrize("case", ["valid", "remote", "missing_keys", "not_list", "bad_type", "duplicate", "too_many",
                                  "bad_key", "missing_event", "missing_board", "wrong_kind", "still_blocked", "missing_task",
                                  "null_payload", "read_error"])
def test_whole_row_receipt_requires_all_local_source_proof(db, monkeypatch, case):
    import contextlib
    from hermes_cli import kanban_db_connect as kbc

    row = _retired_block(db)
    cand = ng.candidates(db, time.time())[0]
    assert cand["msg_id"] == row
    meta = cand["meta"]
    if case == "remote":
        meta["gateway"] = "foreign"
    elif case == "missing_keys":
        meta.pop("kanban_event_keys")
    elif case == "not_list":
        meta["kanban_event_keys"] = "default:1"
    elif case == "bad_type":
        meta["kanban_event_keys"] = [True]
    elif case == "duplicate":
        meta["kanban_event_keys"] *= 2
    elif case == "too_many":
        meta["kanban_event_keys"] = [f"default:{n}" for n in range(51)]
    elif case == "bad_key":
        meta["kanban_event_keys"] = ["not-a-local-event"]
    elif case == "missing_event":
        meta["kanban_event_keys"] = ["default:9999999"]
    elif case == "missing_board":
        meta["kanban_event_keys"] = ["missing-board:1"]
    elif case == "read_error":
        def unavailable(**kwargs):
            raise OSError("injected source unavailable")
        monkeypatch.setattr(kbc, "connect", unavailable)
    elif case != "valid":
        with contextlib.closing(kbc.connect()) as conn:
            eid = int(meta["kanban_event_keys"][0].split(":")[-1])
            if case == "wrong_kind":
                conn.execute("UPDATE task_events SET kind='completed' WHERE id=?", (eid,))
            elif case == "still_blocked":
                conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (meta["task_id"],))
            elif case == "missing_task":
                from hermes_cli import kanban_db as kb
                monkeypatch.setattr(kb, "get_task", lambda *_: None)
            else:
                conn.execute("UPDATE task_events SET payload='null' WHERE id=?", (eid,))
            conn.commit()
    assert nh.whole_row_settled(cand) is (case == "valid")


