"""Immediate confirm failures must be visible and eventually wake their owner."""
from __future__ import annotations

import shlex
import sys
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_wake_check as kwc
from tests.hermes_cli import test_kanban_wake_check as base

kanban_home = base.kanban_home
BUILD, _events, _running = base.BUILD, base._events, base._running

SENTENCE = ("On exit 0 the probe is run again at once and the card wakes only if that run exits 0 too: "
            "it must give the same answer when re-run seconds later.")
NOTE = ("This card's wake_check passed and then failed its immediate confirm re-run 3 times in a row. "
        "A passing probe is run twice and the card wakes only if both runs pass, so a probe that compares "
        "against the record its own first run just wrote can never wake. Make it give the same answer "
        "when re-run seconds later.")


def _park(conn, cmd):
    tid = _running(conn)
    assert kb.schedule_task(conn, tid, reason="SCHEDULED_UNTIL=2099-01-01T00:00:00Z waiting for release",
                     wake_check=cmd, check_every=120, awaiting=BUILD)
    return tid


@pytest.fixture
def pair_probe(tmp_path):
    script = tmp_path / "pair.py"
    history = tmp_path / "history"
    enabled = tmp_path / "enabled"
    steady = tmp_path / "steady"
    script.write_text("import pathlib, sys\n"
                      f"p = pathlib.Path({str(history)!r})\n"
                      f"enabled = pathlib.Path({str(enabled)!r})\n"
                      f"steady = pathlib.Path({str(steady)!r})\n"
                      "if not enabled.exists(): sys.exit(1)\n"
                      "if steady.exists(): print('steady'); sys.exit(0)\n"
                      "n = int(p.read_text()) if p.exists() else 0\n"
                      "p.write_text(str(n + 1))\n"
                      "print(('first ' if n % 2 == 0 else 'confirm ') + 'x' * 600)\n"
                      "sys.exit(n % 2)\n")
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}", enabled, steady


def test_three_pairs_survive_restart_and_wake_once(kanban_home, pair_probe):
    cmd, enabled, _ = pair_probe
    with kbc.connect_closing() as conn:
        tid = _park(conn, cmd)
    enabled.touch()
    now = int(time.time())
    for cycle in range(3):
        # A fresh dispatcher connection each cycle; the real runner also claims a new generation.
        with kbc.connect_closing() as conn:
            assert kwc.run_due_wake_checks(conn, now=now + cycle * 121, wait=True)["submitted"] == [tid]
            events = _events(conn, tid, "wake_confirm_failed")
            assert len(events) == cycle + 1
            payload = events[-1].payload
            assert payload["first_rc"] == 0 and payload["confirm_rc"] == 1
            assert payload["first_output"] == "x" * kwc.OUTPUT_TAIL_CHARS
            assert payload["confirm_output"] == "x" * kwc.OUTPUT_TAIL_CHARS
            assert len(_events(conn, tid, "wake_episode_reset")) == cycle + 1
            assert kb.get_task(conn, tid).status == ("ready" if cycle == 2 else "scheduled")
    with kbc.connect_closing() as conn:
        assert len(_events(conn, tid, "wake_check_flapping")) == 1
        comment = conn.execute("SELECT author, body FROM task_comments WHERE task_id=?", (tid,)).fetchall()
        assert NOTE in kwc.render_wake_context(conn, tid)
        assert ("kanban-dispatcher", NOTE) in [tuple(row) for row in comment]
        kwc.run_due_wake_checks(conn, now=now + 1000, wait=True)
        assert len(_events(conn, tid, "wake_check_flapping")) == 1


def test_passing_confirm_wakes_normally_and_repark_resets(kanban_home, pair_probe):
    cmd, enabled, steady = pair_probe
    with kbc.connect_closing() as conn:
        tid = _park(conn, cmd)
        enabled.touch()
        now = int(time.time())
        kwc.run_due_wake_checks(conn, now=now, wait=True)
        assert kb.get_task(conn, tid).status == "scheduled"
        steady.touch()
        kwc.run_due_wake_checks(conn, now=now + 121, wait=True)
        assert kb.get_task(conn, tid).status == "ready"
        assert len(_events(conn, tid, "condition_met")) == 1
        assert kwc.get_state(conn, tid) is None
        steady.unlink()
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='scheduled' WHERE id=?", (tid,))
            kwc.set_check(conn, tid, cmd, 120, now=now + 242)
        for cycle in range(2):
            kwc.run_due_wake_checks(conn, now=now + 242 + cycle * 121, wait=True)
            assert kb.get_task(conn, tid).status == "scheduled"


