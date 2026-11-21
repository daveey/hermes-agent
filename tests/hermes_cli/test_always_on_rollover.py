"""Warm blue/green handoff for the always-on dashboard (hermes_cli/always_on_rollover.py).

A core update used to restart the one always-on ``hermes serve``: its port refused connections for seconds
and its first requests paid every cold cache (7-30 s, past Desktop's 15 s/30 s timeouts). Now a proxy owns
the port, a roller warms a successor before switching ``active.json``, and the superseded backend retires
only once it is idle.
"""

from __future__ import annotations

import asyncio
import errno
import json
import os
import socket
import subprocess
import time
import types
from pathlib import Path

import pytest

import hermes_cli.always_on_dashboard as ao
import hermes_cli.always_on_rollover as ro
from hermes_cli import web_server_skew_exit as skew


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_HOME", raising=False)
    # pick_port probes real 127.0.0.1 ports: on a host running always-on backends (the hub) it skipped the
    # ports FakeHost expects and the roll tests failed there only.
    monkeypatch.setattr(ro, "_port_listening", lambda port: False)
    return tmp_path


# ── active.json ──────────────────────────────────────────────────────────────────────────────────────


def test_superseded_needs_a_valid_record_naming_another_port(home):
    assert ro.is_superseded(9148) is False  # no record proves nothing
    ro.active_path().parent.mkdir(parents=True, exist_ok=True)
    ro.active_path().write_text("{not json")
    assert ro.is_superseded(9148) is False
    ro.write_active(9148)
    assert ro.is_superseded(9148) is False
    ro.write_active(9149, warming=[9148])
    assert ro.is_superseded(9148) is False  # still warming: a restart cut the roll short
    ro.write_active(9149)
    assert ro.is_superseded(9148) is True and ro.is_superseded(9149) is False


def test_backend_identity_comes_from_the_unit_cgroup_not_the_environment(tmp_path):
    for pid, cgroup in ((10, "0::/user.slice/user@1000.service/app.slice/hermes-always-on-backend@9149.service\n"),
                        (11, "0::/user.slice/user@1000.service/app.slice/hermes-always-on-dashboard.service\n")):
        (tmp_path / str(pid)).mkdir()
        (tmp_path / str(pid) / "cgroup").write_text(cgroup)
    assert ro.backend_port_for_pid(10, proc=tmp_path) == 9149
    assert ro.backend_port_for_pid(11, proc=tmp_path) is None
    assert ro.backend_port_for_pid(12, proc=tmp_path) is None


def test_backend_port_env_is_popped_so_agent_children_never_inherit_it():
    env = {ro.ENV_BACKEND: "9150"}
    assert ro.pop_backend_port(env) == 9150 and ro.ENV_BACKEND not in env
    assert ro.pop_backend_port({}) is None
    assert ro.pop_backend_port({ro.ENV_BACKEND: "x"}) is None


# ── hermes update's dashboard refresh ────────────────────────────────────────────────────────────────


def test_update_requests_one_warm_roll_instead_of_killing_always_on_backends(capsys):
    rolls = []
    kept = ro.route_always_on_backends([1, 2, 3], roll=lambda: rolls.append(1) or True,
                                       port_for_pid={1: 9148, 3: 9149}.get)
    assert kept == [2] and rolls == [1]
    assert "requested a warm roll" in capsys.readouterr().out
    assert ro.route_always_on_backends([2], roll=lambda: rolls.append(1) or True, port_for_pid=lambda p: None) == [2]
    assert rolls == [1]


def test_kill_stale_dashboard_processes_never_kills_an_always_on_backend(monkeypatch):
    from hermes_cli import dashboard_procs as dp
    from hermes_cli import main_dashboard as dash

    monkeypatch.setattr(ro, "_on_linux", lambda: True)
    monkeypatch.setattr(dash, "_restart_managed_dashboard_service", lambda reason: False)
    monkeypatch.setattr(dp, "_lock_owned_serve_pids", set)
    monkeypatch.setattr(dash, "_find_stale_dashboard_pids", lambda **kw: [4242])
    rolls = []
    real_route = ro.route_always_on_backends
    monkeypatch.setattr(ro, "route_always_on_backends", lambda pids: real_route(
        pids, roll=lambda: rolls.append(1) or True, port_for_pid=lambda pid: 9148))
    killed = []
    monkeypatch.setattr(dp, "_kill_pids_posix", lambda pids, k, f: killed.extend(pids))
    result = dp._kill_stale_dashboard_processes(restart_managed=True)
    assert killed == [] and result["killed"] == [] and rolls == [1]


# ── the proxy ────────────────────────────────────────────────────────────────────────────────────────


