"""A core update's drain restart is cancelled when that update gives up (t_01a189db034f3b2b).

hermes-gateway-launcher SIGUSR1s the gateway when hermes-sync publishes ``<home>/core-update/intent``;
hermes-sync removes the intent when the gateway drain times out. Before this fix the deferred restart
still ran when the last work unit finished, relaunching the OLD code (David-MBP 2026-10-29 15:47:29).
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.gateway.restart_test_helpers import make_restart_runner


async def _let_the_wait_loop_run():
    for _ in range(5):
        await asyncio.sleep(0)


def _intent(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = tmp_path / "core-update" / "intent"
    path.parent.mkdir(parents=True)
    path.write_text('{"pid": 1}\n')
    return path


@pytest.mark.asyncio
async def test_update_restart_is_cancelled_when_the_intent_goes_away(tmp_path, monkeypatch):
    intent = _intent(tmp_path, monkeypatch)
    runner, _adapter = make_restart_runner()
    runner.stop = AsyncMock()
    runner._restart_after_turn_timeout = 30.0
    session_key = "agent:main:telegram:dm:1"
    runner._running_agents[session_key] = MagicMock()

    assert runner.request_restart(detached=False, via_service=True) is True
    assert runner._draining is True
    await _let_the_wait_loop_run()
    intent.unlink()  # the updater gave up on the drain
    await asyncio.wait_for(runner._restart_task, 5)

    runner.stop.assert_not_awaited()
    assert runner._draining is False
    assert runner._restart_requested is False
    # The work unit finishing later must not restart anything either: the restart task is over.
    del runner._running_agents[session_key]
    await _let_the_wait_loop_run()
    assert runner._restart_task.done()
    runner.stop.assert_not_awaited()
    # The next update's SIGUSR1 can request a fresh restart.
    intent.write_text('{"pid": 2}\n')
    assert runner.request_restart(detached=False, via_service=True) is True
    await asyncio.wait_for(runner._restart_task, 5)
    runner.stop.assert_awaited_once_with(restart=True, detached_restart=False, service_restart=True)


@pytest.mark.asyncio
async def test_update_restart_proceeds_when_work_drains_while_the_intent_stands(tmp_path, monkeypatch):
    _intent(tmp_path, monkeypatch)
    runner, _adapter = make_restart_runner()
    runner.stop = AsyncMock()
    runner._restart_after_turn_timeout = 30.0
    session_key = "agent:main:telegram:dm:1"
    runner._running_agents[session_key] = MagicMock()

    assert runner.request_restart(detached=False, via_service=True) is True
    await _let_the_wait_loop_run()
    del runner._running_agents[session_key]
    await asyncio.wait_for(runner._restart_task, 5)
    runner.stop.assert_awaited_once_with(restart=True, detached_restart=False, service_restart=True)


@pytest.mark.asyncio
async def test_restart_without_an_update_intent_is_never_cancelled(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))  # no core-update/intent: a user restart
    runner, _adapter = make_restart_runner()
    runner.stop = AsyncMock()
    runner._restart_after_turn_timeout = 30.0
    session_key = "agent:main:telegram:dm:1"
    runner._running_agents[session_key] = MagicMock()

    assert runner.request_restart(detached=False, via_service=True) is True
    await _let_the_wait_loop_run()
    runner.stop.assert_not_awaited()
    del runner._running_agents[session_key]
    await asyncio.wait_for(runner._restart_task, 5)
    runner.stop.assert_awaited_once()