@pytest.mark.parametrize("change_cmd", [False, True])
def test_repark_discards_streak_and_old_result(kanban_home, pair_probe, change_cmd):
    cmd, enabled, _ = pair_probe
    with kbc.connect_closing() as conn:
        tid = _park(conn, cmd)
        enabled.touch()
        now = int(time.time())
        kwc.run_due_wake_checks(conn, now=now, wait=True)
        old = kwc.get_state(conn, tid)["generation"]
        new_cmd = cmd + " # new" if change_cmd else cmd
        with kb.write_txn(conn):
            kwc.set_check(conn, tid, new_cmd, 120, now=now + 121)
        kwc.apply_result(conn, tid, cmd, 1, "old", generation=old,
                         diagnostics={"first_rc": 0, "first_output": "old", "confirm_rc": 1})
        for cycle in range(2):
            kwc.run_due_wake_checks(conn, now=now + 121 + cycle * 121, wait=True)
            assert kb.get_task(conn, tid).status == "scheduled"
        assert not _events(conn, tid, "wake_check_flapping")


def test_confirm_rule_documented():
    from tools.kanban_tools_schemas import _WAKE_CHECK_PROPS
    from hermes_cli.kanban_parser import build_parser

    assert SENTENCE in _WAKE_CHECK_PROPS["wake_check"]["description"]
    import argparse
    parser = build_parser(argparse.ArgumentParser().add_subparsers())
    schedule = parser._subparsers._group_actions[0].choices["schedule"]
    action = next(a for a in schedule._actions if a.dest == "wake_check")
    assert SENTENCE in action.help


def test_legacy_migration_adds_zero_counter(kanban_home):
    with kbc.connect_closing() as conn:
        tid = _park(conn, "exit 1")
        with kb.write_txn(conn):
            conn.execute("ALTER TABLE task_wake_checks DROP COLUMN confirm_fails")
    kb.init_db()
    with kbc.connect_closing() as conn:
        assert conn.execute("SELECT confirm_fails FROM task_wake_checks WHERE task_id=?", (tid,)).fetchone()[0] == 0


@pytest.mark.parametrize("rc", [1, 2, "timeout"])
def test_nonconfirm_result_breaks_streak(kanban_home, rc):
    with kbc.connect_closing() as conn:
        tid = _park(conn, "exit 1")
        generation = kwc.get_state(conn, tid)["generation"]
        failed = {"first_rc": 0, "first_output": "first", "confirm_rc": 1}
        kwc.apply_result(conn, tid, "exit 1", 1, "confirm", generation=generation, diagnostics=failed)
        kwc.apply_result(conn, tid, "exit 1", rc, "waiting", generation=generation)
        for _ in range(2):
            kwc.apply_result(conn, tid, "exit 1", 1, "confirm", generation=generation, diagnostics=failed)
        assert kb.get_task(conn, tid).status == "scheduled"
        assert not _events(conn, tid, "wake_check_flapping")


@pytest.mark.parametrize("rc", [2, "timeout"])
def test_nonzero_confirm_diagnostics_and_threshold(kanban_home, rc):
    with kbc.connect_closing() as conn:
        tid = _park(conn, "exit 1")
        generation = kwc.get_state(conn, tid)["generation"]
        for _ in range(3):
            kwc.apply_result(conn, tid, "exit 1", rc, "confirm", generation=generation,
                             diagnostics={"first_rc": 0, "first_output": "first", "confirm_rc": rc})
        assert kb.get_task(conn, tid).status == "ready"
        assert _events(conn, tid, "wake_confirm_failed")[-1].payload["confirm_rc"] == rc
        assert len(_events(conn, tid, "wake_check_flapping")) == 1