@pytest.fixture(params=[False, True], ids=["uncontended", "port-takeover"])
def backend_port_contender(request, monkeypatch):
    """Try a real silent listener in the gap after the first (dead) active port is published."""
    real_write_active = ro.write_active
    published = []
    with socket.socket() as contender:
        def publish(port):
            real_write_active(port)
            published.append(port)
            if request.param and len(published) == 1:  # later publishes name live backends
                try:
                    contender.bind(("127.0.0.1", port))
                except OSError as exc:
                    assert exc.errno == errno.EADDRINUSE
                else:
                    contender.listen(1)  # A successful TCP connect must not be mistaken for refusal.

        monkeypatch.setattr(ro, "write_active", publish)
        yield


def test_proxy_holds_a_client_while_no_backend_answers_then_splices_it(home, backend_port_contender):
    async def scenario(dead):
        async def echo(reader, writer):
            writer.write(b"pong:" + await reader.read(4))
            await writer.drain()
            writer.close()

        backend = await asyncio.start_server(echo, "127.0.0.1", 0)
        backend_port = backend.sockets[0].getsockname()[1]
        ro.write_active(dead)  # active names a port nothing listens on (a backend mid-restart)
        proxy = ro.Proxy(hold_s=10)
        front = await asyncio.start_server(proxy.handle, "127.0.0.1", 0)
        front_port = front.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", front_port)
        writer.write(b"ping")
        await writer.drain()
        await asyncio.sleep(0.6)  # health: allow HX013 -- the hold under test: no backend answers meanwhile
        ro.write_active(backend_port)  # the roller switches: the held client goes to the new backend
        data = await asyncio.wait_for(reader.read(100), 5)
        writer.close()
        front.close()
        backend.close()
        return data

    # Bind without listening for the whole hold: connections refuse, and no other listener can take the port.
    with socket.socket() as dead:
        dead.bind(("127.0.0.1", 0))
        assert asyncio.run(scenario(dead.getsockname()[1])) == b"pong:ping"


def test_proxy_closes_a_client_only_after_the_hold(home, backend_port_contender):
    async def scenario(dead_port):
        ro.write_active(dead_port)
        proxy = ro.Proxy(hold_s=0.5)
        handlers = []
        clients = []

        def handle(reader, writer):
            clients.append(writer)
            handlers.append(asyncio.create_task(proxy.handle(reader, writer)))

        front = await asyncio.start_server(handle, "127.0.0.1", 0)
        writer = None
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", front.sockets[0].getsockname()[1])
            started = time.monotonic()
            data = await asyncio.wait_for(reader.read(10), 5)
            return data, time.monotonic() - started
        finally:
            front.close()
            closing = [front.wait_closed()]
            if writer is not None:
                clients.append(writer)
            for client in clients:
                client.close()
                closing.append(client.wait_closed())
            for task in handlers:
                task.cancel()
            await asyncio.wait_for(asyncio.gather(*handlers, *closing, return_exceptions=True), 5)

    # Bind without listening: connections refuse, but nobody (including the frontend) can take the port.
    with socket.socket() as dead:
        dead.bind(("127.0.0.1", 0))
        data, elapsed = asyncio.run(scenario(dead.getsockname()[1]))
    assert data == b"" and elapsed >= 0.4


def test_proxy_delivers_the_response_to_a_client_that_half_closed_after_its_request(home):
    # Review B2 of #1611: _pipe closed the upstream socket right after the client's EOF, so a client that
    # shuts down its write side after sending its request got b"" instead of the response.
    async def scenario():
        async def respond_after_eof(reader, writer):
            request = await reader.read()  # until the client's EOF
            await asyncio.sleep(0.2)  # health: allow HX013 -- the response is produced after the half-close
            writer.write(b"RESPONSE:" + request)
            await writer.drain()
            writer.close()

        backend = await asyncio.start_server(respond_after_eof, "127.0.0.1", 0)
        ro.write_active(backend.sockets[0].getsockname()[1])
        front = await asyncio.start_server(ro.Proxy(hold_s=5).handle, "127.0.0.1", 0)
        reader, writer = await asyncio.open_connection("127.0.0.1", front.sockets[0].getsockname()[1])
        writer.write(b"hello")
        writer.write_eof()
        data = await asyncio.wait_for(reader.read(), 5)
        writer.close()
        front.close()
        backend.close()
        return data

    assert asyncio.run(scenario()) == b"RESPONSE:hello"


def test_proxy_target_follows_every_atomic_replace(home):
    proxy = ro.Proxy()
    ro.write_active(9148)
    assert proxy.target() == 9148
    path = ro.active_path()
    stamp = path.stat().st_mtime_ns
    ro.write_active(9149)
    os.utime(path, ns=(stamp, stamp))  # the replace landed inside the same mtime tick
    assert proxy.target() == 9149


# ── the roller ───────────────────────────────────────────────────────────────────────────────────────


