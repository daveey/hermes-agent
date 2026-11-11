"""The opted-in pre-swap stop is ONE transaction with ONE absolute deadline.

These drive the real supervisor helper paths (``_systemctl`` subprocesses, ``launchctl``, real
processes, ``os.kill``) against fake executables on PATH, in real time with the 30 s pre-swap bound
scaled down; only the gateway control-socket RPC is replaced. A stop that blocks, an RPC that is
slow or a stop job that never finishes must all end inside the bound, and nothing may report the
gateways stopped (so the checkout may move) until each one is verified stopped.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from hermes_cli import update_cmd_posix_pause as pause

pytestmark = pytest.mark.platforms("posix")

BOUND = 4.0  # the scaled-down stand-in for the 30 s pre-swap bound
SLACK = 1.5  # scheduling slack on a loaded CI box


FAKE_SYSTEMCTL = textwrap.dedent('''\
    #!{python}
    import json, os, signal, sys, time
    state = {state!r}
    args = sys.argv[1:]
    with open(os.path.join(state, "calls.log"), "a") as log:
        log.write(" ".join(args) + "\\n")
    cfg = json.load(open(os.path.join(state, "cfg.json")))
    verb = next((a for a in args if a in ("show", "stop", "kill", "reset-failed", "start")), None)
    if verb is None:
        sys.exit(2)
    unit = next(a for a in args[args.index(verb) + 1:] if not a.startswith("-"))
    conf = cfg.get(unit, {{}})
    pid = conf.get("pid", 0)

    def sigkill():
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass

    def alive():
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    flag = lambda name: os.path.join(state, unit + "." + name)
    if verb == "show":
        up, req = alive(), os.path.exists(flag("stopreq"))
        print("ActiveState=" + ("deactivating" if up and req else "active" if up else "inactive"))
        print("SubState=" + ("stop-sigterm" if up and req else "running" if up else "dead"))
        print("MainPID=%d" % (pid if up else 0))
        print("Job=" + ("1" if up and req else ""))
    elif verb == "stop" and "--no-block" in args:
        open(flag("stopreq"), "w").close()
        if conf.get("mode") == "term_ok":
            sigkill()
    elif verb == "stop":
        time.sleep(60)  # a blocking stop that systemd is still working on
    elif verb == "kill":
        if conf.get("mode") != "unkillable":
            sigkill()
''')

FAKE_LAUNCHCTL = textwrap.dedent('''\
    #!{python}
    import os, sys, time
    with open(os.path.join({state!r}, "calls.log"), "a") as log:
        log.write("launchctl " + " ".join(sys.argv[1:]) + "\\n")
    if sys.argv[1] == "bootout":
        time.sleep(60)
''')


class Fixture:
    def __init__(self, tmp_path, monkeypatch):
        self.dir = tmp_path / "fake"
        self.dir.mkdir()
        self.procs = []
        self.cfg = {}
        for name, template in (("systemctl", FAKE_SYSTEMCTL), ("launchctl", FAKE_LAUNCHCTL)):
            path = self.dir / name
            path.write_text(template.format(python=sys.executable, state=str(self.dir)))
            path.chmod(0o755)
        monkeypatch.setenv("PATH", f"{self.dir}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setattr(pause, "_PRE_SWAP_DRAIN_WAIT_S", BOUND)
        monkeypatch.setattr(pause, "_drain_budget", lambda: 1885.0)
        monkeypatch.setattr(pause, "_MANAGE_CMDS", {})
        monkeypatch.setenv(pause.STOP_BUSY_ENV, "1")

    def process(self, ignore_term=False) -> int:
        body = "trap '' TERM; while :; do sleep 0.1; done" if ignore_term else "while :; do sleep 0.1; done"
        proc = subprocess.Popen(["bash", "-c", body], start_new_session=True)
        threading.Thread(target=proc.wait, daemon=True).start()  # reap: a zombie still answers kill -0
        self.procs.append(proc)
        return proc.pid

    def unit(self, name, mode, ignore_term=False) -> dict:
        pid = self.process(ignore_term=ignore_term)
        self.cfg[name] = {"pid": pid, "mode": mode}
        (self.dir / "cfg.json").write_text(json.dumps(self.cfg))
        return {"kind": "systemd", "scope": "user", "unit": name, "pid": pid, "home": str(self.dir / name),
                "ct": None}

    def calls(self) -> list[str]:
        log = self.dir / "calls.log"
        return log.read_text().splitlines() if log.exists() else []

    def cleanup(self):
        for proc in self.procs:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)


@pytest.fixture
def fake(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path, monkeypatch)
    from hermes_cli import update_pause_record as record
    monkeypatch.setattr(record, "mark_stop_sent", lambda *a: None)
    monkeypatch.setattr(record, "mark_stop_refused", lambda *a: None)
    from gateway import control_socket
    monkeypatch.setattr(control_socket, "pause_gateway_for_update", lambda home, **kw: None)  # old gateways
    yield fixture
    fixture.cleanup()


def _run(entries):
    started = time.monotonic()
    try:
        pause._stop_gateways({}, entries, deadline=started + BOUND)
        error = None
    except pause.PauseRefused as exc:
        error = exc
    return time.monotonic() - started, error


def test_a_hung_blocking_stop_is_never_issued_and_the_transaction_ends_in_bound(fake):
    """The old path called blocking ``systemctl stop`` (120 s client timeout, then the unit-job wait)."""
    unit = fake.unit("a.service", "term_ok")
    elapsed, error = _run([unit])
    assert error is None
    assert elapsed <= BOUND + SLACK
    assert not any(" stop " in f" {c} " and "--no-block" not in c for c in fake.calls()), fake.calls()


def test_slow_drain_rpcs_spend_the_same_single_deadline(fake, monkeypatch):
    from gateway import control_socket

    def slow(home, **kw):
        time.sleep(1.0)  # health: allow HX013 -- a slow control-socket reply is the behaviour under test
    monkeypatch.setattr(control_socket, "pause_gateway_for_update", slow)
    units = [fake.unit(f"u{i}.service", "term_ok") for i in range(3)]  # three serial 1 s RPCs
    elapsed, error = _run(units)
    assert error is None
    assert elapsed <= BOUND + SLACK


def test_a_stop_job_that_ignores_sigterm_is_killed_through_the_supervisor_inside_the_bound(fake):
    unit = fake.unit("stubborn.service", "stubborn", ignore_term=True)
    elapsed, error = _run([unit])
    assert error is None
    assert elapsed <= BOUND + SLACK
    assert any(" kill " in f" {c} " and "SIGKILL" in c for c in fake.calls()), fake.calls()
    assert not pause._alive(unit["pid"], None)


def test_an_unstoppable_unit_refuses_at_the_deadline_and_the_stopped_ones_are_not_called_done(fake):
    ok = fake.unit("ok.service", "term_ok")
    stuck = fake.unit("stuck.service", "unkillable", ignore_term=True)
    elapsed, error = _run([ok, stuck])
    assert isinstance(error, pause.PauseRefused) and "stuck.service" in str(error)
    assert BOUND - 0.5 <= elapsed <= BOUND + SLACK
    assert pause._alive(stuck["pid"], None)


def test_a_drained_unit_that_exited_is_disarmed_without_a_blocking_stop(fake, monkeypatch):
    from gateway import control_socket
    unit = fake.unit("drained.service", "term_ok")
    monkeypatch.setattr(control_socket, "pause_gateway_for_update",
                        lambda home, **kw: os.kill(unit["pid"], signal.SIGKILL) or {"pausing": True})
    elapsed, error = _run([unit])
    assert error is None and elapsed <= BOUND + SLACK
    stops = [c for c in fake.calls() if " stop " in f" {c} "]
    assert stops and all("--no-block" in c for c in stops), stops  # queued, verified, never awaited


def test_bare_gateway_that_ignores_sigterm_is_killed_and_verified(fake):
    pid = fake.process(ignore_term=True)
    entry = {"pid": pid, "argv": ["x"], "home": str(fake.dir), "ct": None}
    elapsed, error = _run([entry])
    assert error is None and elapsed <= BOUND + SLACK
    assert not pause._alive(pid, None)


def test_bare_gateway_that_cannot_be_killed_refuses_inside_the_bound(fake, monkeypatch):
    pid = fake.process()
    real_kill = os.kill  # signals to the survivor are swallowed: a process stuck in D state
    monkeypatch.setattr(pause.os, "kill", lambda target, sig: None if target == pid else real_kill(target, sig))
    entry = {"pid": pid, "argv": ["x"], "home": str(fake.dir), "ct": None}
    elapsed, error = _run([entry])
    assert isinstance(error, pause.PauseRefused) and str(pid) in str(error)
    assert elapsed <= BOUND + SLACK


def test_a_hung_launchctl_bootout_cannot_outlast_the_bound(fake, monkeypatch):
    pid = fake.process()
    job = {"kind": "launchd", "label": "ai.hermes.gateway", "domain": "gui/501", "plist": "/x", "pid": pid,
           "home": None, "ct": None}
    from hermes_cli import gateway
    monkeypatch.setattr(gateway, "_locate_launchd_gateway_service", lambda label: ("gui/501", pid))
    elapsed, error = _run([job])
    assert isinstance(error, pause.PauseRefused) and "ai.hermes.gateway" in str(error)
    assert elapsed <= BOUND + SLACK
    assert any(c.startswith("launchctl bootout") for c in fake.calls())


def test_the_pause_hands_one_deadline_to_the_stop_that_covers_discovery(monkeypatch):
    """``_pause`` starts the clock before discovery and passes the same absolute deadline down."""
    from hermes_cli import update_pause_record as record
    seen = {}
    clock = [100.0]
    monkeypatch.setattr(pause, "time", type("T", (), {"monotonic": staticmethod(lambda: clock[0]),
                                                        "sleep": staticmethod(lambda s: None)}))
    monkeypatch.setenv(pause.STOP_BUSY_ENV, "1")

    def discover():
        clock[0] += 12.0  # slow discovery spends the same budget
        return []
    monkeypatch.setattr(pause, "_discover_systemd", discover)
    monkeypatch.setattr(pause, "_discover_launchd", list)
    monkeypatch.setattr(pause, "_left_running", lambda units, jobs: (units, jobs, []))
    entry = {"pid": 1, "argv": ["g"], "home": "/h", "ct": None}
    monkeypatch.setattr(pause, "_discover_bare", lambda pids: ([entry], []))
    from gateway import status
    monkeypatch.setattr(status, "get_process_start_time", lambda pid: 1)
    monkeypatch.setattr(record, "identity", lambda pid=None: {"ct": "ct:1"})
    monkeypatch.setattr(record, "record_pause", lambda *a: None)
    monkeypatch.setattr(record, "mark_stop_requested", lambda *a, **k: None)
    monkeypatch.setattr(record, "sync", lambda token: None)
    monkeypatch.setattr(pause, "_stop_gateways", lambda token, entries, deadline=None: seen.update(deadline=deadline))
    token = pause._empty_token()
    assert pause._pause(token) is None
    assert seen["deadline"] == 100.0 + pause._PRE_SWAP_DRAIN_WAIT_S


def test_discovery_that_spent_the_whole_budget_stops_nothing(monkeypatch):
    from hermes_cli import update_pause_record as record
    clock = [0.0]
    monkeypatch.setattr(pause, "time", type("T", (), {"monotonic": staticmethod(lambda: clock[0]),
                                                        "sleep": staticmethod(lambda s: None)}))
    monkeypatch.setenv(pause.STOP_BUSY_ENV, "1")

    def discover():
        clock[0] += 31.0
        return []
    monkeypatch.setattr(pause, "_discover_systemd", discover)
    monkeypatch.setattr(pause, "_discover_launchd", list)
    monkeypatch.setattr(pause, "_left_running", lambda units, jobs: (units, jobs, []))
    monkeypatch.setattr(pause, "_discover_bare", lambda pids: ([{"pid": 1, "argv": ["g"], "home": "/h"}], []))
    monkeypatch.setattr(record, "record_pause", lambda *a: pytest.fail("recorded after the deadline"))
    stop = []
    monkeypatch.setattr(pause, "_stop_gateways", lambda *a, **k: stop.append(1))
    verdict = pause._pause(pause._empty_token())
    assert "deadline" in verdict and "the checkout was not changed" in verdict
    assert not stop
