"""`hermes kanban schedule|block <id>` from outside refuses a card a live worker owns (t_01a1d0622df6ea87). [daveey]

PR #1517 fenced the agent tools (``kanban_schedule``/``kanban_block``) against parking a card under a live worker,
but the CLI is a sibling entry path: ``_cmd_schedule``/``_cmd_block`` passed ``expected_run_id=None`` for an
outside caller, so ``hermes kanban schedule <id>`` over ssh still ended the worker's open run underneath it (the
2026-11-12 incident shape). The CLI now consults the same ``live_claim()`` per id: a live one is refused with
nothing changed, the rest of a bulk ``--ids`` still parks, and the exit is non-zero.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb

COOLDOWN = "SCHEDULED_UNTIL=2099-01-01T00:00:00Z rate-limit cooldown"


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / ".hermes"
    h.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(h))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_CLAIM_LOCK", "HERMES_KANBAN_BOARD",
                "HERMES_KANBAN_DB", "HERMES_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return h


def _conn():
    from hermes_cli import kanban_db_connect as kbc
    return kbc.connect()


def _cli():
    from hermes_cli import kanban as kcli
    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    kcli.build_parser(parser.add_subparsers(dest="command"))
    return lambda *argv: kcli.kanban_command(parser.parse_args(["kanban", *argv]))


def _claimed(live: bool = True) -> tuple[str, int]:
    """A card the dispatcher claimed; with ``live`` this test process stands in for its spawned worker."""
    from hermes_cli import kanban_db_dispatch as kbd
    with _conn() as conn:
        tid = kb.create_task(conn, title="verify after owner lands", assignee="default")
        assert kb.claim_task(conn, tid, claimer=kb._claimer_id()) is not None
        if live:
            kbd._set_worker_pid(conn, tid, os.getpid())
        return tid, kb._current_run_id(conn, tid)


def _ready() -> str:
    with _conn() as conn:
        tid = kb.create_task(conn, title="not started", assignee="default")
        assert kb.get_task(conn, tid).status == "ready"
        return tid


def _run(run_id: int):
    with _conn() as conn:
        return conn.execute("SELECT ended_at, outcome FROM task_runs WHERE id = ?", (run_id,)).fetchone()


def _status(tid: str) -> str:
    with _conn() as conn:
        return kb.get_task(conn, tid).status


def _assert_untouched(tid: str, run_id: int) -> None:
    assert _status(tid) == "running"
    assert _run(run_id)["ended_at"] is None  # the worker's run was not ended underneath it
    with _conn() as conn:
        assert not kb.list_comments(conn, tid)  # no SCHEDULED:/BLOCKED: comment claiming a park
        assert not [e for e in kb.list_events(conn, tid) if e.kind in ("scheduled", "blocked")]


def _assert_refusal(err: str, tid: str, run_id: int) -> None:
    assert "live worker claim" in err and tid in err and f"run {run_id}" in err, err
    assert kb._claimer_id() in err, err  # names the holder
    assert "Nothing changed" in err, err
    assert f"hermes kanban comment {tid}" in err, err
    assert "hermes kanban reclaim" in err and "hermes kanban archive" in err, err


def test_cli_schedule_of_a_live_claimed_card_is_refused_and_the_run_stays_open(home, capsys):
    tid, run_id = _claimed()
    assert _cli()("schedule", tid, COOLDOWN, "--clock-only") != 0
    _assert_refusal(capsys.readouterr().err, tid, run_id)
    _assert_untouched(tid, run_id)


def test_cli_block_of_a_live_claimed_card_is_refused_and_the_run_stays_open(home, capsys):
    tid, run_id = _claimed()
    assert _cli()("block", tid, COOLDOWN, "--kind", "time", "--clock-only") != 0
    _assert_refusal(capsys.readouterr().err, tid, run_id)
    _assert_untouched(tid, run_id)


@pytest.mark.parametrize("verb,extra", [("schedule", ()), ("block", ("--kind", "time"))])
def test_cli_bulk_ids_refuses_only_the_live_card_and_parks_the_rest(home, capsys, verb, extra):
    live, run_id = _claimed()
    idle = _ready()
    assert _cli()(verb, live, COOLDOWN, *extra, "--clock-only", "--ids", idle) == 1
    out = capsys.readouterr()
    _assert_refusal(out.err, live, run_id)
    assert idle not in out.err and idle in out.out, out
    _assert_untouched(live, run_id)
    assert _status(idle) == "scheduled"


def test_the_worker_still_parks_its_own_live_card_via_the_cli(home, monkeypatch, capsys):
    tid, run_id = _claimed()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    assert _cli()("schedule", tid, COOLDOWN, "--clock-only") == 0, capsys.readouterr().err
    assert _status(tid) == "scheduled" and _run(run_id)["outcome"] == "scheduled"


def test_the_worker_still_blocks_its_own_live_card_via_the_cli(home, monkeypatch, capsys):
    tid, run_id = _claimed()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    assert _cli()("block", tid, COOLDOWN, "--kind", "time", "--clock-only") == 0, capsys.readouterr().err
    assert _status(tid) == "scheduled" and _run(run_id)["ended_at"] is not None


@pytest.mark.parametrize("verb,extra", [("schedule", ()), ("block", ("--kind", "time"))])
def test_a_claim_whose_worker_is_gone_is_still_parked_via_the_cli(home, capsys, verb, extra):
    """A claim with no live worker protects no run (the same rule the tool path applies)."""
    tid, run_id = _claimed(live=False)
    assert _cli()(verb, tid, COOLDOWN, *extra, "--clock-only") == 0, capsys.readouterr().err
    assert _status(tid) == "scheduled" and _run(run_id)["ended_at"] is not None


@pytest.mark.parametrize("verb,extra", [("schedule", ()), ("block", ("--kind", "time"))])
def test_a_ready_card_is_still_parked_via_the_cli(home, capsys, verb, extra):
    tid = _ready()
    assert _cli()(verb, tid, COOLDOWN, *extra, "--clock-only") == 0, capsys.readouterr().err
    assert _status(tid) == "scheduled"


def test_tool_and_cli_refusals_carry_the_same_facts(home, capsys):
    """One helper decides both paths: the tool's typed error and the CLI line name the same holder and run."""
    tid, run_id = _claimed()
    from tools import kanban_tools as kt
    out = json.loads(kt._handle_schedule({"task_id": tid, "reason": COOLDOWN, "clock_only": True}))
    assert out.get("code") == "live_claim_park" and out.get("run_id") == run_id, out
    assert _cli()("schedule", tid, COOLDOWN, "--clock-only") != 0
    err = capsys.readouterr().err
    assert out["holder"] in err and f"run {run_id}" in err, err
    _assert_untouched(tid, run_id)