class FakeHost:
    """systemctl + HTTP stand-in: units are a set of running ports; health answers per port."""

    def __init__(self, *, running=(), commits=None, answers=True):
        self.running = set(running)
        self.commits = dict(commits or {})
        self.answers = answers
        self.calls: list[tuple] = []
        self.gets: list[tuple[int, str]] = []

    def systemctl(self, *args, timeout=60):
        self.calls.append(args)
        ports = [int(m.group(1)) for a in args if (m := ro.BACKEND_UNIT_RE.fullmatch(a))]
        port = ports[0] if ports else None
        out = {"is-active": lambda: "active" if port in self.running else "inactive",
               "start": lambda: self._start(port), "stop": lambda: self.running.discard(port),
               "show": lambda: str(1000 + port) if port in self.running and port else "0"}.get(args[0], str)()
        return subprocess.CompletedProcess(args, 0, out or "", "")

    def _start(self, port):
        self.running.add(port)
        self.commits.setdefault(port, "c" * 40)

    def get(self, port, path, timeout):
        self.gets.append((port, path))
        if port not in self.running or not self.answers:
            return None, 0.0, None
        if path == "/api/health":
            return 200, 0.01, {"ok": True, "commit": self.commits.get(port)}
        if path == "/api/ssh/ownership":
            return 200, 0.01, {"runtimeIntact": True}
        return 200, 0.01, {}


def make_roller(home, host, *, head="c" * 40, boot_timeout_s=5.0):
    clock = [0.0]

    def sleep(s):
        clock[0] += s

    roller = ro.Roller(token="t" * 64, checkout=None, marker=home / "no-marker", systemctl=host.systemctl,
                       get=host.get, clock=lambda: clock[0], sleep=sleep, boot_timeout_s=boot_timeout_s)
    roller.head = lambda: head  # type: ignore[method-assign]
    return roller


def test_roll_warms_the_successor_before_switching_and_never_stops_the_old_backend(home):
    host = FakeHost(running={9148}, commits={9148: "a" * 40})
    ro.write_active(9148)
    seen_active = []
    real_get = host.get

    def get(port, path, timeout):
        if port == 9149 and path not in ("/api/health",):
            seen_active.append(ro.read_active()["port"])  # warm traffic happens before the switch
        return real_get(port, path, timeout)

    roller = make_roller(home, host)
    roller.get = get
    assert roller.roll("test") is True
    record = ro.read_active()
    assert record["port"] == 9149 and record["warming"] == [] and record["commit"] == "c" * 40
    assert seen_active and set(seen_active) == {9148}
    warmed = {path for port, path in host.gets if port == 9149}
    assert "/api/plugins/kanban/board?board=default" in warmed
    assert not any(c[0] == "stop" for c in host.calls)  # the old backend retires itself when idle
    assert ro.is_superseded(9148)


def test_roll_warms_every_board_on_disk_not_only_the_replayed_ones(home):
    # Live 18:41Z: right after a switch Desktop's reconnect read the rarely-polled boards (bb, hermes, ...)
    # cold. Board reads share 2 admission slots, so those cold first reads queued the warm board=default
    # read for 18-22 s. Every board the successor lists must be warm before it takes traffic.
    host = FakeHost(running={9148}, commits={9148: "a" * 40})
    real_get = host.get

    def get(port, path, timeout):
        if path == "/api/plugins/kanban/boards" and port in host.running:
            host.gets.append((port, path))
            return 200, 0.01, {"boards": [{"slug": "default"}, {"slug": "bb"}, {"slug": "paint bot"},
                                          {"slug": "old", "archived": 1}, {"slug": ""}, {"nope": 1}],
                                "current": "default"}
        return real_get(port, path, timeout)

    ro.write_active(9148)
    roller = make_roller(home, host)
    roller.get = get
    assert roller.roll("test") is True
    warmed = [path for port, path in host.gets if port == 9149]
    assert "/api/plugins/kanban/board?board=bb" in warmed
    assert "/api/plugins/kanban/board?board=paint%20bot" in warmed
    assert warmed.count("/api/plugins/kanban/board?board=default") == 2  # once per round, not duplicated
    assert "/api/plugins/kanban/board?board=" not in warmed
    assert "/api/plugins/kanban/board?board=old" not in warmed  # archived boards are not polled


def test_roll_that_never_answers_keeps_the_old_backend_active(home):
    host = FakeHost(running={9148}, commits={9148: "a" * 40})
    host_get = host.get
    ro.write_active(9148)
    roller = make_roller(home, host, boot_timeout_s=3)
    roller.get = lambda port, path, timeout: (None, 0.0, None) if port == 9149 else host_get(port, path, timeout)
    assert roller.roll("test") is False
    assert ro.read_active()["port"] == 9148 and ro.read_active()["warming"] == []
    assert ("stop", "--no-block", ro.backend_unit(9149)) in host.calls  # never served a client


