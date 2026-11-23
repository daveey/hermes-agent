"""Owner-local exact-text edits preserve inactive custody and receipt identity."""
from __future__ import annotations

import copy
import importlib
import json
import sqlite3

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tests.hermes_cli import test_kanban_edit_operator_fields as fixtures

kanban_home = fixtures.kanban_home


def api():
    return importlib.import_module("hermes_cli.kanban_text_edit")


def request(body="old", replacement="new"):
    return {"request_id": "text-test-1", "reason": "Neutral future routing only",
            "expected": {"title": "Text fixture", "body": body},
            "replacement": {"title": "Text fixture", "body": replacement}}


@pytest.mark.parametrize("body,replacement", [("old", "new"), (None, ""), ("", None),
                                               ("é\n", " 字\r\n")])
def test_apply_replay_preserves_nontext_and_history(kanban_home, monkeypatch, body, replacement):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Text fixture", body=body)
        kb.add_comment(conn, tid, "original", "Original history")
        before = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
        comments = list(conn.execute("SELECT * FROM task_comments"))
        seen = []
        monkeypatch.setattr(kb, "notify_task_updated", lambda *a, **kw: seen.append((a, kw)))
        result = api().edit_text(conn, tid, request(body, replacement))
        assert result["disposition"] == "applied" and result["event_uid"]
        after = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
        assert after == {**before, "body": replacement}
        assert list(conn.execute("SELECT * FROM task_comments")) == comments
        assert len(seen) == 1
        events = kb.list_events(conn, tid)
        assert [e.kind for e in events].count("text_cas_edited") == 1
        assert "edited" not in [e.kind for e in events]
        snapshot = list(conn.iterdump())
        replay = api().edit_text(conn, tid, request(body, replacement))
        assert replay["disposition"] == "replayed"
        assert replay["event_uid"] == result["event_uid"]
        assert list(conn.iterdump()) == snapshot and len(seen) == 1


@pytest.mark.parametrize("field", ["title", "body"])
def test_stale_exact_preimage(kanban_home, field):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Text fixture", body="old")
        req = request()
        req["expected"][field] += " "
        snapshot = list(conn.iterdump())
        result = api().edit_text(conn, tid, req)
        assert result["disposition"] == "stale_preimage"
        assert list(conn.iterdump()) == snapshot


@pytest.mark.parametrize("status", ["todo", "ready", "blocked", "scheduled", "triage"])
def test_inactive_states_keep_holds(kanban_home, status):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Text fixture", body="old")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status=?,block_kind='needs_input' WHERE id=?", (status, tid))
        before = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
        assert api().edit_text(conn, tid, request())["ok"]
        assert dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()) == {**before, "body": "new"}


@pytest.mark.parametrize("field,value", [("status", "running"), ("status", "review"),
    ("status", "done"), ("status", "archived"), ("claim_lock", "held"),
    ("claim_expires", 1), ("worker_pid", 123)])
def test_custody_refuses_without_writes(kanban_home, field, value):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Text fixture", body="old")
        with kb.write_txn(conn):
            conn.execute(f"UPDATE tasks SET {field}=? WHERE id=?", (value, tid))
        snapshot = list(conn.iterdump())
        result = api().edit_text(conn, tid, request())
        assert result["disposition"] == "active_custody" and not result["receipt_checked"]
        assert list(conn.iterdump()) == snapshot


def test_conflict_drift_missing_receipt(kanban_home):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Text fixture", body="old")
        assert api().edit_text(conn, tid, request())["ok"]
        req = request(); req["reason"] = "different reason"
        assert api().edit_text(conn, tid, req)["disposition"] == "request_id_conflict"
        kb.edit_task(conn, tid, body="later")
        assert api().edit_text(conn, tid, request())["disposition"] == "replay_state_drift"
        with kb.write_txn(conn):
            conn.execute("DELETE FROM task_events WHERE task_id=? AND kind='text_cas_edited'", (tid,))
        assert api().edit_text(conn, tid, request())["disposition"] == "stale_preimage"


def test_audit_failure_rolls_back(kanban_home, monkeypatch):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Text fixture", body="old")
        before = list(conn.iterdump())
        original = kb._append_event
        def fail(c, task, kind, *a, **kw):
            if kind == "text_cas_edited":
                raise sqlite3.OperationalError("injected audit failure")
            return original(c, task, kind, *a, **kw)
        monkeypatch.setattr(kb, "_append_event", fail)
        assert api().edit_text(conn, tid, request())["disposition"] == "unknown"
        assert list(conn.iterdump()) == before


@pytest.mark.parametrize("key", ["HERMES_KANBAN_TASK", "HERMES_KANBAN_WORKER_TASK"])
def test_worker_denied_before_db(kanban_home, monkeypatch, key):
    monkeypatch.setenv(key, "fixture-worker")
    result = api().edit_text(None, "fixture", request())
    assert result["disposition"] == "worker_refused"


@pytest.mark.parametrize("mutation", ["extra", "noop", "blank", "surrogate", "token", "nulltitle"])
def test_invalid_before_db(kanban_home, mutation):
    req = copy.deepcopy(request())
    if mutation == "extra": req["force"] = True
    if mutation == "noop": req["replacement"] = req["expected"].copy()
    if mutation == "blank": req["replacement"]["title"] = " \n"
    if mutation == "surrogate": req["replacement"]["body"] = "\ud800"
    if mutation == "token": req["request_id"] = "bad token"
    if mutation == "nulltitle": req["expected"]["title"] = None
    assert api().edit_text(None, "fixture", req)["disposition"] == "invalid_request"


def test_stamps_do_not_break_replay(kanban_home):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Text fixture", body="old")
        result = api().edit_text(conn, tid, request())
        row = conn.execute("SELECT id,payload FROM task_events WHERE kind='text_cas_edited'").fetchone()
        payload = json.loads(row["payload"]); payload["stamp"] = "extra"
        with kb.write_txn(conn):
            conn.execute("UPDATE task_events SET payload=? WHERE id=?", (json.dumps(payload), row["id"]))
        assert api().edit_text(conn, tid, request())["event_uid"] == result["event_uid"]
