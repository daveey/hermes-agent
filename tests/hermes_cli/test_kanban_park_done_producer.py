"""A park never waits on something that is already true. [daveey]

MacBook card t_01a18c12b9e15db0, run by a hub worker, was parked twice (continuations 6 and 7, 2026-10-30) with
``awaiting={kind:'card', ref:'t_01a1848506cae619', inferred:true}`` and the auto-added probe ``hermes kanban
wait-check task t_01a1848506cae619``. That card had completed two hours earlier; the checkpoint named it only as
background (the real wait was main CI). ``_live_card_ref`` took the first card id in the text that was not an
orphan, and a done card is not an orphan, so the probe exited 0 on its first run and each park woke in seconds.

- Inference skips a done card (it produces nothing later).
- A declared ``awaiting={kind:'card'}`` on a done card is refused ``producer_done`` (local, and on another gateway).
- An explicit wake_check is run once at park time; exit 0 is refused ``wake_check_already_true`` on every entry
  path (kanban_schedule, kanban_continue with a wait, kanban_block kind=time, ``hermes kanban schedule``).
"""
from __future__ import annotations

import argparse

import pytest

from hermes_cli import kanban_awaiting as kaw
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_wake_check as kwc
from tests.hermes_cli import test_kanban_awaiting as _awaiting_tests
from tests.hermes_cli.test_kanban_awaiting import _as_worker, _park_payload, _running, _tool

kanban_home = _awaiting_tests.kanban_home  # the shared fixture (HERMES_HOME, board, dry run off)

BUILD = {"kind": "external", "ref": "release build 559", "why": "the build job writes the flag when it lands"}


def _done(conn) -> str:
    tid = _running(conn)
    assert kb.complete_task(conn, tid, summary="landed the fix")
    assert kb.get_task(conn, tid).status == "done"
    return tid


@pytest.fixture
def dry_run(monkeypatch):
    """The park-time probe runs (the shared fixture turns it off for unrelated tests)."""
    monkeypatch.delenv("HERMES_KANBAN_WAKE_DRYRUN", raising=False)


# -- 1. inference ---------------------------------------------------------------------------------

def test_infer_never_picks_a_done_card(kanban_home) -> None:
    with kbc.connect_closing() as conn:
        done, live, tid = _done(conn), _running(conn), _running(conn)
        # Only a done card named: no producer is inferred, so the park names nothing and is refused.
        with pytest.raises(kaw.AwaitingError) as err:
            kaw.resolve(conn, tid, awaiting=None, op="schedule",
                        reason=f"{done} landed the fix earlier; waiting for main CI on 3ca096ffbbc")
        assert err.value.code == kaw.NO_WAKE_REASON
        both = kaw.resolve(conn, tid, awaiting=None, op="schedule",
                           reason=f"{done} landed the fix; now waiting on {live} to deploy it")
        assert both == {"kind": "card", "ref": live, "inferred": True}


def test_relayed_worker_node_park_naming_a_done_card_infers_no_producer(tmp_path, monkeypatch) -> None:
    """The t_01a18c12b9e15db0 shape: the home board's card runs on a worker node, whose worker checkpoints with a
    wait and names a card that is done on the worker's board."""
    from hermes_cli import kanban_continuation as kcont
    from hermes_cli import kanban_db_dispatch as kbd
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_worker_node as wn
    from tests.hermes_cli.kanban_sync_fake_board import FakeService
    from tests.hermes_cli.test_kanban_worker_node import HUB, WORKER, Gateway

    svc = FakeService(lease_runs=True)
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: None)
    monkeypatch.setenv("HERMES_KANBAN_WAKE_DRYRUN", "0")
    sync.consume_dispatcher_wake()
    home = Gateway(tmp_path, HUB, svc, monkeypatch, pickup=False)
    worker = Gateway(tmp_path, WORKER, svc, monkeypatch, pickup="true")
    try:
        home.enter()
        tid = kb.create_task(home.conn, title="runs on the worker", assignee="default", runs_on=["linux"])
        home.round()
        worker.round()
        worker.tick()
        assert kb.get_task(worker.conn, tid).status == "running"
        worker.enter()
        done = kb.create_task(worker.conn, title="earlier fix", assignee="default")
        assert kb.complete_task(worker.conn, done, summary="landed")
        run_id = kb.get_task(worker.conn, tid).current_run_id
        res = kcont.continue_task(
            worker.conn, tid, resume_after_seconds=1800, expected_run_id=run_id,
            checkpoint=f"Fix shipped; {done} completed the earlier half. Waiting for main CI for 3ca096ffbbc.")
        assert res.get("ok"), res
        park = _park_payload(worker.conn, tid)
        assert (park.get("awaiting") or {}).get("kind") != "card", park
        state = kwc.get_state(worker.conn, tid)
        assert not state or done not in (state.get("cmd") or ""), state
    finally:
        kwc._shutdown_for_tests()
        for g in (home, worker):
            g.conn.close()
        sync.set_lease_client(None)
        wn.set_registry_fetch(None)


