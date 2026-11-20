"""Supervisor parks must not expose a triage card to a remote dispatcher."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import kanban as cli
from hermes_cli import kanban_continuation as continuation
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatcher


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for key in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_DB",
                "HERMES_KANBAN_BOARD", "HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_SESSION_ID"):
        monkeypatch.delenv(key, raising=False)
    from tests.hermes_cli.kanban_sync_support import write_sync_config
    write_sync_config(home, True)
    kb.init_db()
    with kbc.connect_closing() as conn:
        yield conn


def _triage(conn):
    parent = kb.create_task(conn, title="finished prerequisite", assignee="default")
    assert kb.complete_task(conn, parent, summary="finished")
    tid = kb.create_task(conn, title="remote wait", assignee="default", parents=[parent],
                         triage=True, runs_on=["machine:other"])
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET consecutive_failures=2, last_failure_error='crashed', "
                     "block_kind='transient', block_recurrences=1 WHERE id=?", (tid,))
    return tid


def _park(conn, tid, op, surface):
    args = {"reason": "rate-limit cooldown", "clock_only": True,
            "awaiting": {"kind": "clock", "why": "rate-limit cooldown"}}
    if op == "block":
        args["kind"] = "time"
    if surface == "db":
        return getattr(kb, op + "_task")(conn, tid, **args)
    if surface == "tool":
        from tools import kanban_tools
        result = json.loads(getattr(kanban_tools, "_handle_" + op)({"task_id": tid, **args}))
        assert result.get("ok"), result
        return True
    parser = argparse.ArgumentParser()
    cli.build_parser(parser.add_subparsers(dest="command"))
    argv = ["kanban", op, tid, "rate-limit cooldown", "--clock-only", "--awaiting", '{"kind":"clock","why":"rate-limit cooldown"}']
    if op == "block":
        argv += ["--kind", "time"]
    return cli.kanban_command(parser.parse_args(argv)) == 0


@pytest.mark.parametrize("surface", ["db", "cli", "tool"])
@pytest.mark.parametrize("op", ["schedule", "block"])
def test_triage_parks_atomically_before_dispatch(board, op, surface):
    tid = _triage(board)
    before = len(kb.list_events(board, tid))
    # Observe every status UPDATE, including intermediate states inside a transaction.
    board.executescript("CREATE TABLE status_trace(status TEXT); CREATE TRIGGER trace_status "
                        "AFTER UPDATE OF status ON tasks BEGIN "
                        "INSERT INTO status_trace VALUES (NEW.status); END;")
    assert _park(board, tid, op, surface)
    task = kb.get_task(board, tid)
    assert task.status == "scheduled"
    assert json.loads(board.execute("SELECT runs_on FROM tasks WHERE id=?", (tid,)).fetchone()[0]) == ["machine:other"]
    assert task.consecutive_failures == 0
    assert task.last_failure_error is None
    assert task.block_recurrences == 1
    assert [r[0] for r in board.execute("SELECT status FROM status_trace")] == ["scheduled"]
    events = kb.list_events(board, tid)[before:]
    assert [e.kind for e in events if e.kind != "commented"] == ["scheduled"]
    wake = next(e.payload["wake_at"] for e in events if e.kind == "scheduled")
    assert continuation.wake_due_continuations(board, now=wake - 1) == []
    spawned = []
    dispatcher.dispatch_once(board, spawn_fn=lambda *a, **kw: spawned.append(a))
    assert not spawned
    assert kb.get_task(board, tid).status == "scheduled"
    assert not {"unblocked", "promoted", "claimed"}.intersection(e.kind for e in kb.list_events(board, tid)[before:])
    assert continuation.wake_due_continuations(board, now=wake) == [tid]
    assert kb.get_task(board, tid).status == "ready"


def test_triage_can_be_blocked_for_evidenced_human_action(board):
    tid = _triage(board)
    ask = {"action": "Unlock the iPhone", "why_human": "needs Face ID", "interaction": "physical",
           "scope": "one unlock", "evidence": "device locked"}
    assert kb.block_task(board, tid, kind="needs_input", reason="device locked", human_request=ask)
    task = kb.get_task(board, tid)
    assert task.status == "blocked"
    assert task.consecutive_failures == 0 and task.last_failure_error is None


@pytest.mark.parametrize("op", ["schedule", "block"])
@pytest.mark.parametrize("status", ["done", "archived", "review"])
def test_terminal_and_review_parks_stay_refused(board, op, status):
    tid = _triage(board)
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))
    before = kb.list_events(board, tid)
    assert not _park(board, tid, op, "db")
    assert kb.get_task(board, tid).status == status
    assert kb.list_events(board, tid) == before


@pytest.mark.parametrize("op", ["schedule", "block"])
def test_stale_run_cannot_park_triage(board, op):
    tid = _triage(board)
    kwargs = {"kind": "time"} if op == "block" else {}
    assert not getattr(kb, op + "_task")(board, tid, reason="rate-limit cooldown", clock_only=True, awaiting={"kind": "clock", "why": "fixture window"},
                                          expected_run_id=999, **kwargs)
    assert kb.get_task(board, tid).status == "triage"
    assert kb.get_task(board, tid).consecutive_failures == 2


@pytest.mark.parametrize("op", ["schedule", "block"])
@pytest.mark.parametrize("kwargs", [
    {"awaiting": {"kind": "human"}},
    {"awaiting": {"kind": "external", "ref": "vendor-ticket"}},
    {"awaiting": {"kind": "external", "ref": "vendor-ticket"}, "wake_check": "exit 2"},
])
def test_triage_park_guards_preserved(board, op, kwargs):
    tid = _triage(board)
    args = {"kind": "time"} if op == "block" else {}
    before = kb.list_events(board, tid)
    with pytest.raises(ValueError):
        getattr(kb, op + "_task")(board, tid, reason="waiting", **args, **kwargs)
    assert kb.get_task(board, tid).status == "triage"
    assert kb.get_task(board, tid).consecutive_failures == 2
    assert kb.list_events(board, tid) == before
