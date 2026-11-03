"""Restart vs stop picks the gateway's kill source in both shutdown sweeps. [daveey]

A restart passes ``gateway_restart`` (a lifecycle source that spares persist_on_release jobs, which
the next gateway re-adopts); a stop keeps ``gateway_shutdown``. Drives the real
``_stop_interrupt_remaining_work`` (post-interrupt) and ``_stop_kill_tool_subprocesses_off_loop``
(final-cleanup) entry points.
"""

import time

import pytest

from gateway.run_shutdown import GatewayShutdownMixin
from tests.gateway.restart_test_helpers import make_restart_runner


def _runner(monkeypatch, sources, *, restarting):
    runner, _adapter = make_restart_runner()
    runner._restart_drain_timeout = 0.01
    runner._restart_requested = restarting

    def _fake_kill_all(task_id=None, **kwargs):
        sources.append(kwargs.get("source"))
        return 0

    import tools.process_registry as _pr

    monkeypatch.setattr(_pr.process_registry, "kill_all", _fake_kill_all)
    monkeypatch.setattr("cron.scheduler.mark_running_jobs_interrupted", lambda *a, **k: [])
    monkeypatch.setattr("tools.async_delegation.interrupt_all", lambda *a, **k: 0)
    monkeypatch.setattr("tools.terminal_tool_lifecycle.cleanup_all_environments", lambda: None)
    monkeypatch.setattr("tools.browser_session_lifecycle.cleanup_all_browsers", lambda *, process_shutdown=False: None)
    return runner


@pytest.mark.asyncio
@pytest.mark.parametrize(("restarting", "source"), [(True, "gateway_restart"), (False, "gateway_shutdown")])
async def test_post_interrupt_sweep_source_follows_restart(monkeypatch, restarting, source):
    sources: list = []
    runner = _runner(monkeypatch, sources, restarting=restarting)
    ctx = GatewayShutdownMixin._StopContext(deferred_count=lambda: 0)
    ctx.started_at = time.monotonic()

    await runner._stop_interrupt_remaining_work(ctx)

    assert sources == [source]


@pytest.mark.asyncio
@pytest.mark.parametrize(("restarting", "source"), [(True, "gateway_restart"), (False, "gateway_shutdown")])
async def test_off_loop_sweep_forwards_restarting(monkeypatch, restarting, source):
    sources: list = []
    _runner(monkeypatch, sources, restarting=restarting)

    await GatewayShutdownMixin._stop_kill_tool_subprocesses_off_loop("final-cleanup", restarting=restarting)

    assert sources == [source]


def test_restart_source_is_a_lifecycle_sweep():
    from tools.process_registry import ProcessRegistry

    assert "gateway_restart" in ProcessRegistry._LIFECYCLE_KILL_SOURCES
    assert "gateway_shutdown" not in ProcessRegistry._LIFECYCLE_KILL_SOURCES