def test_roll_adopts_a_backend_a_restart_left_warming_on_head(home):
    host = FakeHost(running={9148, 9149}, commits={9148: "a" * 40, 9149: "c" * 40})
    ro.write_active(9148, warming=[9149])
    roller = make_roller(home, host)
    assert roller.roll("restart mid-roll") is True
    assert ro.read_active()["port"] == 9149
    assert not any(c[0] == "start" for c in host.calls)


def test_first_roll_points_the_proxy_at_the_new_backend_at_once(home):
    host = FakeHost()
    roller = make_roller(home, host)
    assert roller.roll("roller started") is True
    assert ro.read_active()["port"] == 9148


def test_supervisor_rolls_on_a_dead_backend_and_on_confirmed_commit_drift(home):
    host = FakeHost(running={9148}, commits={9148: "c" * 40})
    ro.write_active(9148)
    roller = make_roller(home, host)
    assert roller.drift() is None
    host.commits[9148] = "a" * 40
    assert "checkout is at" in roller.drift()
    (home / "no-marker").write_text("")
    assert roller.drift() is None  # mid-update: the update's own roll request handles it
    (home / "no-marker").unlink()
    host.running.discard(9148)
    assert "not running" in roller.drift()
    rolls = []
    roller.roll = lambda reason: rolls.append(reason) or True  # type: ignore[method-assign]
    roller.supervise(max_polls=1)
    assert rolls and "not running" in rolls[0]


def _scripted_health(host, answers):
    """host.answers follows *answers*, one entry per supervise poll (each poll asks /api/health once)."""
    seq = list(answers)
    real_get = host.get

    def get(port, path, timeout):
        if path == "/api/health" and seq:
            host.answers = seq.pop(0)
        return real_get(port, path, timeout)
    return get


def test_supervisor_rolls_an_alive_backend_only_after_it_stops_answering_for_several_polls(home):
    # Live 2026-11-21: the active backend stayed `active` but its threadpool was starved for ~2 h; every Desktop
    # call through the proxy timed out at 30 s and drift() saw no reason to roll (no health body).
    host = FakeHost(running={9148}, commits={9148: "c" * 40})
    ro.write_active(9148)
    roller = make_roller(home, host)
    rolls = []
    roller.roll = lambda reason: rolls.append(reason) or True  # type: ignore[method-assign]
    miss = [False] * (ro.UNRESPONSIVE_CONFIRMATIONS - 1)
    roller.get = _scripted_health(host, [*miss, True, *miss])
    roller.supervise(max_polls=2 * len(miss) + 1)  # one run: the count lives in supervise()
    assert rolls == []  # short stalls, and one good answer between them resets the count


def test_supervisor_rolls_after_consecutive_unanswered_health_polls_and_demotes_the_old_backend(home):
    host = FakeHost(running={9148}, commits={9148: "c" * 40}, answers=False)
    ro.write_active(9148)
    roller = make_roller(home, host)
    rolls = []

    def roll(reason):
        rolls.append(reason)
        ro.write_active(9149, home=home)
        return True
    roller.roll = roll  # type: ignore[method-assign]
    roller.supervise(max_polls=ro.UNRESPONSIVE_CONFIRMATIONS)
    assert len(rolls) == 1 and "does not answer" in rolls[0]
    assert set(roller._load_demoted()) == {9148}


def test_commit_drift_polls_do_not_complete_an_unresponsive_count(home):
    host = FakeHost(running={9148}, commits={9148: "c" * 40})
    ro.write_active(9148)
    roller = make_roller(home, host)
    rolls = []
    roller.roll = lambda reason: rolls.append(reason) or True  # type: ignore[method-assign]
    reasons = iter(["active backend :9148 runs aaaaaaaaaa, checkout is at cccccccccc"]
                   + [f"active backend :9148 {ro.UNRESPONSIVE_REASON}"] * (ro.UNRESPONSIVE_CONFIRMATIONS - 1))
    roller.drift = lambda: next(reasons)  # type: ignore[method-assign]
    roller.supervise(max_polls=ro.UNRESPONSIVE_CONFIRMATIONS)
    assert rolls == []


def test_pick_port_never_adopts_a_demoted_backend(home):
    host = FakeHost(running={9148, 9149}, commits={9148: "c" * 40, 9149: "c" * 40})
    ro.write_active(9148)
    roller = make_roller(home, host)
    assert roller.pick_port(9148, "c" * 40) == (9149, True)  # control: an answering backend on HEAD is adopted
    roller.demote(9149)
    assert roller.pick_port(9148, "c" * 40) == (9150, False)


def test_reap_stops_a_demoted_backend_after_the_short_hard_limit(home):
    host = FakeHost(running={9148, 9149}, commits={9148: "c" * 40, 9149: "c" * 40})
    ro.write_active(9149)
    wall = [1000.0]
    roller = make_roller(home, host)
    roller.wall = lambda: wall[0]  # type: ignore[method-assign]
    roller._retire_fenced = lambda port: False  # type: ignore[method-assign]  # busy: the fence refuses
    roller.demote(9148)
    assert roller.reap_superseded() == []
    wall[0] += ro.DEMOTED_HARD_S - 1
    assert roller.reap_superseded() == []  # just under the limit: still kept
    wall[0] += 1
    assert roller.reap_superseded() == [9148]
    assert ("stop", "--no-block", ro.backend_unit(9148)) in host.calls
    host.running.discard(9148)
    roller.reap_superseded()
    assert roller._load_demoted() == {}


