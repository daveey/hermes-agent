"""Supervised READY includes the dashboard; idle-exit startup stays lazy."""
import asyncio
import argparse
from contextlib import asynccontextmanager
import sys
from types import SimpleNamespace

import pytest

from gateway import run_api, run_bootstrap, run_runtime
from gateway.config import GatewayConfig
from tests.gateway import test_bootstrap_deferred_integrity_scan as helpers

_startup = helpers._startup
_REAL_WARM = run_api.warm_gateway_dashboard


@pytest.fixture
def dashboard(_startup, monkeypatch):
    events, _, _ = _startup
    entered, release = asyncio.Event(), asyncio.Event()
    state = SimpleNamespace(attempts=0, fail=False, runner=None)

    @asynccontextmanager
    async def lifespan(app):
        state.attempts += 1
        events.append("mount_started")
        entered.set()
        await release.wait()
        if state.fail and state.attempts == 1:
            raise RuntimeError("mount failed")
        events.append("mount_loaded")
        yield

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200})
        await send({"type": "http.response.body", "body": b"ok"})

    app.state = SimpleNamespace()
    app.router = SimpleNamespace(lifespan_context=lifespan)
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server", SimpleNamespace(app=app))

    async def start_api(runner):
        state.runner = runner
        mount = run_api.GatewayDashboardMount(runner, SimpleNamespace())
        monkeypatch.setattr(mount, "_bind_state", lambda web: None)
        monkeypatch.setattr(mount, "_unbind_state", lambda: None)
        runner.session_api = SimpleNamespace(app=mount)

    monkeypatch.setattr(run_runtime, "start_gateway_runtime_api", start_api)
    monkeypatch.setattr(run_runtime, "drain_gateway_runtime", helpers.AsyncMock())
    monkeypatch.setattr(run_api, "warm_gateway_dashboard", _REAL_WARM)
    yield events, entered, release, state


def boot(**kwargs):
    return run_bootstrap.start_gateway(config=GatewayConfig(), replace=False, verbosity=None, **kwargs)


@pytest.mark.asyncio
async def test_supervised_ready_waits_for_mount(dashboard):
    events, entered, release, state = dashboard
    task = asyncio.create_task(boot())
    try:
        await asyncio.wait_for(entered.wait(), 10)
        assert "ready" not in events
        release.set()
        assert await task is True
        assert state.runner.session_api.app.loaded
        assert events.index("mount_loaded") < events.index("ready")
    finally:
        release.set()
        await task
        await state.runner.session_api.app.close()


@pytest.mark.asyncio
async def test_timeout_publishes_ready_without_cancelling_mount(dashboard, monkeypatch, caplog):
    events, entered, release, state = dashboard
    real_wait = getattr(run_api, "await_gateway_dashboard_warm", None)
    assert real_wait is not None
    waits = []

    async def bounded(runner):
        waits.append(asyncio.get_running_loop().time())
        await real_wait(runner, timeout=0.02)
        waits.append(asyncio.get_running_loop().time())

    monkeypatch.setattr(run_api, "await_gateway_dashboard_warm", bounded)
    try:
        assert await asyncio.wait_for(boot(), 10) is True
        assert entered.is_set() and "ready" in events and "mount_loaded" not in events
        assert waits[1] - waits[0] >= 0.02
        assert "publishing READY" in caplog.text and "still running" in caplog.text
        warm = state.runner._gateway_dashboard_warmup
        assert not warm.done()
        release.set()
        await asyncio.wait_for(warm, 5)
        assert state.runner.session_api.app.loaded
        assert events.index("ready") < events.index("mount_loaded")
    finally:
        release.set()
        await asyncio.gather(state.runner._gateway_dashboard_warmup, return_exceptions=True)
        await state.runner.session_api.app.close()


@pytest.mark.asyncio
async def test_failed_mount_warns_before_ready_and_first_request_retries(dashboard, caplog):
    events, _, release, state = dashboard
    state.fail = True
    release.set()
    assert await boot() is True
    mount = state.runner.session_api.app
    try:
        assert state.attempts == 1 and not mount.loaded
        assert events.index("mount_started") < events.index("ready")
        assert "loads on first request" in caplog.text
        sent = []

        async def send(message):
            sent.append(message)

        await mount({"type": "http"}, helpers.AsyncMock(), send)
        assert state.attempts == 2 and mount.loaded
        assert sent[0]["status"] == 200
    finally:
        await mount.close()


@pytest.mark.asyncio
async def test_idle_exit_does_not_warm_dashboard(dashboard, monkeypatch):
    events, _, _, state = dashboard
    monkeypatch.delitem(sys.modules, "hermes_cli.web_server", raising=False)
    monkeypatch.setattr(run_api, "preload_session_socket", lambda: None)
    monkeypatch.setattr("gateway.run_idle_exit.arm_unmanaged_idle_exit", lambda runner: None)
    assert await boot(idle_exit=True) is True
    assert "ready" in events and state.attempts == 0
    assert not hasattr(state.runner, "_gateway_dashboard_warmup")
    assert "hermes_cli.web_server" not in sys.modules


@pytest.mark.asyncio
async def test_early_verdict_awaits_started_warmup(dashboard, monkeypatch):
    events, _, release, state = dashboard
    monkeypatch.setattr(run_bootstrap, "_start_gateway_run_runner", helpers.AsyncMock(return_value=False))
    release.set()
    assert await boot() is False
    try:
        assert state.runner._gateway_dashboard_warmup.done()
        assert state.runner.session_api.app.loaded and "ready" not in events
    finally:
        await state.runner.session_api.app.close()


@pytest.mark.asyncio
async def test_warmup_overlaps_adapter_start(dashboard, monkeypatch):
    _, entered, release, state = dashboard

    async def start_runner(runner, signal_shutdown):
        await asyncio.wait_for(entered.wait(), 5)
        release.set()

    monkeypatch.setattr(run_bootstrap, "_start_gateway_run_runner", start_runner)
    assert await boot() is True
    await state.runner.session_api.app.close()


@pytest.mark.asyncio
async def test_missing_warmup_is_noop(caplog):
    await run_api.await_gateway_dashboard_warm(SimpleNamespace())
    assert not caplog.text


@pytest.mark.asyncio
async def test_unexpected_warmup_failure_is_nonfatal(caplog):
    async def fail():
        raise RuntimeError("unexpected warmup failure")

    runner = SimpleNamespace(_gateway_dashboard_warmup=asyncio.create_task(fail()))
    await run_api.await_gateway_dashboard_warm(runner)
    assert "loads on first request" in caplog.text


@pytest.mark.parametrize("verb", ["ensure", "attach"])
def test_cli_deadline_covers_starved_start_and_dashboard_barrier(verb):
    from hermes_cli.gateway_runtime_progress import MAX_ADAPTIVE_TIMEOUT
    from hermes_cli.subcommands.gateway import build_gateway_parser

    parser = argparse.ArgumentParser()
    build_gateway_parser(parser.add_subparsers(), cmd_gateway=lambda: None,
                         cmd_proxy=lambda: None, cmd_gateway_enroll=lambda: None)
    assert float(parser.parse_args(["gateway", verb]).timeout) >= MAX_ADAPTIVE_TIMEOUT + 30
    assert parser.parse_args(["gateway", verb, "--timeout", "2"]).timeout == "2"
