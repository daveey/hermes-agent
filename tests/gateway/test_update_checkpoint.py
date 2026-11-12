"""Instant updates negotiate a bounded checkpoint, never waive unknown admission."""
import asyncio
import json
import os
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from hermes_cli import admission_fence


@pytest.fixture
def checkpoint(tmp_path, monkeypatch):
    from gateway import update_checkpoint as checkpoint
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(checkpoint, "get_hermes_home", lambda: home)
    monkeypatch.setattr(checkpoint, "get_default_hermes_root", lambda **kw: home)
    monkeypatch.setattr(checkpoint, "known_cron_reservations", lambda: set())
    monkeypatch.setattr(checkpoint, "kanban_managed_count", lambda runner: 1)
    monkeypatch.setattr(checkpoint, "preserve_cron_slots", Mock())
    runner = SimpleNamespace(
        config=object(), request_restart=Mock(return_value=True),
        _active_work_count=lambda: 1,
        _active_cron_job_count=lambda: 0,
    )
    admission_fence.set_hold(home)
    intent = {"pid": os.getpid(), "kind": "unfenced", "checkpoint": True,
              "cron_cut": True, "created_at": time.time(), "expires_at": time.time() + 120,
              "nonce": "episode-one"}
    (home / "core-update/intent").write_text(json.dumps(intent))
    return checkpoint, home, runner, intent


def invoke(checkpoint, runner, nonce="episode-one", before=None):
    def schedule(callback):
        if before:
            before()
        callback()
    return checkpoint.checkpoint_for_update(
        runner, SimpleNamespace(call_soon_threadsafe=schedule), {"nonce": nonce})


def test_known_managed_work_uses_existing_restart_checkpoint(checkpoint):
    module, home, runner, _intent = checkpoint
    result = invoke(module, runner)
    assert result["pausing"] and result["checkpoint"]
    assert result["drain_timeout"] >= 60
    runner.request_restart.assert_called_once_with(detached=False, via_service=True, update_intent=home / "core-update/intent")


@pytest.mark.parametrize("change", ["no-hold", "expired", "dead-owner", "wrong-kind", "no-checkpoint", "nonce"])
def test_invalid_episode_never_starts_restart(checkpoint, change):
    module, home, runner, intent = checkpoint
    if change == "no-hold":
        admission_fence.clear_hold(home)
    changes = {"expired": {"expires_at": time.time() - 1}, "dead-owner": {"pid": -1},
               "wrong-kind": {"kind": "fenced"}, "no-checkpoint": {"checkpoint": False}}
    intent.update(changes.get(change, {}))
    (home / "core-update/intent").write_text(json.dumps(intent))
    result = invoke(module, runner, nonce="wrong" if change == "nonce" else "episode-one")
    assert not result["pausing"]
    runner.request_restart.assert_not_called()


def test_unpublished_reservation_is_not_covered_by_known_cron(checkpoint, monkeypatch):
    module, home, runner, _intent = checkpoint
    known = admission_fence.extend(home)
    unknown = admission_fence.extend(home)
    monkeypatch.setattr(module, "known_cron_reservations", lambda: {known.path.name})
    try:
        result = invoke(module, runner)
        assert not result["pausing"]
        runner.request_restart.assert_not_called()
    finally:
        known.close()
        unknown.close()


def test_known_cron_reservation_can_cross_only_cut_authorized_episode(checkpoint, monkeypatch):
    module, home, runner, intent = checkpoint
    known = admission_fence.extend(home)
    monkeypatch.setattr(module, "known_cron_reservations", lambda: {known.path.name})
    runner._active_work_count = lambda: 2
    runner._active_cron_job_count = lambda: 1
    try:
        assert invoke(module, runner)["pausing"]
        runner.request_restart.reset_mock()
        intent["cron_cut"] = False
        (home / "core-update/intent").write_text(json.dumps(intent))
        assert not invoke(module, runner)["pausing"]
        runner.request_restart.assert_not_called()
    finally:
        known.close()


def test_unknown_work_in_callback_is_not_tolerated(checkpoint):
    module, _home, runner, _intent = checkpoint
    def racing_work():
        runner._active_work_count = lambda: 2
    assert not invoke(module, runner, before=racing_work)["pausing"]
    runner.request_restart.assert_not_called()


@pytest.mark.parametrize("cron_cut, turns, accepted", [
    (True, 1, True),    # the agent turn of the cut cron run: cut with it
    (False, 1, False),  # no cron cut: the turn is awaited like any work
    (True, 0, False),   # the extra unit is not a cron turn: unknown work stays refused
])
def test_agent_cron_turn_is_cut_with_its_run(checkpoint, monkeypatch, cron_cut, turns, accepted):
    """hermes-hub 2026-11-12 10:36-10:39Z: an hourly agent cron (kanban-hub-pin-sweep) counted once as
    its scheduler run and once as its owner turn, so every checkpoint of its 11-minute run was refused."""
    module, home, runner, intent = checkpoint
    intent["cron_cut"] = cron_cut
    (home / "core-update/intent").write_text(json.dumps(intent))
    runner._active_work_count = lambda: 3  # 1 Kanban card + 1 cron run + its agent turn
    runner._active_cron_job_count = lambda: 1
    monkeypatch.setattr(module, "cron_turn_count", lambda r: turns, raising=False)
    assert bool(invoke(module, runner)["pausing"]) is accepted
    assert runner.request_restart.called is accepted