def test_a_failed_unresponsive_roll_demotes_nothing(home):
    host = FakeHost(running={9148}, commits={9148: "c" * 40}, answers=False)
    ro.write_active(9148)
    roller = make_roller(home, host)
    roller.roll = lambda reason: False  # type: ignore[method-assign]
    roller.supervise(max_polls=ro.UNRESPONSIVE_CONFIRMATIONS)
    assert roller._load_demoted() == {}


def test_a_replacement_on_the_freed_port_is_not_demoted(home):
    # The starved backend exited between drift() and roll(); the roll restarted on that same port.
    host = FakeHost(running={9148}, commits={9148: "c" * 40}, answers=False)
    ro.write_active(9148)
    roller = make_roller(home, host)

    def roll(reason):
        ro.write_active(9148, home=home)
        return True
    roller.roll = roll  # type: ignore[method-assign]
    roller.supervise(max_polls=ro.UNRESPONSIVE_CONFIRMATIONS)
    assert roller._load_demoted() == {}


def test_start_demotes_a_starved_active_backend_it_replaces(home):
    host = FakeHost(running={9148}, commits={9148: "c" * 40})
    ro.write_active(9148)
    real_get = host.get
    roller = make_roller(home, host)
    roller.get = lambda port, path, timeout: (None, 0.0, None) if port == 9148 else real_get(port, path, timeout)
    assert roller.start() is True
    assert ro.read_active()["port"] == 9149 and set(roller._load_demoted()) == {9148}


def test_start_does_not_demote_a_backend_that_answers_one_of_its_probes(home):
    host = FakeHost(running={9148}, commits={9148: "a" * 40})
    ro.write_active(9148)
    real_get = host.get
    misses = [ro.UNRESPONSIVE_CONFIRMATIONS - 1]

    def get(port, path, timeout):
        if port == 9148 and path == "/api/health" and misses[0] > 0:
            misses[0] -= 1
            return None, 0.0, None  # busy for a few probes, then answers
        return real_get(port, path, timeout)
    roller = make_roller(home, host)
    roller.get = get
    assert roller.start() is True
    assert ro.read_active()["port"] == 9149 and roller._load_demoted() == {}


def test_start_probes_only_after_the_update_marker_clears(home):
    host = FakeHost(running={9148}, commits={9148: "c" * 40})
    ro.write_active(9148)
    marker = home / "no-marker"
    marker.write_text("")
    probes = []
    real_get = host.get

    def get(port, path, timeout):
        if port == 9148 and path == "/api/health":
            probes.append(marker.exists())
            return None, 0.0, None
        return real_get(port, path, timeout)
    roller = make_roller(home, host)
    roller.get = get
    real_sleep = roller.sleep
    roller.sleep = lambda s: (marker.unlink(missing_ok=True), real_sleep(s))  # the update finishes while waiting
    assert roller.start() is True
    assert probes and not any(probes)  # never probed while the update held the marker
    assert set(roller._load_demoted()) == {9148}


def test_start_does_not_demote_a_healthy_active_backend(home):
    host = FakeHost(running={9148}, commits={9148: "a" * 40})
    ro.write_active(9148)
    roller = make_roller(home, host)
    assert roller.start() is True
    assert ro.read_active()["port"] == 9149 and roller._load_demoted() == {}


def test_unanswered_health_mid_update_is_not_a_reason_to_roll(home):
    host = FakeHost(running={9148}, commits={9148: "c" * 40}, answers=False)
    ro.write_active(9148)
    roller = make_roller(home, host)
    (home / "no-marker").write_text("")
    assert roller.drift() is None


class FakeFence:
    """``/api/health/retirement`` of each backend: prepare refuses while the port is busy (a live turn)."""

    def __init__(self, busy=()):
        self.busy, self.posts = set(busy), []

    def post(self, port, path, payload):
        self.posts.append((port, payload["action"]))
        if payload["action"] == "prepare":
            return {"ok": False, "reason": "busy"} if port in self.busy else {"ok": True, "token": f"tok{port}"}
        return {"ok": payload.get("token") == f"tok{port}"}


def make_reaper(home, host, fence, wall):
    roller = make_roller(home, host)
    roller.post, roller.wall = fence.post, lambda: wall[0]
    return roller


