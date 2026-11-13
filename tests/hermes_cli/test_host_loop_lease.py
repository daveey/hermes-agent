"""host_loop_lease: one flock-held owner per Hermes home for host-wide loops (t_01a1d470ec8f86ca). [daveey]

The multi-process tests run real child processes against one temporary home: each child polls
``held()`` every ``interval`` and prints ``HELD`` / ``STANDBY`` on every change, so the parent sees
exactly when each process owns the lease.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from hermes_cli import host_loop_lease as hll
from tests.waiting import wait_until

ROOT = Path(__file__).resolve().parents[2]
INTERVAL = 0.2

CHILD = r"""
import logging, sys, threading
sys.path.insert(0, sys.argv[1])
logging.basicConfig(level=logging.INFO, stream=sys.stderr)
from hermes_cli.host_loop_lease import HostLoopLease
home, name, role, interval, mode = sys.argv[2], sys.argv[3], sys.argv[4], float(sys.argv[5]), sys.argv[6]
lease = HostLoopLease(name, role, interval=interval, home=home)
tick, state = threading.Event(), None
while True:
    held = lease.held()
    if held != state:
        state = held
        print("HELD" if held else "STANDBY", flush=True)
    if held and mode == "wedge":
        print("WEDGED", flush=True)
        tick.wait()  # a pass that never ends: no beat() again
    if held:
        lease.beat()
    tick.wait(interval)