# -- 2. declared producer --------------------------------------------------------------------------

def test_schedule_on_a_local_done_producer_is_refused(kanban_home, monkeypatch) -> None:
    with kbc.connect_closing() as conn:
        done, tid = _done(conn), _running(conn)
        _as_worker(monkeypatch, conn, tid)
        out = _tool("schedule", {"reason": "waiting for the fix", "awaiting": {"kind": "card", "ref": done}})
        assert kaw.PRODUCER_DONE in out["error"] and done in out["error"], out
        assert f"kanban_show {done}" in out["error"] and "already done (completed 20" in out["error"], out
        assert kb.get_task(conn, tid).status == "running"


def test_schedule_on_a_remote_done_producer_is_refused(kanban_home, monkeypatch) -> None:
    from hermes_cli import kanban_wait_check as kwait

    monkeypatch.setattr(kwait, "check_remote", lambda gw, kind, ref, board=None, until=None: (0, f"{ref} done"))
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _as_worker(monkeypatch, conn, tid)
        out = _tool("schedule", {"reason": "waiting for the MacBook fix",
                                 "awaiting": {"kind": "card", "ref": "t_01a1848506cae619", "gateway": "macbook"}})
        assert kaw.PRODUCER_DONE in out["error"] and "t_01a1848506cae619" in out["error"], out
        assert kb.get_task(conn, tid).status == "running"


# -- 3. park-time probe ----------------------------------------------------------------------------

def test_schedule_wake_check_already_true_is_refused(kanban_home, monkeypatch, dry_run) -> None:
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _as_worker(monkeypatch, conn, tid)
        out = _tool("schedule", {"reason": "waiting for build 559", "awaiting": BUILD,
                                 "wake_check": "echo build 559 seen; true"})
        assert kwc.WAKE_CHECK_ALREADY_TRUE in out["error"] and "build 559 seen" in out["error"], out
        assert out.get("rejection") == kwc.WAKE_CHECK_ALREADY_TRUE, out
        assert kb.get_task(conn, tid).status == "running"


def test_schedule_wake_check_not_yet_parks(kanban_home, monkeypatch, dry_run) -> None:
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _as_worker(monkeypatch, conn, tid)
        out = _tool("schedule", {"reason": "waiting for build 559", "awaiting": BUILD, "wake_check": "false"})
        assert out.get("status") == "scheduled", out


def test_schedule_wake_check_that_outlives_the_probe_timeout_parks(kanban_home, monkeypatch, dry_run) -> None:
    monkeypatch.setattr(kwc, "PROBE_TIMEOUT_SECONDS", 1)
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _as_worker(monkeypatch, conn, tid)
        out = _tool("schedule", {"reason": "waiting for build 559", "awaiting": BUILD, "wake_check": "sleep 5"})
        assert out.get("status") == "scheduled", out


def test_continue_with_a_wait_and_a_true_wake_check_is_refused(kanban_home, monkeypatch, dry_run) -> None:
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _as_worker(monkeypatch, conn, tid)
        out = _tool("continue", {"checkpoint": "Pushed; waiting for build 559.", "resume_after_seconds": 3600,
                                 "awaiting": BUILD, "wake_check": "true"})
        assert kwc.WAKE_CHECK_ALREADY_TRUE in out["error"], out
        assert kb.get_task(conn, tid).status == "running"


def test_block_time_with_a_true_wake_check_is_refused_and_points_to_clear_blocker(
        kanban_home, monkeypatch, dry_run) -> None:
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _as_worker(monkeypatch, conn, tid)
        out = _tool("block", {"kind": "time", "reason": "SCHEDULED_UNTIL=2099-01-01T00:00:00Z CI billing wall",
                              "blocker_id": "github-actions-billing", "awaiting": BUILD, "wake_check": "true"})
        assert kwc.WAKE_CHECK_ALREADY_TRUE in out["error"], out
        assert "clear_blocker=true" in out["error"] and "github-actions-billing" in out["error"], out
        assert kb.get_task(conn, tid).status == "running"


def test_cli_schedule_with_a_true_wake_check_exits_nonzero(kanban_home, capsys, dry_run) -> None:
    from hermes_cli import kanban as kcli

    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    kcli.build_parser(parser.add_subparsers(dest="command"))
    with kbc.connect_closing() as conn:
        tid = _running(conn)
    rc = kcli.kanban_command(parser.parse_args([
        "kanban", "schedule", tid, "SCHEDULED_UNTIL=2099-01-01T00:00:00Z waiting for build 559",
        "--wake-check", "true", "--awaiting", "ci:40443505012", "--awaiting-why", "in progress"]))
    assert rc != 0
    assert kwc.WAKE_CHECK_ALREADY_TRUE in capsys.readouterr().err
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "running"
