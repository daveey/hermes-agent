"""An unplanned stop signal is logged as ``gateway.exit_signal``, not a bare ``gateway.exit_nonzero``.

``start_gateway()`` returns False for a SIGTERM with no planned-stop marker (``launchctl bootout``,
``kickstart -k``, ``systemctl stop``) so the service manager revives the gateway. ``run_gateway()``
then wrote ``gateway.exit_nonzero`` with no signal or parent, so a supervisor bounce, a refused start
and a restart loop looked identical and the self-heal detector filed every planned restart as a
crash (mini, Oct 2026, defect gateway-3cf2256311). The exit code must stay 1 either way.
"""

from __future__ import annotations

import json
import os
import signal
import types
from pathlib import Path

import pytest


class _HardExit(BaseException):
    def __init__(self, code: int):
        super().__init__(code)
        self.code = code


def _rows() -> list[dict]:
    path = Path(os.environ["HERMES_HOME"]) / "logs" / "gateway-exit-diag.log"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _runner(*, fail: bool = False):
    async def _stop():
        return None

    return types.SimpleNamespace(
        _signal_initiated_shutdown=False, _restart_requested=False, _restart_via_service=False,
        should_exit_with_failure=fail, exit_reason="no platform connected" if fail else None,
        exit_code=None, stop=_stop,
    )


def _prepare(monkeypatch, runner, sig):
    """Run the real CLI entry with a fake asyncio.run that drives the real signal handler + verdict."""
    import gateway.run as gateway_run
    import gateway.shutdown_forensics as forensics
    import hermes_cli.gateway as gateway_cli
    from gateway import status
    from gateway.run_shutdown import _resolve_gateway_exit_verdict

    monkeypatch.setattr(gateway_cli, "_guard_official_docker_root_gateway", lambda: None)
    monkeypatch.setattr(gateway_cli, "_guard_named_profile_under_multiplexer", lambda force=False: None)
    monkeypatch.setattr(gateway_cli, "_attach_to_host_gateway_or_guard", lambda **kwargs: None)
    monkeypatch.setattr(gateway_cli, "_guard_supervised_gateway_conflict", lambda force=False: None)
    monkeypatch.setattr(gateway_cli, "_guard_existing_gateway_process_conflict", lambda replace=False: None)
    monkeypatch.setattr(gateway_cli, "_respawn_storm_backoff", lambda: None)
    monkeypatch.setattr(gateway_cli, "supports_systemd_services", lambda: False)
    monkeypatch.setattr(gateway_cli.sys, "stdin", types.SimpleNamespace(isatty=lambda: False))
    monkeypatch.setenv("HERMES_GATEWAY_EXIT_DIAG", "1")
    monkeypatch.setattr(status, "consume_takeover_marker_for_self", lambda: False)
    monkeypatch.setattr(status, "consume_planned_stop_marker_for_self", lambda: False)
    monkeypatch.setattr(forensics, "spawn_async_diagnostic", lambda *a, **k: None)
    monkeypatch.setattr(gateway_run.asyncio, "create_task", lambda coro: coro.close())

    def _hard_exit(code: int) -> None:
        raise _HardExit(code)

    monkeypatch.setattr(gateway_run, "_exit_after_graceful_shutdown", _hard_exit)

    def _fake_run(coro):
        coro.close()
        flag = [False]
        if sig is not None:
            gateway_run._start_gateway_make_shutdown_signal_handler(runner, flag)(sig)
        return _resolve_gateway_exit_verdict(runner, flag[0])

    monkeypatch.setattr(gateway_cli.asyncio, "run", _fake_run)
    return gateway_cli


def test_unplanned_sigterm_writes_exit_signal_with_signal_and_parent(monkeypatch):
    gateway_cli = _prepare(monkeypatch, _runner(), signal.SIGTERM)
    with pytest.raises(_HardExit) as exc:
        gateway_cli.run_gateway()
    assert exc.value.code == 1  # launchd/systemd still revive it
    last = _rows()[-1]
    assert last["tag"] == "gateway.exit_signal"
    assert last["signal"] == "SIGTERM"
    assert last["level"] == "info"
    assert last["ppid"] == os.getppid()
    assert "gateway.exit_nonzero" not in [r["tag"] for r in _rows()]


def test_refused_start_still_writes_exit_nonzero(monkeypatch):
    """A failure verdict (no platform connected) is a real failure even when a signal also arrived."""
    gateway_cli = _prepare(monkeypatch, _runner(fail=True), signal.SIGTERM)
    with pytest.raises(_HardExit) as exc:
        gateway_cli.run_gateway()
    assert exc.value.code == 1
    tags = [r["tag"] for r in _rows()]
    assert tags[-1] == "gateway.exit_nonzero"
    assert "gateway.exit_signal" not in tags


def test_refused_start_without_signal_writes_exit_nonzero(monkeypatch):
    gateway_cli = _prepare(monkeypatch, _runner(fail=True), None)
    with pytest.raises(_HardExit) as exc:
        gateway_cli.run_gateway()
    assert exc.value.code == 1
    assert _rows()[-1]["tag"] == "gateway.exit_nonzero"
