"""A refused start is logged as ``gateway.start_refused`` with its reason, not a bare ``exit_nonzero``.

``start_gateway()`` returns False when another gateway already owns this home or host: a live
duplicate (``already_running``), a lost runtime-lock / PID-file race (``pid_claim``) or a host owner
that refuses a second gateway (``host_refused``). ``run_gateway()`` wrote all of them as a bare
``gateway.exit_nonzero``, the same line a crash writes, so the self-heal detector filed a refused
second ``gateway run`` next to a live gateway as "died uncleanly" (MacBook, Oct 2026, defect
gateway-6efc15a270). Each refusal now carries its reason, the owner's pid and whether that owner is
alive; the exit code stays 1 so launchd/systemd behave as before.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

from gateway.run_exit_reason import reset_start_refusal


class _HardExit(BaseException):
    def __init__(self, code: int):
        super().__init__(code)
        self.code = code


def _rows() -> list[dict]:
    path = Path(os.environ["HERMES_HOME"]) / "logs" / "gateway-exit-diag.log"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def live_pid():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        yield proc.pid
    finally:
        proc.kill()
        proc.wait()


def _prepare_cli(monkeypatch):
    """The real ``run_gateway`` entry with its pre-start guards and hard exit stubbed out."""
    import gateway.run as gateway_run
    import hermes_cli.gateway as gateway_cli

    reset_start_refusal()
    monkeypatch.setattr(gateway_cli, "_guard_official_docker_root_gateway", lambda: None)
    monkeypatch.setattr(gateway_cli, "_guard_named_profile_under_multiplexer", lambda force=False: None)
    monkeypatch.setattr(gateway_cli, "_attach_to_host_gateway_or_guard", lambda **kwargs: None)
    monkeypatch.setattr(gateway_cli, "_guard_supervised_gateway_conflict", lambda force=False: None)
    monkeypatch.setattr(gateway_cli, "_guard_existing_gateway_process_conflict", lambda replace=False: None)
    monkeypatch.setattr(gateway_cli, "_respawn_storm_backoff", lambda: None)
    monkeypatch.setattr(gateway_cli, "supports_systemd_services", lambda: False)
    monkeypatch.setattr(gateway_cli.sys, "stdin", types.SimpleNamespace(isatty=lambda: False))
    monkeypatch.setenv("HERMES_GATEWAY_EXIT_DIAG", "1")

    def _hard_exit(code: int) -> None:
        raise _HardExit(code)

    monkeypatch.setattr(gateway_run, "_exit_after_graceful_shutdown", _hard_exit)
    return gateway_cli, gateway_run


def _run_cli(gateway_cli, **kwargs) -> int:
    with pytest.raises(_HardExit) as exc:
        gateway_cli.run_gateway(**kwargs)
    return exc.value.code


def _assert_refused(reason: str, owner_pid, owner_alive) -> dict:
    rows = _rows()
    last = rows[-1]
    assert last["tag"] == "gateway.start_refused", [r["tag"] for r in rows]
    assert last["reason"] == reason
    assert last["owner_pid"] == owner_pid
    assert last["owner_alive"] is owner_alive
    assert last["level"] == "info"
    assert "gateway.exit_nonzero" not in [r["tag"] for r in rows]
    return last


def test_live_duplicate_logs_start_refused_already_running(monkeypatch, live_pid):
    """A second ``hermes gateway run`` next to a live gateway: the real start_gateway refuses it."""
    gateway_cli, gateway_run = _prepare_cli(monkeypatch)
    from gateway import status

    async def _no_host_decision(replace, force=False):
        return None

    monkeypatch.setattr(gateway_run, "_host_attach_or_none", _no_host_decision)
    monkeypatch.setattr(status, "get_running_pid", lambda *a, **k: live_pid)
    assert _run_cli(gateway_cli) == 1  # exit code unchanged
    _assert_refused("already_running", live_pid, True)


def test_host_owner_refusal_logs_start_refused_host_refused(monkeypatch, live_pid):
    gateway_cli, _ = _prepare_cli(monkeypatch)
    from gateway import host_attach

    owner = types.SimpleNamespace(pid=live_pid, describe=lambda: f"pid {live_pid}")
    monkeypatch.setattr(
        host_attach, "decide",
        lambda home, replace=False: types.SimpleNamespace(
            outcome=host_attach.REFUSE, owner=owner, message="refused"))
    assert _run_cli(gateway_cli) == 1
    _assert_refused("host_refused", live_pid, True)


@pytest.mark.parametrize("lost", ["running_pid", "runtime_lock", "pid_race"])
def test_pid_claim_loss_logs_start_refused_pid_claim(monkeypatch, live_pid, lost):
    """Losing the runtime lock / PID file to a concurrent start is a refusal, not a crash."""
    gateway_cli, gateway_run = _prepare_cli(monkeypatch)
    from gateway import status

    # What get_running_pid() answers on each call: the pre-claim check, then the owner lookup.
    # pid_race: the check misses the winner, which then wins the O_EXCL write.
    answers = {"running_pid": [live_pid], "runtime_lock": [None], "pid_race": [None, live_pid]}[lost]
    seq = iter(answers)
    monkeypatch.setattr(status, "get_running_pid", lambda *a, **k: next(seq, answers[-1]))
    monkeypatch.setattr(status, "acquire_gateway_runtime_lock", lambda: lost != "runtime_lock")
    monkeypatch.setattr(status, "release_gateway_runtime_lock", lambda: None)

    def _write_pid_file():
        raise FileExistsError

    monkeypatch.setattr(status, "write_pid_file", _write_pid_file)

    def _fake_run(coro):
        coro.close()
        reset_start_refusal()
        return gateway_run._start_gateway_claim_pid_file()

    monkeypatch.setattr(gateway_cli.asyncio, "run", _fake_run)
    assert _run_cli(gateway_cli) == 1
    owner = None if lost == "runtime_lock" else live_pid
    row = _assert_refused("pid_claim", owner, None if owner is None else True)
    assert row["detail"] == lost


def test_dead_owner_is_recorded_as_not_alive(monkeypatch):
    """A refusal naming a pid that is gone says so: the detector files that one."""
    from gateway.run_exit_reason import last_start_refusal, note_start_refused

    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    note_start_refused("already_running", owner_pid=proc.pid)
    assert last_start_refusal() == {"reason": "already_running", "owner_pid": proc.pid, "owner_alive": False}


def test_other_failure_still_writes_exit_nonzero(monkeypatch):
    """Anything that is not a refusal or an unplanned signal keeps the bare exit_nonzero tag."""
    gateway_cli, _ = _prepare_cli(monkeypatch)

    def _fake_run(coro):
        coro.close()
        reset_start_refusal()
        return False

    monkeypatch.setattr(gateway_cli.asyncio, "run", _fake_run)
    assert _run_cli(gateway_cli) == 1
    assert _rows()[-1]["tag"] == "gateway.exit_nonzero"


def test_reset_clears_the_refusal(monkeypatch, live_pid):
    from gateway.run_exit_reason import last_start_refusal, note_start_refused

    note_start_refused("already_running", owner_pid=live_pid)
    reset_start_refusal()
    assert last_start_refusal() is None
