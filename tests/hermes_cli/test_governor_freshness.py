"""Truthful, bounded tolerance for slow real dispatcher completions. [daveey]"""
from types import SimpleNamespace
import json
import os
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_governors as governors
from hermes_cli import kanban_why_fast as fast
from tests.hermes_cli import test_kanban_governors as helpers

board = helpers.board
clean = helpers.clean


def test_real_ticks_derive_gap_tolerance_without_advancing_the_decision(board, monkeypatch):
    helpers._usage(monkeypatch, [10.0])
    cfg = json.loads((board / "config.yaml").read_text())
    cfg.setdefault("kanban", {})["dispatch_interval_seconds"] = 30
    (board / "config.yaml").write_text(json.dumps(cfg))
    clock = [time.time()]
    monkeypatch.setattr(governors, "time", SimpleNamespace(time=lambda: clock[0], monotonic=time.monotonic))
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="recorded idle card", assignee="default")
        other = kb.create_task(conn, title="another idle card", assignee="default")
        helpers._tick(conn, max_spawn=0)
        clock[0] += 360
        helpers._tick(conn, max_spawn=0)
        snap = governors.snapshot(conn, now=clock[0])
        assert snap["freshness_bound_s"] == 420
        assert snap["tick_gap_p95_s"] == 360
        tick_at = snap["tick_at"]
        path = governors._db_path(conn)

        def slow_spawn(task, workspace, board=None):
            # The real dispatcher's spawn seam is occupied; no recorder has run.
            waiting = other if task.id == tid else tid
            clock[0] += 240
            answer = fast.read_answer(path, waiting, now=clock[0])
            assert answer is not None and answer["source"] == "snapshot"
            assert answer["age_s"] == 240 and answer["freshness_bound_s"] == 420
            assert answer["recorded"]["tick_at"] == tick_at
            assert not governors.snapshot(conn, now=clock[0])["stale"]
            assert fast.read_answer(path, waiting, now=tick_at + 421) is None
            assert governors.snapshot(conn, now=tick_at + 421)["stale"]
            stale_card = governors.card(conn, waiting, now=tick_at + 421)
            assert stale_card is not None and stale_card["stale"]
            clock[0] += 120
            return os.getpid()

        from hermes_cli import kanban_db_dispatch as kbd
        result = kbd.dispatch_once(conn, spawn_fn=slow_spawn, max_spawn=1)
        assert len(result.spawned) == 1, result
        assert governors.snapshot(conn, now=clock[0])["tick_at"] == clock[0]


@pytest.mark.parametrize("interval,gap,bound", [(30, 360, 420), (200, 30, 600), (30, 900, 600)])
def test_tolerance_is_interval_or_measured_gap_with_a_hard_ceiling(interval, gap, bound):
    policy = fast.completion_policy({"tick_at": 1000}, 1000 + gap, interval)
    assert policy["freshness_bound_s"] == bound
    assert policy["tick_gap_p95_s"] == gap
    assert fast.freshness_bound(policy) == bound


@pytest.mark.parametrize("value", [None, True, "600", -1, 0, float("nan"), float("inf")])
def test_invalid_bound_fails_closed(value):
    assert fast.freshness_bound({"freshness_bound_s": value}) == 0


def test_gap_history_is_bounded_and_ages_out_outliers():
    policy = {"tick_at": 1000}
    for now in [1900, *range(1930, 2600, 30)]:
        policy = fast.completion_policy(policy, now, 30)
    assert len(policy["tick_completions"]) == 21
    assert policy["tick_gap_p95_s"] == 30
    assert policy["freshness_bound_s"] == 90
    assert fast.freshness_bound({}) == 180
    assert fast.freshness_bound({"freshness_bound_s": 9000}) == 600


def test_snapshot_text_reports_tolerance_as_historical_not_current(capsys):
    from hermes_cli import kanban_governors_cli as cli

    cli._print_why({"task_id": "example", "status": "ready", "board": "default", "source": "snapshot",
                    "first_stop": {"label": "Host slots", "age_s": 240, "freshness_bound_s": 420},
                    "freshness_bound_s": 420, "age_s": 240})
    assert "240s old; tolerance 420s; recorded decision, not live" in capsys.readouterr().out