def test_superseded_backend_is_retired_through_its_fence_never_mid_turn(home):
    # Review B1 of #1611: the backstop used to SIGTERM a superseded backend 900 s in, even mid-turn.
    host, fence, wall = FakeHost(running={9148, 9149}), FakeFence(busy={9148}), [1000.0]
    ro.write_active(9149)
    assert make_reaper(home, host, fence, wall).reap_superseded(grace_s=600, hard_s=7200) == []
    wall[0] += 601
    # A fresh roller (each core update restarts it) still knows when 9148 was superseded.
    assert make_reaper(home, host, fence, wall).reap_superseded(grace_s=600, hard_s=7200) == []
    assert (9148, "prepare") in fence.posts and not any(c[0] == "stop" for c in host.calls)  # busy: kept
    fence.busy.clear()  # the turn ended
    assert make_reaper(home, host, fence, wall).reap_superseded(grace_s=600, hard_s=7200) == [9148]
    assert (9148, "commit") in fence.posts
    assert ("stop", "--no-block", ro.backend_unit(9148)) in host.calls
    assert not any(c[0] == "stop" and ro.backend_unit(9149) in c for c in host.calls)


def test_superseded_backend_busy_past_the_hard_bound_is_stopped(home):
    host, fence, wall = FakeHost(running={9148, 9149}), FakeFence(busy={9148}), [1000.0]
    ro.write_active(9149)
    roller = make_reaper(home, host, fence, wall)
    assert roller.reap_superseded(grace_s=600, hard_s=7200) == []
    wall[0] += 7200
    assert roller.reap_superseded(grace_s=600, hard_s=7200) == [9148]
    assert ("stop", "--no-block", ro.backend_unit(9148)) in host.calls
    host.running.discard(9148)
    roller.reap_superseded(grace_s=600, hard_s=7200)
    assert json.loads((ao.state_dir() / ro.SUPERSEDED_NAME).read_text()) == {}  # forgotten once gone


def test_roll_adopts_a_still_booting_backend_the_previous_roll_left_warming(home):
    # Nit of review 5878814630: restarts close together must not stack cold boots onto the 4-port pool.
    host = FakeHost(running={9148, 9149}, commits={9148: "a" * 40, 9149: "c" * 40})
    ro.write_active(9148, warming=[9149])
    booting = [3]
    real_get = host.get

    def get(port, path, timeout):
        if port == 9149 and booting[0] > 0:
            booting[0] -= 1
            return None, 0.0, None
        return real_get(port, path, timeout)

    roller = make_roller(home, host)
    roller.get = get
    assert roller.roll("restart mid-boot") is True
    assert ro.read_active()["port"] == 9149
    assert not any(c[0] == "start" for c in host.calls)


def test_roll_never_switches_to_a_backend_that_died_while_it_warmed(home):
    host = FakeHost(running={9148}, commits={9148: "a" * 40})
    ro.write_active(9148)
    roller = make_roller(home, host)
    real_warm = roller.warm

    def warm(port, paths):
        summary = real_warm(port, paths)
        host.running.discard(port)  # e.g. OOM-killed during the warm rounds
        return summary

    roller.warm = warm
    assert roller.roll("test") is False
    assert ro.read_active()["port"] == 9148 and ro.read_active()["warming"] == []


def test_supervisor_survives_a_poll_that_raises(home, capsys):
    host = FakeHost(running={9148})
    ro.write_active(9148)
    roller = make_roller(home, host)
    roller.drift = lambda: (_ for _ in ()).throw(ValueError("bad hour_epoch"))  # type: ignore[method-assign]
    roller.supervise(max_polls=3)
    assert capsys.readouterr().err.count("poll failed") == 3


def test_successor_pending_while_another_port_warms_or_after_the_switch(home):
    assert ro.successor_pending(9148) is False  # no record proves nothing
    ro.write_active(9148)
    assert ro.successor_pending(9148) is False
    ro.write_active(9148, warming=[9149])
    assert ro.successor_pending(9148) is True and ro.successor_pending(9149) is False
    ro.write_active(9149)
    assert ro.successor_pending(9148) is True


def test_replay_paths_take_only_allowlisted_reads_the_old_backend_served(home, tmp_path):
    log = tmp_path / "loop-watch.jsonl"
    now = time.time()
    rows = [
        {"kind": "request_stats", "pid": 7, "hour_epoch": now - 60, "paths": {
            "/api/plugins/user-steps/session-cards": {"n": 3, "queries": [["ids=a%2Cb", 3]]},
            "/api/plugins/session-mentions/outbox": {"n": 9, "queries": [["courier=x", 9]]},  # claims items
            "/api/sessions/:id/messages": {"n": 5, "queries": [["limit=1", 5]]}}},  # templated
        {"kind": "request_stats", "pid": 8, "hour_epoch": now - 60, "paths": {
            "/api/session-kanban-tasks": {"n": 1, "queries": [["ids=z", 1]]}}},  # another process
    ]
    log.write_text("".join(json.dumps(r) + "\n" for r in rows))
    roller = make_roller(home, FakeHost())
    roller.loop_watch = log
    paths = roller.replay_paths(7)
    assert "/api/plugins/user-steps/session-cards?ids=a%2Cb" in paths
    assert not any("outbox" in p or "/messages" in p or "kanban-tasks" in p for p in paths)
    assert paths[: len(ro.DEFAULT_WARM)] == list(ro.DEFAULT_WARM)


