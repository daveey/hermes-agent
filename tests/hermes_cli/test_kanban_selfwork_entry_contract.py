"""Isolated entry contracts; no live receiver or board calls."""
import json
import sqlite3
from contextlib import closing, contextmanager

import pytest

from hermes_cli import kanban_selfwork_exception as sx


@pytest.mark.parametrize("case", ["wrong_task", "extra_type", "missing_row", "null_ask", "list_ask", "empty_ask", "old_version", "valid"])
def test_missing_local_adapter_contract(case):
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.execute("CREATE TABLE tasks(id TEXT, human_request TEXT)")
        ask = {sx.STAMP: {}}
        if case == "list_ask":
            ask = []
        if case == "empty_ask":
            ask = {}
        if case != "missing_row":
            conn.execute("INSERT INTO tasks VALUES (?,?)", (sx.TARGET.owner_task,
                         None if case == "null_ask" else json.dumps(ask)))
        extra = {"user_steps_projection": {}, sx.GRANT: {"local_version": 1}}
        if case in ("old_version", "empty_ask"):
            # Keep the version predicate true to isolate stamp presence.
            extra[sx.GRANT]["local_version"] = 0
        if case == "extra_type":
            extra = ["user_steps_projection"]
        if case == "wrong_task":
            conn.execute("INSERT INTO tasks VALUES (?,?)", ("fixture-other", json.dumps(ask)))
            extra.pop(sx.GRANT)
        task = "fixture-other" if case == "wrong_task" else sx.TARGET.owner_task
        assert sx.missing_local_adapter(conn, task, extra) is (case == "old_version")


@pytest.mark.parametrize("argv", [[], ["unknown"], ["recover-local"], ["recover-local", "fixture", "extra"], ["status", "extra"]])
def test_invalid_cli_never_opens_board(monkeypatch, capsys, argv):
    from hermes_cli import kanban_card_answer_runtime as runtime

    def forbidden(*args, **kwargs):
        pytest.fail("invalid argv reached board lookup")

    monkeypatch.setattr(runtime, "_board_db", forbidden)
    assert sx.main(argv) == 64
    assert json.loads(capsys.readouterr().out) == {"ok": False, "error": "usage"}


def test_recovery_cli_dispatches_exact_operation(monkeypatch, capsys):
    from hermes_cli import kanban_card_answer_runtime as runtime
    from hermes_cli import kanban_card_answer_transport as transport
    from hermes_cli import kanban_db_connect as db
    from hermes_cli import kanban_selfwork_local as local

    sentinel, records = object(), object()
    calls = []

    @contextmanager
    def connect(path):
        assert path == "fixture-only"
        yield sentinel

    def recover(conn, source, operation):
        assert conn is sentinel and source is records
        calls.append(operation)
        return {"fixture": operation}

    monkeypatch.setattr(runtime, "_board_db", lambda board: "fixture-only")
    monkeypatch.setattr(db, "connect_closing", connect)
    monkeypatch.setattr(transport, "Records", lambda: records)
    monkeypatch.setattr(local, "recover", recover)
    assert sx.main(["recover-local", "fixture-operation"]) == 0
    assert calls == ["fixture-operation"]
    assert json.loads(capsys.readouterr().out) == {"ok": True, "result": {"fixture": "fixture-operation"}}


def test_reconsider_keeps_source_custody_during_receive(monkeypatch):
    from hermes_cli import kanban_selfwork_local_source as source

    grant = {"local_version": 1, "operationId": "fixture-operation"}
    held = []

    @contextmanager
    def custody(operation, *, delivered):
        assert operation == "fixture-operation" and delivered is True
        held.append(operation)
        try:
            yield
        finally:
            held.clear()

    def receive(envelope, actual_grant):
        assert held == ["fixture-operation"]
        assert actual_grant is grant and envelope == {"fixture": True}
        return {"accepted": True}, None

    monkeypatch.setattr(sx, "_home", lambda conn: None)
    monkeypatch.setattr(sx, "_live_grant", lambda conn: grant)
    monkeypatch.setattr(sx, "original", lambda records: {"fixture": True})
    monkeypatch.setattr(source, "custody", custody)
    monkeypatch.setattr(sx, "_receive", receive)
    assert sx.reconsider(None, None) == {"accepted": True}
    assert held == []


def test_remote_status_does_not_validate_local_authority(monkeypatch):
    from hermes_cli import kanban_selfwork_local as local

    def forbidden(*args, **kwargs):
        pytest.fail("remote grant entered local verifier")

    grant = {"offer_uid": "fixture-offer", "operationId": "fixture-operation"}
    monkeypatch.setattr(sx, "_live_grant", lambda conn: grant)
    monkeypatch.setattr(sx, "_own", lambda conn, kind: [])
    monkeypatch.setattr(local, "validate", forbidden)
    with closing(sqlite3.connect(":memory:")) as conn:
        assert sx.status(conn)["local_authority"] is None
