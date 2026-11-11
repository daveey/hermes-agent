"""An isolated serve says so in the spawn ledger; an ordinary serve does not.

Desktop's attach-first discovery reads ``spawn-ledger.json`` and attaches to any live loopback
serve. ``hermes serve --isolated`` (the backend another machine's Desktop spawns over SSH) opts out
of the host singleton on the CLI side, so the ledger row must carry that fact as a structured field
for Desktop to honour it too. Real processes against a temp ``HERMES_HOME``; no mocks.
"""

from contextlib import suppress
import json
import os
import socket
import subprocess
import sys
import time

import psutil
import pytest


def _await_ledger_row(ledger, proc, log, *, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        pids = {proc.pid, *(c.pid for c in psutil.Process(proc.pid).children(recursive=True))}
        try:
            rows = json.loads(ledger.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            rows = []
        for row in rows:
            if row.get("purpose") == "serve" and row.get("pid") in pids and row.get("port"):
                return row
        time.sleep(0.1)
    pytest.fail(f"serve never registered (exit={proc.poll()}): {log.read_text(encoding='utf-8', errors='replace')}")


def _stop(proc):
    if proc.poll() is None:
        family = []
        # A live snapshot does not keep the launcher or its children alive.
        with suppress(psutil.NoSuchProcess):
            parent = psutil.Process(proc.pid)
            family = [*parent.children(recursive=True), parent]
        for p in family:
            with suppress(psutil.NoSuchProcess):
                p.terminate()
        _, alive = psutil.wait_procs(family, timeout=5)
        for p in alive:
            with suppress(psutil.NoSuchProcess):
                p.kill()
    proc.wait(timeout=10)


@pytest.mark.parametrize("phase", ["lookup", "terminate", "kill"])
def test_stop_tolerates_exit_between_observation_and_signal(monkeypatch, phase):
    # Reap a real process at the observation/signal boundary, not after a timer.
    proc = subprocess.Popen([sys.executable, "-c", "input()"], stdin=subprocess.PIPE)
    original_process = psutil.Process
    original_signal = getattr(original_process, phase) if phase != "lookup" else None
    raced = []

    def reap():
        proc.kill()
        proc.wait(timeout=10)
        raced.append(proc.pid)

    def signal(process):
        if process.pid == proc.pid:
            reap()
        assert original_signal is not None
        return original_signal(process)

    def lookup(pid):
        if pid == proc.pid:
            # The live-system signal guard also queries psutil; don't recurse.
            monkeypatch.setattr(psutil, "Process", original_process)
            reap()
        return original_process(pid)

    try:
        if phase == "lookup":
            monkeypatch.setattr(psutil, "Process", lookup)
        else:
            monkeypatch.setattr(original_process, phase, signal)
            if phase == "kill":
                # Keep the child alive through the grace phase to exercise escalation.
                monkeypatch.setattr(original_process, "terminate", lambda process: None)
                monkeypatch.setattr(psutil, "wait_procs", lambda family, timeout: ([], family))
        _stop(proc)
        assert raced == [proc.pid]
        assert proc.returncode is not None
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)
        assert proc.stdin is not None
        proc.stdin.close()


def test_stop_terminates_a_live_process():
    proc = subprocess.Popen([sys.executable, "-c", "input()"], stdin=subprocess.PIPE)
    try:
        _stop(proc)
        assert proc.returncode is not None
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)
        assert proc.stdin is not None
        proc.stdin.close()


@pytest.mark.parametrize("isolated", [False, True])
def test_ledger_launch_does_not_release_a_reserved_port(tmp_path, monkeypatch, isolated):
    """A competing listener wins any bind/close reservation before serve starts."""
    popen = subprocess.Popen
    launches = []
    with socket.socket() as competitor:
        def launch(argv, **kwargs):
            if "serve" in argv and ("--isolated" in argv) == isolated:
                port = int(argv[argv.index("--port") + 1])
                # With port 0, the competitor gets its own allocation instead.
                competitor.bind(("127.0.0.1", port))
                competitor.listen()
                launches.append(port)
            return popen(argv, **kwargs)

        monkeypatch.setattr(subprocess, "Popen", launch)
        test_isolated_serve_ledger_row_is_marked_and_ordinary_serve_is_not(tmp_path)
    assert launches == [0]


def test_isolated_serve_ledger_row_is_marked_and_ordinary_serve_is_not(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "HERMES_HOME": str(home), "HERMES_GATEWAY_LOCK_DIR": str(tmp_path / "locks")}
    for key in ("HERMES_DESKTOP", "HERMES_PARENT_PID", "HERMES_PARENT_START_MARKER",
                "HERMES_DASHBOARD_SESSION_TOKEN", "HERMES_SPAWN"):
        env.pop(key, None)
    ledger = home / "spawn-ledger.json"

    # Ordinary first: started second it would attach to the isolated one and exit.
    launches = [("ordinary", []), ("isolated", ["--isolated"])]
    procs, rows = {}, {}
    try:
        for name, extra in launches:
            log = tmp_path / f"{name}.log"
            with log.open("w", encoding="utf-8") as out:
                procs[name] = subprocess.Popen(
                    [sys.executable, "-m", "hermes_cli.main", "serve", *extra,
                     "--host", "127.0.0.1", "--port", "0"],
                    env=env, stdout=out, stderr=subprocess.STDOUT,
                )
            rows[name] = _await_ledger_row(ledger, procs[name], log)
    finally:
        for proc in procs.values():
            _stop(proc)

    assert rows["isolated"].get("isolated") is True
    assert not rows["ordinary"].get("isolated")