# ── units ────────────────────────────────────────────────────────────────────────────────────────────


def test_units_keep_the_port_stable_and_the_roll_off_callers_clocks(tmp_path):
    units = ro.render_units(exec_prefix=["/h/hermes", "always-on-dashboard"], hermes="/h/hermes", port=9147,
                            log_path=tmp_path / "l.log", path_env="/usr/bin")
    proxy, backend, roller = units[ro.PROXY_UNIT], units[ro.BACKEND_TEMPLATE], units[ao.SYSTEMD_UNIT]
    assert "proxy --port 9147" in proxy and "Restart=always" in proxy
    assert "backend --port %i" in backend and "Restart=no" in backend and "[Install]" not in backend
    # simple: `systemctl restart` (hermes update 15 s, hermes-sync 120 s) returns before the warm roll.
    assert "Type=simple" in roller and "run --port 9147" in roller and f"Wants={ro.PROXY_UNIT}" in roller


def test_linux_run_is_the_roller(monkeypatch):
    import argparse

    called = []
    monkeypatch.setattr(ao, "_uses_rollover", lambda: True)
    monkeypatch.setattr(ro, "cmd_roll", lambda args: called.append(args.port) or 0)
    assert ao.cmd_run(argparse.Namespace(port=9147, hermes="/h/hermes")) == 0 and called == [9147]


def test_backend_unit_execs_the_isolated_serve_with_its_port(home, monkeypatch):
    import argparse

    captured = {}
    monkeypatch.setattr(ro.os, "execve", lambda path, argv, env: captured.update(argv=argv, env=env))
    ro.cmd_backend(argparse.Namespace(port=9149, hermes="/h/hermes"))
    assert captured["argv"][:7] == ["/h/hermes", "serve", "--isolated", "--host", "127.0.0.1", "--port", "9149"]
    assert captured["env"][ro.ENV_BACKEND] == "9149" and captured["env"][ao.ENV_FLAG] == "1"
    record = json.loads(ro.custody_path(9149).read_text())  # written before the exec, same pid
    assert record["pid"] == os.getpid() and record["port"] == 9149 and record["unit"] == ro.backend_unit(9149)
    assert record["ownershipId"] == ao.OWNERSHIP_ID and record["creationTime"].startswith("linux:")
    assert oct(ro.custody_path(9149).stat().st_mode & 0o777) == "0o600"


def _fake_proc(root, pid, *, port, nonce, starttime=4242):
    d = root / str(pid)
    d.mkdir(parents=True)
    (d / "cgroup").write_text(f"0::/user.slice/app.slice/hermes-always-on-backend@{port}.service\n")
    fields = ["S"] + ["0"] * 18 + [str(starttime)] + ["0"] * 10
    (d / "stat").write_text(f"{pid} (hermes serve) " + " ".join(fields) + "\n")
    (d / "cmdline").write_bytes(b"\0".join([b"/h/hermes", b"serve", b"--ssh-owner-nonce", nonce.encode(), b""]))
    return root


def test_backend_custody_record_proves_the_live_backend_and_nothing_else(home, tmp_path):
    # t_01a1cbfae450bef1: an always-on backend has no Desktop backend.lock.json (a systemd unit, and several
    # backends share the always-on slot during a roll); its per-port custody record is the proof instead.
    proc, nonce = _fake_proc(tmp_path / "proc", 777, port=9148, nonce="ab" * 8), "ab" * 8
    ao._private_dir(ao.state_dir())
    (ao.state_dir() / "nonce").write_text(nonce)
    assert ro.verify_backend_custody(777, proc=proc) is None  # no record yet
    ro.write_backend_custody(port=9148, nonce=nonce, hermes="/h/hermes", pid=777)
    record = json.loads(ro.custody_path(9148).read_text())
    record["creationTime"] = "linux:4242"  # the fake process's start time (written from the real /proc)
    ro.custody_path(9148).write_text(json.dumps(record))
    assert ro.verify_backend_custody(777, proc=proc)["port"] == 9148
    # a recycled pid (other start time), another unit's port, another nonce: no custody
    (proc / "777" / "stat").write_text("777 (x) " + " ".join(["S"] + ["0"] * 18 + ["9999"] + ["0"] * 10))
    assert ro.verify_backend_custody(777, proc=proc) is None
    _fake_proc(proc, 778, port=9149, nonce=nonce)
    assert ro.verify_backend_custody(778, proc=proc) is None  # 9149's record names no such pid
    assert ro.verify_backend_custody(777, proc=proc, nonce="cd" * 8) is None


