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


# --- [daveey] every False exit names its cause (defect gateway-3cf2256311 came back) -------------------------
# The fix above named two causes (stop signal, refused start). Every other False return of
# start_gateway() still wrote a bare ``gateway.exit_nonzero`` with no cause, so a new cause (mini,
# 10-29 22:01 PT, after a reboot) was filed as a recurrence of the fixed one and nobody could read why.


@pytest.fixture(autouse=True)
def _reset_exit_cause():
    from gateway import run_exit_reason
    reset = getattr(run_exit_reason, "reset_exit_cause", None)
    if reset is not None:
        reset()
    run_exit_reason.reset_start_refusal()
    yield
    if reset is not None:
        reset()


def test_failure_verdict_records_cause_and_exit_reason(monkeypatch):
    runner = _runner(fail=True)
    runner.exit_reason = "All messaging adapters disconnected"
    gateway_cli = _prepare(monkeypatch, runner, None)
    with pytest.raises(_HardExit) as exc:
        gateway_cli.run_gateway()
    assert exc.value.code == 1  # exit code unchanged: launchd/systemd behave the same
    last = _rows()[-1]
    assert last["tag"] == "gateway.exit_nonzero"
    assert last["cause"] == "runner_failure"
    assert last["exit_reason"] == "All messaging adapters disconnected"


def test_false_exit_with_no_cause_note_says_unrecorded():
    from hermes_cli.gateway_exit_diag import nonzero_exit_record
    tag, extra = nonzero_exit_record()
    assert tag == "gateway.exit_nonzero"
    assert extra.get("cause") == "unrecorded"


def test_early_start_gateway_false_records_its_cause(monkeypatch):
    """A start_gateway() abort before the runner exists (profile reservation failed) names its cause."""
    from gateway import run_bootstrap
    from hermes_cli.gateway_exit_diag import nonzero_exit_record

    def _disk_full(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(run_bootstrap, "_reserve_waiting_out_browse_reconcile", _disk_full)
    assert run_bootstrap._start_gateway_reserve_profiles([]) is False
    tag, extra = nonzero_exit_record()
    assert tag == "gateway.exit_nonzero"
    assert extra["cause"] == "profile_reservation_failed"
    assert "disk full" in extra["exit_reason"]


_NOTE_CALLS = {"note_exit_cause", "note_start_refused"}


def _false_returns_without_note(func) -> list[int]:
    """Line numbers of ``return False`` statements in ``func`` with no cause note earlier in their block."""
    import ast

    def _notes(stmt) -> bool:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Call):
                fn = node.func
                name = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else ""
                if name in _NOTE_CALLS:
                    return True
        return False

    bad: list[int] = []

    def _scan(stmts):
        for i, stmt in enumerate(stmts):
            if (isinstance(stmt, ast.Return) and isinstance(stmt.value, ast.Constant)
                    and stmt.value.value is False and not any(_notes(s) for s in stmts[:i])):
                bad.append(stmt.lineno)
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue  # nested defs return to their own caller, not to start_gateway()
            for field in ("body", "orelse", "finalbody"):
                _scan(getattr(stmt, field, None) or [])
            for handler in getattr(stmt, "handlers", None) or []:
                _scan(handler.body)

    _scan(func.body)
    return bad


@pytest.mark.parametrize("module, qualname", [
    ("gateway.run_bootstrap", "start_gateway"),
    ("gateway.run_bootstrap", "_start_gateway_run_runner"),
    ("gateway.run_bootstrap", "_start_gateway_reserve_profiles"),
    ("gateway.run_bootstrap", "_start_gateway_claim_pid_file"),
    ("gateway.run_bootstrap", "_start_gateway_replace_existing_instance"),
    ("gateway.run_bootstrap", "_launch_home_may_multiplex"),
    ("gateway.run_shutdown", "_resolve_gateway_exit_verdict"),
    ("gateway.run_startup", "GatewayStartupMixin.start"),
    ("gateway.run_startup", "GatewayStartupMixin._start_impl"),
    ("gateway.run", "_host_attach_or_none"),
])
def test_every_false_exit_path_notes_a_cause(module, qualname):
    """Guard: a new ``return False`` on the gateway's exit path must say why (note_exit_cause)."""
    import ast
    import importlib
    import inspect
    import textwrap

    obj = importlib.import_module(module)
    for part in qualname.split("."):
        obj = getattr(obj, part)
    src = textwrap.dedent(inspect.getsource(obj))
    func = ast.parse(src).body[0]
    first = inspect.getsourcelines(obj)[1]
    bad = [first + n - 1 for n in _false_returns_without_note(func)]
    assert not bad, f"{module}.{qualname}: `return False` without note_exit_cause() at lines {bad}"
