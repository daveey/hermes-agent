"""``wait-check task X --gateway G`` with no route to G: the registry replica and this gateway's Board replica
both answer; a fresh Board replica may only move the answer to met (t_01a1d5ead059278d). [daveey]"""
import json
import sqlite3
import subprocess
import time

import pytest

from hermes_cli import kanban_wait_check as wait

TID = "t_123456789abcdef0"
GW = "worker-7"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HUB_STATE", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    with sqlite3.connect(tmp_path / "hub.db") as db:
        db.execute("CREATE TABLE replica(gw TEXT, coll TEXT, key TEXT, data TEXT, updated_at INTEGER)")
    shadow = tmp_path / "kanban/sync-shadow/default.db"
    shadow.parent.mkdir(parents=True)
    with sqlite3.connect(shadow) as db:
        db.execute("CREATE TABLE tasks(id TEXT PRIMARY KEY, status TEXT)")
        db.execute("CREATE TABLE kanban_sync_state(board_id TEXT, pulled_at REAL)")
        db.execute("INSERT INTO kanban_sync_state VALUES ('default', NULL)")
    calls = []

    def ssh(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 255, "", "Permission denied (publickey).")

    monkeypatch.setattr(wait.subprocess, "run", ssh)
    return tmp_path


def registry(home, status, *, age):
    """The hermes-hub registry replica holds the card as ``status``, observed ``age`` seconds ago."""
    scope = {"task_scopes": [{"board": "default", "profile": "default", "state": "ok", "observed_at": time.time() - age}]}
    row = dict(id=TID, board="default", status=status, gateway=GW, profile="default", owner_profile="default",
               wait_observation={"parked_agent": False, "orphan_reason": None})
    with sqlite3.connect(home / "hub.db") as db:
        db.execute("DELETE FROM replica")
        db.execute("INSERT INTO replica VALUES(?,?,?,?,?)", (GW, "gateway", GW, json.dumps(scope), 1))
        db.execute("INSERT INTO replica VALUES(?,?,?,?,?)", (GW, "tasks", f"default/default/{TID}", json.dumps(row), 1))


def board(home, status, *, age):
    """This gateway's Board replica holds the card as ``status``, last pulled to the head ``age`` seconds ago."""
    with sqlite3.connect(home / "kanban/sync-shadow/default.db") as db:
        db.execute("INSERT OR REPLACE INTO tasks VALUES (?, ?)", (TID, status))
        db.execute("UPDATE kanban_sync_state SET pulled_at = ?", (time.time() - age,))


def probe(**kw):
    return wait.check_remote(GW, "task", TID, until=("done",), **kw)


def test_a_fresh_board_met_wins_over_a_lagging_registry(home):
    registry(home, "running", age=120)
    board(home, "done", age=5)
    rc, text = probe()
    assert rc == 0 and text.split()[:2] == [TID, "done"] and "(board replica)" in text, text


def test_a_board_replica_never_downgrades_the_registry_met(home):
    """pulled_at is this gateway's pull time: a row pulled 5 s ago can still lag the owner's push."""
    registry(home, "done", age=300)
    board(home, "running", age=5)
    rc, text = probe()
    assert rc == 0 and "(replica)" in text and "(board replica)" not in text, text


def test_a_fresh_board_met_wins_whatever_the_registry_scope_age(home):
    """Only met moves, so the two hosts' clocks are never compared: a registry not-yet observed after the
    Board pull does not hold back the Board's done (what the no-``--gateway`` probe of that row reports)."""
    registry(home, "running", age=5)
    board(home, "done", age=120)
    rc, text = probe()
    assert rc == 0 and "(board replica)" in text, text


@pytest.mark.parametrize("age", [601, 3600])
def test_a_board_replica_not_pulled_recently_adds_nothing(home, age):
    registry(home, "running", age=30)
    board(home, "done", age=age)
    rc, text = probe()
    assert rc == 1 and "(replica)" in text, text


def test_a_board_replica_with_a_future_stamp_adds_nothing(home):
    registry(home, "running", age=30)
    board(home, "done", age=-120)
    assert probe()[0] == 1


def test_stale_registry_and_no_board_row_stays_unknown(home):
    registry(home, "running", age=5000)
    rc, text = probe()
    assert rc == 3 and "stale_age=" in text, text