# ── the superseded watchdog (in the backend) ─────────────────────────────────────────────────────────


class _Server:
    should_exit = False


# No keep-alive drain wait: these tests cover the fence and poll cadence (test_always_on_drain covers the drain).
_DRAINED = {"drain_min_s": 0.0, "http_connections": lambda: 0}


@pytest.fixture(autouse=True)
def _not_draining():
    yield
    skew._DRAINING.clear()


class _Fence:
    def __init__(self, idle=True):
        self.idle, self.committed = idle, False

    def prepare(self):
        return {"ok": True, "token": "t"} if self.idle else {"ok": False}

    def commit(self, token):
        self.committed = True
        return {"ok": True}


def test_superseded_backend_retires_through_the_idle_fence():
    server, fence = _Server(), _Fence()
    thread = skew.start_superseded_watchdog(server, port=9148, superseded=lambda p: True, fence=fence,
                                            poll_s=0.01, max_polls=5, **_DRAINED)
    assert thread in skew._SKEW_WATCHDOGS
    thread.join(5)
    assert server.should_exit is True and fence.committed is True


def test_active_or_busy_backend_stays_up():
    server = _Server()
    skew.start_superseded_watchdog(server, port=9148, superseded=lambda p: False, fence=_Fence(),
                                   poll_s=0.01, max_polls=4).join(5)
    assert server.should_exit is False
    server = _Server()
    skew.start_superseded_watchdog(server, port=9148, superseded=lambda p: True, fence=_Fence(idle=False),
                                   poll_s=0.01, max_polls=4, **_DRAINED).join(5)
    assert server.should_exit is False  # a live turn holds it


def test_superseded_watchdog_polls_fast_and_reports_its_successor(monkeypatch):
    # Review B1 of #1611: a paused turn must learn that the successor is still warming (so it waits past
    # HANDOFF_WAIT_S), and the old backend must retire within seconds of the switch, not 30-60 s.
    assert skew.SUPERSEDED_POLL_S <= 2.0
    sleeps = []
    monkeypatch.setattr(skew, "time", types.SimpleNamespace(sleep=sleeps.append))
    monkeypatch.setattr(skew, "_SUCCESSOR_PROBES", [])
    state = {"pending": True}
    busy = skew.start_superseded_watchdog(_Server(), port=9148, superseded=lambda p: True,
                                          pending=lambda p: state["pending"], fence=_Fence(idle=False),
                                          max_polls=4, **_DRAINED)
    busy.join(5)
    # a busy superseded backend re-tries the fence at the slower busy cadence, not every 2 s
    assert sleeps == [skew.SUPERSEDED_POLL_S, skew.SUPERSEDED_BUSY_POLL_S, skew.SUPERSEDED_BUSY_POLL_S,
                      skew.SUPERSEDED_BUSY_POLL_S]
    assert skew.successor_pending() is False  # the thread ended: its probe no longer counts
    skew._SUCCESSOR_PROBES.append((_alive(), lambda: state["pending"]))  # type: ignore[arg-type]
    assert skew.successor_pending() is True
    state["pending"] = False
    assert skew.successor_pending() is False


class _alive:
    def is_alive(self):
        return True


def test_always_on_backend_arms_the_superseded_watchdog_not_code_skew(monkeypatch):
    import hermes_cli.process_identity as pi
    import hermes_cli.web_server as ws

    armed = []
    monkeypatch.setattr(skew, "start_code_skew_watchdog", lambda server: armed.append("skew"))
    monkeypatch.setattr(skew, "start_superseded_watchdog", lambda server, port: armed.append(port))
    monkeypatch.setattr(pi, "reap_orphaned_mcp_helpers", lambda: None)
    monkeypatch.setattr(ws, "_start_parent_death_watchdog", lambda: None)
    monkeypatch.setattr(ws, "_arm_ssh_isolated_idle_exit", lambda server: False)
    monkeypatch.setattr(ws.app.state, "ssh_isolated_clients", object(), raising=False)
    monkeypatch.setattr(ws, "_read_bound_port", lambda server, fallback: (_ for _ in ()).throw(_Stop()))
    monkeypatch.setenv(ro.ENV_BACKEND, "9150")
    with pytest.raises(_Stop):
        ws._on_server_started(_Server(), host="127.0.0.1", port=9150, headless=True, isolated=True,
                              open_browser=False, initial_profile="default", start_mcp_discovery_after_bind=False)
    assert armed == [9150]
    monkeypatch.delenv(ro.ENV_BACKEND, raising=False)
    with pytest.raises(_Stop):
        ws._on_server_started(_Server(), host="127.0.0.1", port=9150, headless=True, isolated=True,
                              open_browser=False, initial_profile="default", start_mcp_discovery_after_bind=False)
    assert armed == [9150, "skew"]


class _Stop(Exception):
    pass
