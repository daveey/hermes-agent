"""2026-11-20 sync outage: the supervisor gave up on kanban_sync_watcher after 5 rapid deaths and the whole host
stopped syncing until its gateway restarted. A watcher listed in ``_SUPERVISED_SLOW_RETRY_SECS`` is never abandoned:
after the budget it respawns with a fresh budget."""
from __future__ import annotations

import asyncio

import pytest

from gateway.run import GatewayRunner
from tests.gateway.test_platform_reconnect import _make_runner


async def _settle(times=300):
    for _ in range(times):
        await asyncio.sleep(0)


def _runner(monkeypatch):
    runner = _make_runner()
    runner._background_tasks = set()
    runner._running = True
    monkeypatch.setattr(GatewayRunner, "_MAX_SUPERVISED_RESTARTS", 2)
    monkeypatch.setattr(GatewayRunner, "_SUPERVISED_HEALTHY_SECS", 3600)
    monkeypatch.setattr(GatewayRunner, "_supervised_backoff", staticmethod(lambda _a: 0))
    return runner


def _crashing(crashes):
    state = {"spawns": 0, "live": 0, "left": crashes}

    async def watcher():
        state["spawns"] += 1
        if state["left"] > 0:
            state["left"] -= 1
            raise RuntimeError("ContainmentRefused-like crash")
        state["live"] += 1
        await asyncio.Event().wait()

    return state, watcher


@pytest.mark.asyncio
async def test_sync_watcher_respawns_after_giving_up(monkeypatch):
    runner = _runner(monkeypatch)
    monkeypatch.setattr(GatewayRunner, "_SUPERVISED_SLOW_RETRY_SECS", {"kanban_sync_watcher": 0})
    gave_up = []
    state, watcher = _crashing(crashes=4)  # past the 2-restart budget: the give-up branch is reached
    runner._spawn_supervised(watcher, "kanban_sync_watcher", on_give_up=gave_up.append)
    await _settle()
    assert gave_up == ["kanban_sync_watcher"]  # the existing give-up hook still fires
    assert state["live"] == 1, "the slow tier must bring the watcher back once it stops crashing"
    for t in list(runner._background_tasks):
        t.cancel()


@pytest.mark.asyncio
async def test_unlisted_watcher_still_gives_up_for_good(monkeypatch):
    runner = _runner(monkeypatch)
    monkeypatch.setattr(GatewayRunner, "_SUPERVISED_SLOW_RETRY_SECS", {"kanban_sync_watcher": 0})
    state, watcher = _crashing(crashes=4)
    runner._spawn_supervised(watcher, "some_other_watcher")
    await _settle()
    assert state["live"] == 0 and state["spawns"] == 3  # unchanged behaviour for other tasks


@pytest.mark.asyncio
async def test_no_slow_respawn_after_shutdown(monkeypatch):
    runner = _runner(monkeypatch)
    monkeypatch.setattr(GatewayRunner, "_SUPERVISED_SLOW_RETRY_SECS", {"kanban_sync_watcher": 0})
    state, watcher = _crashing(crashes=3)

    def stop(_name):
        runner._running = False

    runner._spawn_supervised(watcher, "kanban_sync_watcher", on_give_up=stop)
    await _settle()
    assert state["live"] == 0