def test_stale_registry_with_a_fresh_board_met_reads_the_board(home):
    registry(home, "running", age=5000)
    board(home, "done", age=5)
    rc, text = probe()
    assert rc == 0 and "(board replica)" in text, text


def test_stale_registry_with_a_fresh_board_not_yet_stays_unknown(home):
    """The owner is offline: its frozen Board row still says running, however recently this gateway pulled it.
    rc 3 must stand, or the park never wakes as gateway_offline at STALE_WAKE_AFTER."""
    registry(home, "running", age=5000)
    board(home, "running", age=5)
    rc, text = probe()
    assert rc == 3 and "stale_age=" in text, text


def test_from_applies_to_the_board_replica(home):
    registry(home, "running", age=120)
    board(home, "done", age=5)
    rc, text = probe(since=("done",))
    assert rc == 1, text  # the Board's done is the status the waiter already handled: not yet


def test_unreadable_board_replica_keeps_the_registry_answer(home):
    registry(home, "running", age=30)
    (home / "kanban/sync-shadow/default.db").write_text("not sqlite")
    rc, text = probe()
    assert rc == 1 and "(replica)" in text, text


def test_a_live_owner_read_stands(home, monkeypatch):
    """No registry record: the replica reader asks the owner live; that answer is current, not weighed."""
    from hermes_cli import kanban_wait_live
    board(home, "done", age=5)
    monkeypatch.setattr(kanban_wait_live, "read_task", lambda *a, **k: {"status": "running"})
    rc, text = probe()
    assert rc == 1 and "(live owner)" in text, text


def cli(*args, capsys):
    """``hermes kanban wait-check ...`` through the real kanban parser: ``(rc, stdout, stderr)``."""
    import argparse
    from hermes_cli import kanban
    from hermes_cli.kanban_parser import build_parser
    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers())
    rc = kanban.kanban_command(parser.parse_args(["kanban", "wait-check", *args]))
    out = capsys.readouterr()
    return rc, out.out.strip(), out.err.strip()


SSH_FAILURES = {
    "connect timeout": lambda argv: subprocess.TimeoutExpired(argv, 15),
    "no route": lambda argv: OSError(113, "No route to host"),
}


@pytest.fixture(params=sorted(SSH_FAILURES))
def unreachable(request, home, monkeypatch):
    """The owner box is down or its tailnet route is gone: the ssh probe raises instead of exiting."""
    calls = []

    def ssh(argv, **kwargs):
        calls.append(argv)
        raise SSH_FAILURES[request.param](argv)

    monkeypatch.setattr(wait.subprocess, "run", ssh)
    return calls


@pytest.mark.parametrize("registry_age", [120, 5000], ids=["registry-not-yet", "registry-stale"])
def test_an_unreachable_owner_wakes_on_a_fresh_board_met(home, unreachable, capsys, registry_age):
    """#2370: the commonest unreachable owner is an ssh that never connects (timeout / OSError), not rc 255.
    The fresh Board replica's done must still turn it into met, whatever the registry replica says."""
    registry(home, "running", age=registry_age)
    board(home, "done", age=5)
    rc, out, err = cli("task", TID, "--gateway", GW, "--until", "done", capsys=capsys)
    assert unreachable and unreachable[0][0] == "ssh", unreachable
    assert rc == 0 and out.split()[:2] == [TID, "done"] and "(board replica)" in out, (rc, out, err)


def test_an_unreachable_owner_without_board_evidence_keeps_the_registry_answer(home, unreachable, capsys):
    registry(home, "running", age=120)
    rc, out, err = cli("task", TID, "--gateway", GW, "--until", "done", capsys=capsys)
    assert unreachable
    assert rc == 1 and "(replica)" in out and "(board replica)" not in out, (rc, out, err)


def test_an_unreachable_gateway_is_an_error_for_a_non_task_probe(home, unreachable, capsys):
    """Only a task has a replica to fall back on: a board-empty probe that cannot reach ssh cannot tell."""
    board(home, "done", age=5)
    rc, out, err = cli("board-empty", "default", "--gateway", GW, capsys=capsys)
    assert unreachable
    assert rc == 2 and err.startswith(f"error: ssh {GW}: ") and not out, (rc, out, err)