"""


class Child:
    def __init__(self, home: Path, role: str, *, interval: float = INTERVAL, mode: str = "loop",
                 name: str = "test-loop", env: dict | None = None) -> None:
        self.lines: queue.Queue = queue.Queue()
        self.seen: list[str] = []
        self.proc = subprocess.Popen(
            [sys.executable, "-c", CHILD, str(ROOT), str(home), name, role, str(interval), mode],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True,
            env={**os.environ, **(env or {})})
        self.stderr: list[str] = []
        threading.Thread(target=self._pump, args=(self.proc.stdout, self.lines), daemon=True).start()
        threading.Thread(target=self._drain, daemon=True).start()

    @staticmethod
    def _pump(stream, out: queue.Queue) -> None:
        for line in stream:
            out.put(line.strip())

    def _drain(self) -> None:
        for line in self.proc.stderr:
            self.stderr.append(line)

    def _collect(self) -> None:
        while True:
            try:
                self.seen.append(self.lines.get_nowait())
            except queue.Empty:
                return

    def state(self) -> str | None:
        self._collect()
        states = [s for s in self.seen if s in ("HELD", "STANDBY")]
        return states[-1] if states else None

    def ever(self, what: str) -> bool:
        self._collect()
        return what in self.seen

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGKILL)
        self.proc.wait(timeout=10)


@pytest.fixture
def home(tmp_path):
    root = tmp_path / "home"
    root.mkdir()
    return root


@pytest.fixture
def spawn(home):
    children: list[Child] = []

    def make(role: str, **kw) -> Child:
        child = Child(home, role, **kw)
        children.append(child)
        return child

    yield make
    for child in children:
        child.kill()


def _holders(children):
    return [c for c in children if c.state() == "HELD"]


def test_two_processes_only_one_holds(spawn, home):
    a, b = spawn("serve"), spawn("serve")
    wait_until(lambda: a.state() and b.state(), timeout=20, what="both children polled")
    # several intervals of steady state: exactly one holder throughout
    deadline = time.monotonic() + 5 * INTERVAL
    while time.monotonic() < deadline:
        assert len(_holders([a, b])) == 1, (a.seen, b.seen)
        threading.Event().wait(INTERVAL / 4)  # health: allow HX013 -- sampling a steady state is the behaviour under test
    holder = _holders([a, b])[0]
    meta = json.loads((hll.lease_dir(home) / "test-loop.lock").read_text())
    assert meta["pid"] == holder.proc.pid and meta["held"] is True and meta["role"] == "serve"
    assert meta["start"] is not None and meta["heartbeat_at"] > 0


def test_holder_killed_standby_takes_over_within_an_interval(spawn):
    a = spawn("serve")
    wait_until(lambda: a.state() == "HELD", timeout=20, what="first holder")
    b = spawn("serve")
    wait_until(lambda: b.state() == "STANDBY", timeout=20, what="standby")
    a.kill()
    killed = time.monotonic()
    wait_until(lambda: b.state() == "HELD", timeout=5, what="standby takes over")
    # one poll interval plus scheduling slack for a loaded CI host
    assert time.monotonic() - killed < INTERVAL + 1.5


def test_priority_handoff_third_process_does_not_steal(spawn, home):
    dash = spawn("dashboard")
    wait_until(lambda: dash.state() == "HELD", timeout=20, what="dashboard holds")
    # an aggressive same-priority standby polling far faster than the holder's pass
    thief = spawn("dashboard", interval=0.01)
    wait_until(lambda: thief.state() == "STANDBY", timeout=20, what="thief standby")
    gateway = spawn("gateway")
    wait_until(lambda: gateway.state() == "HELD", timeout=10, what="gateway takes the lease")
    wait_until(lambda: dash.state() == "STANDBY", timeout=5, what="dashboard handed off")
    threading.Event().wait(5 * INTERVAL)  # health: allow HX013 -- no steal over several passes is the behaviour under test
    assert gateway.state() == "HELD"
    assert not thief.ever("HELD"), thief.seen
    assert not (hll.lease_dir(home) / "test-loop.want").exists()  # the winner cleared its want


def test_lower_priority_never_preempts(spawn):
    gateway = spawn("gateway")
    wait_until(lambda: gateway.state() == "HELD", timeout=20, what="gateway holds")
    serve = spawn("serve")
    wait_until(lambda: serve.state() == "STANDBY", timeout=20, what="serve standby")
    threading.Event().wait(5 * INTERVAL)  # health: allow HX013 -- no preemption over several passes is the behaviour under test
    assert gateway.state() == "HELD" and serve.state() == "STANDBY"


def test_wedged_holder_released_by_watchdog(spawn):
    wedged = spawn("serve", mode="wedge")
    wait_until(lambda: wedged.ever("WEDGED"), timeout=20, what="holder wedged")
    wedged_at = time.monotonic()
    standby = spawn("serve")
    wait_until(lambda: standby.state() == "HELD", timeout=10, what="standby takes over a wedged holder")
    assert time.monotonic() - wedged_at < hll.WATCHDOG_FACTOR * INTERVAL + 3
    assert wedged.proc.poll() is None  # released by its own watchdog, not by dying
    wait_until(lambda: any("releasing the lease" in line for line in wedged.stderr), timeout=5,
               what="watchdog warning")


def test_kill_switch_every_process_holds(spawn):
    a = spawn("serve", env={hll.KILL_SWITCH_ENV: "0"})
    b = spawn("serve", env={hll.KILL_SWITCH_ENV: "0"})
    wait_until(lambda: a.state() == "HELD" and b.state() == "HELD", timeout=20, what="both hold")


def test_kill_switch_in_process(monkeypatch, home):
    monkeypatch.setenv(hll.KILL_SWITCH_ENV, "0")
    a = hll.HostLoopLease("ks", "serve", interval=1, home=home)
    b = hll.HostLoopLease("ks", "serve", interval=1, home=home)
    assert a.held() and b.held()
    assert not (hll.lease_dir(home) / "ks.lock").exists()  # no lock touched at all


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_unwritable_dir_fails_open_and_logs_once(home, caplog):
    directory = hll.lease_dir(home)
    directory.mkdir(parents=True)
    directory.chmod(0o500)
    try:
        lease = hll.HostLoopLease("ro", "serve", interval=0.01, home=home)
        with caplog.at_level(logging.WARNING, logger=hll.__name__):
            assert lease.held() is True
            lease._next_open = 0.0  # the retry window has passed: the second call reopens and fails again
            assert lease.held() is True
        warnings = [r for r in caplog.records if "unavailable" in r.getMessage()]
        assert len(warnings) == 1
    finally:
        directory.chmod(0o700)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_read_only_lock_file_still_locks(home):
    directory = hll.lease_dir(home)
    directory.mkdir(parents=True)
    (directory / "rof.lock").write_text("")
    (directory / "rof.lock").chmod(0o444)
    a = hll.HostLoopLease("rof", "serve", interval=1, home=home)
    assert a.held() is True
    probe = hll.HostLoopLease("rof", "serve", interval=1, home=home)
    assert probe.held() is False  # separate open file description: excluded by the kernel lock
    a.release()
    assert probe.held() is True
    probe.release()


def test_key_is_the_root_home_not_the_profile(tmp_path):
    root = tmp_path / "custom-root"
    (root / "profiles" / "coder").mkdir(parents=True)
    assert hll.lease_dir(root / "profiles" / "coder") == hll.lease_dir(root)
    assert hll.lease_dir(root) == Path(os.path.realpath(root)) / "run" / "host-loops"


def test_stale_heartbeat_warns_once_per_holder(home, caplog):
    holder = hll.HostLoopLease("stale", "serve", interval=0.05, home=home)
    assert holder.held()
    holder._stop.set()  # silence the watchdog: the holder stays wedged-but-holding
    meta_path = hll.lease_dir(home) / "stale.lock"
    meta = json.loads(meta_path.read_text())
    meta["heartbeat_at"] -= 60
    os.pwrite(holder._fd, json.dumps(meta).encode(), 0)
    os.ftruncate(holder._fd, len(json.dumps(meta)))
    standby = hll.HostLoopLease("stale", "serve", interval=0.01, home=home)
    # same process: the flock still excludes a second open file description
    with caplog.at_level(logging.WARNING, logger=hll.__name__):
        for _ in range(4):
            assert standby.held() is False
            standby._next_probe = 0.0
    stale = [r for r in caplog.records if "last beat" in r.getMessage()]
    assert len(stale) == 1
    holder._held = False
    holder.release()
    standby.release()


def test_release_lets_a_standby_in_and_records_release(home):
    a = hll.HostLoopLease("rel", "serve", interval=1, home=home)
    b = hll.HostLoopLease("rel", "serve", interval=1, home=home)
    assert a.held() and not b.held()
    a.release()
    meta = json.loads((hll.lease_dir(home) / "rel.lock").read_text())
    assert meta["held"] is False and "released_at" in meta
    assert b.held()
    rows = {m["name"]: m for m in hll.holders(home)}
    assert rows["rel"]["live"] is True and rows["rel"]["pid"] == os.getpid()
    b.release()
    assert hll.holders(home)[0]["live"] is False


def test_release_window_standby_defers_to_live_want(home):
    """The window between the holder's LOCK_UN and the waiter's wake-up: a standby that wins the
    flock there must hand it straight back while a live ``.want`` of at least its priority exists."""
    directory = hll.lease_dir(home)
    directory.mkdir(parents=True)
    waiter = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE)
    try:
        want = {"pid": waiter.pid, "start": hll._start_of(waiter.pid), "priority": 3, "role": "gateway"}
        (directory / "win.want").write_text(json.dumps(want))
        thief = hll.HostLoopLease("win", "dashboard", interval=1, home=home)
        assert thief.held() is False
        other = hll.HostLoopLease("win", "dashboard", interval=1, home=home)
        assert other.held() is False  # the thief did not keep the flock either
        peer = hll.HostLoopLease("win", "gateway", interval=1, home=home)
        assert peer.held() is False  # equal priority: the process that asked first goes first
    finally:
        waiter.communicate(b"")
    assert thief.held() is True  # the wanter is gone: its .want no longer counts
    thief.release()


def test_dead_want_is_ignored(home):
    directory = hll.lease_dir(home)
    directory.mkdir(parents=True)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    (directory / "dw.want").write_text(json.dumps({"pid": dead.pid, "start": 1, "priority": 3, "role": "gateway"}))
    lease = hll.HostLoopLease("dw", "dashboard", interval=1, home=home)
    assert lease.held() is True
    lease.beat()
    assert lease.held() is True
    lease.release()


def test_doctor_lists_holders(home, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(home))
    lease = hll.HostLoopLease("doc-loop", "gateway", interval=30)
    assert lease.held()
    from hermes_cli.doctor_state import _check_host_loop_leases
    _check_host_loop_leases(False)
    out = capsys.readouterr().out
    assert "Host Loop Leases" in out and "doc-loop: held" in out and f"pid {os.getpid()}" in out
    lease.release()
