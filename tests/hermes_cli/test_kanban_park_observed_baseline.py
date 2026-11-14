"""A generated card probe waits for a change from the park-time observation. [daveey]"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import shlex
import subprocess
import time

import pytest

from hermes_cli import kanban_cli_park as cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_wait_check as wait
from hermes_cli import kanban_wake_check as wake


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "kanban.db"))
    monkeypatch.setenv("HERMES_KANBAN_WAKE_DRYRUN", "0")
    kb.init_db()
    with kbc.connect_closing() as conn:
        yield conn
    wake._shutdown_for_tests()


def _status(conn, tid, status):
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status=?, human_request=? WHERE id=?",
                     (status, json.dumps({"action": "answer"}) if status == "blocked" else None, tid))


def _park(conn, monkeypatch, status, *, explicit=False, gateway=None):
    producer = kb.create_task(conn, title="producer")
    _status(conn, producer, status)
    owner = kb.create_task(conn, title="waiter")
    args = argparse.Namespace(awaiting=f"card:{producer}", awaiting_why=None, reason="waiting for producer",
                              wake_check=f"hermes kanban wait-check task {producer}" if explicit else None)
    if gateway:
        args.awaiting = {"kind": "card", "ref": producer, "gateway": gateway}
        monkeypatch.setattr(wait, "check_remote", lambda *a, **kw: (0, f"{producer} {status} (replica)"))
    record, fields = cli._cli_awaiting(args, cli._cli_wake_check(args))
    assert kb.schedule_task(conn, owner, reason=args.reason, awaiting=record, wake_check=fields[0])
    monkeypatch.setenv("HERMES_KANBAN_TASK", owner)
    monkeypatch.setenv("HERMES_KANBAN_WAKE_PROBE", "1")
    return owner, producer


def _probe(conn, owner, *, until=None):
    state = wake.get_state(conn, owner)
    parser = argparse.ArgumentParser()
    parser.add_argument("check")
    parser.add_argument("target")
    for flag in ("--gateway", "--board", "--until"):
        parser.add_argument(flag)
    parser.add_argument("--from", dest="since")
    args = parser.parse_args(shlex.split(state["cmd"])[3:])
    if until:
        args.until = until
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = wait.run(args)
    return rc, out.getvalue(), state


def _apply(conn, owner):
    rc, out, state = _probe(conn, owner)
    wake.apply_result(conn, owner, state["cmd"], rc, out, generation=state["generation"])
    return rc


@pytest.mark.parametrize("status", ["blocked", "review"])
@pytest.mark.parametrize("finished", ["done", "archived", "triage"])
def test_first_generated_probe_waits_then_wakes(board, monkeypatch, status, finished):
    owner, producer = _park(board, monkeypatch, status)
    assert _apply(board, owner) == 1
    assert kb.get_task(board, owner).status == "scheduled"
    _status(board, producer, finished)
    assert _apply(board, owner) == 0
    assert kb.get_task(board, owner).status == "ready"


def test_running_at_park_then_blocked_still_wakes(board, monkeypatch):
    owner, producer = _park(board, monkeypatch, "running")
    assert _apply(board, owner) == 1
    _status(board, producer, "blocked")
    assert _apply(board, owner) == 0


def test_leaving_and_returning_to_baseline_wakes(board, monkeypatch):
    owner, producer = _park(board, monkeypatch, "blocked")
    assert _apply(board, owner) == 1
    _status(board, producer, "running")
    assert _apply(board, owner) == 1
    _status(board, producer, "blocked")
    assert _apply(board, owner) == 0


@pytest.mark.parametrize("explicit", [False, True])
def test_explicit_command_or_until_unchanged(board, monkeypatch, explicit):
    owner, _ = _park(board, monkeypatch, "blocked", explicit=explicit)
    assert _probe(board, owner, until=None if explicit else "blocked")[0] == 0


def test_daily_deadline_still_wakes(board, monkeypatch):
    owner, _ = _park(board, monkeypatch, "blocked")
    assert _apply(board, owner) == 1
    park = [e for e in kb.list_events(board, owner) if e.kind == "scheduled"][-1]
    assert 86390 <= park.payload["wake_at"] - time.time() <= 86400
    from hermes_cli.kanban_continuation import wake_due_continuations
    wake_due_continuations(board, now=park.payload["wake_at"] + 1)
    assert kb.get_task(board, owner).status == "ready"


@pytest.mark.parametrize("status", ["blocked", "review"])
def test_gateway_replica_first_park_then_done(board, monkeypatch, tmp_path, status):
    original = wait.check_remote
    owner, producer = _park(board, monkeypatch, status, gateway="macbook")
    monkeypatch.setattr(wait, "check_remote", original)
    monkeypatch.setattr(wait.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 255, "", "offline"))
    import sqlite3
    state = tmp_path / "hub-state"
    state.mkdir()
    monkeypatch.setenv("HERMES_HUB_STATE", str(state))
    with sqlite3.connect(state / "hub.db") as replica:
        replica.execute("CREATE TABLE replica(gw TEXT, coll TEXT, key TEXT, data TEXT, updated_at INTEGER)")
        scope = {"task_scopes": [{"board": "default", "profile": "default", "state": "ok",
                                  "observed_at": time.time()}]}
        replica.execute("INSERT INTO replica VALUES(?,?,?,?,?)",
                        ("macbook", "gateway", "macbook", json.dumps(scope), 1))
        row = dict(id=producer, board="default", status=status, gateway="macbook", profile="default",
                   owner_profile="default", wait_observation={"parked_agent": False, "orphan_reason": None})
        replica.execute("INSERT INTO replica VALUES(?,?,?,?,?)",
                        ("macbook", "tasks", f"default/default/{producer}", json.dumps(row), 1))
        replica.commit()
        assert _apply(board, owner) == 1
        row["status"] = "done"
        replica.execute("UPDATE replica SET data=? WHERE coll='tasks'", (json.dumps(row),))
        replica.commit()
        assert _apply(board, owner) == 0


@pytest.mark.parametrize("newer", [False, True])
def test_condition_met_precedence_uses_event_order(board, monkeypatch, newer):
    owner, producer = _park(board, monkeypatch, "blocked")
    state = wake.get_state(board, owner)
    park = [e for e in kb.list_events(board, owner) if e.kind == "scheduled"][-1].payload
    with kb.write_txn(board):
        kb._append_event(board, owner, "condition_met", {"wake_check": state["cmd"],
                                                         "output": f"{producer} review"})
        if not newer:
            kb._append_event(board, owner, "scheduled", park)
    _status(board, producer, "review")
    assert _probe(board, owner)[0] == (1 if newer else 0)
    # --until retains the old handled-condition rule even when the generated park is newer.
    assert _probe(board, owner, until="review")[0] == 1


def test_stale_result_cannot_retire_baseline(board, monkeypatch):
    owner, producer = _park(board, monkeypatch, "blocked")
    state = wake.get_state(board, owner)
    wake.apply_result(board, owner, state["cmd"], 1, f"{producer} running", generation="old-generation")
    assert _probe(board, owner)[0] == 1