def test_reservation_added_before_callback_is_rechecked(checkpoint):
    module, home, runner, _intent = checkpoint
    reservations = []
    try:
        result = invoke(module, runner, before=lambda: reservations.append(admission_fence.extend(home)))
        assert not result["pausing"]
        runner.request_restart.assert_not_called()
    finally:
        for reservation in reservations:
            reservation.close()


@pytest.mark.parametrize("stage", ["preserve", "restart"])
def test_timeout_cancels_unclaimed_but_settles_claimed_callback(checkpoint, monkeypatch, stage):
    import threading
    module, _home, runner, intent = checkpoint
    entered, release = threading.Event(), threading.Event()
    threads = []
    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return True
    monkeypatch.setattr(module, "_REQUEST_WAIT_S", 0.1)
    if stage == "preserve":
        monkeypatch.setattr(module, "preserve_cron_slots", blocked)
    else:
        runner.request_restart.side_effect = blocked
    def schedule(callback):
        thread = threading.Thread(target=callback)
        threads.append(thread)
        thread.start()
        assert entered.wait(3)
    try:
        result = module.checkpoint_for_update(runner, SimpleNamespace(call_soon_threadsafe=schedule),
                                              {"nonce": intent["nonce"]})
        assert threads[0].is_alive()  # handler did not wait for a claimed callback
        if stage == "preserve":
            assert result["busy"] and not result["already_stopping"]
        else:
            assert result["already_stopping"] and result["drain_timeout"] == 70
    finally:
        release.set()
        for thread in threads:
            thread.join(3)
            assert not thread.is_alive()
    if stage == "preserve":
        runner.request_restart.assert_not_called()


def test_cron_inventory_lock_acquisition_is_bounded(monkeypatch):
    import threading
    from cron import scheduler
    from gateway.update_checkpoint import known_cron_reservations
    lock = threading.Lock()
    monkeypatch.setattr(scheduler, "_running_lock", lock)
    lock.acquire()
    try:
        with pytest.raises(TimeoutError):
            known_cron_reservations()
    finally:
        lock.release()
    assert known_cron_reservations() == set()



@pytest.mark.parametrize("stage", ["during-preserve", "after-snapshot"])
def test_withdrawal_after_admission_cancels_real_restart_without_stop(checkpoint, monkeypatch, stage):
    """Real runner: an intent removed after admission must not be rediscovered as 'no update'."""
    from unittest.mock import AsyncMock
    from tests.gateway.restart_test_helpers import make_restart_runner
    module, home, _fake, _intent = checkpoint
    path = home / "core-update/intent"
    runner, _adapter = make_restart_runner()
    runner.stop = AsyncMock()
    runner.config = object()
    monkeypatch.setattr(module, "kanban_managed_count", lambda r: 0)
    if stage == "during-preserve":
        monkeypatch.setattr(module, "preserve_cron_slots", lambda config: path.unlink())
    else:
        real = runner.request_restart
        def withdrawn(**kw):
            accepted = real(**kw)
            path.unlink()
            return accepted
        monkeypatch.setattr(runner, "request_restart", withdrawn)
    async def scenario():
        result = module.checkpoint_for_update(
            runner, SimpleNamespace(call_soon_threadsafe=lambda cb: cb()), {"nonce": "episode-one"})
        assert result["pausing"] is True
        assert runner._restart_update_intent == path
        await asyncio.wait_for(runner._restart_task, 5)
    asyncio.run(scenario())
    runner.stop.assert_not_awaited()
    assert runner._restart_requested is False and runner._draining is False


@pytest.mark.parametrize("change, cause", [
    ("no-hold", "no live instant-update intent"),
    ("nonce", "nonce is not the live update intent's"),
])
def test_invalid_episode_refusal_names_its_check(checkpoint, change, cause):
    module, home, runner, _intent = checkpoint
    if change == "no-hold":
        admission_fence.clear_hold(home)
    result = invoke(module, runner, nonce="wrong" if change == "nonce" else "episode-one")
    assert result["busy"] and cause in result["reason"]


def test_unknown_work_refusal_names_the_kinds_and_chat_sessions(checkpoint):
    module, _home, runner, _intent = checkpoint
    runner._active_work_count = lambda: 3
    runner._running_agent_count = lambda: 1
    runner._running_agents = {"agent:main:local:dm:local-7829": object()}
    runner._active_api_run_count = lambda: 1
    result = invoke(module, runner)
    assert not result["pausing"]
    assert result["reason"].startswith("unpreservable work: 1 chat turn (agent:main:local:dm:local-7829); "
                                       "1 API-server run (active=3, preservable managed=1, cron=0)")


def test_live_delegation_refusal_says_so(checkpoint, monkeypatch):
    module, _home, runner, _intent = checkpoint
    monkeypatch.setattr("gateway.run_active_work_verb._delegation_units", lambda now: [{"id": "d1"}])
    result = invoke(module, runner)
    assert "1 live async delegation(s)" in result["reason"]
