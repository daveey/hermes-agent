"""Warm blue/green handoff for the always-on dashboard (hermes_cli/always_on_rollover.py).

A core update used to restart the one always-on ``hermes serve``: its port refused connections for seconds
and its first requests paid every cold cache (7-30 s, past Desktop's 15 s/30 s timeouts). Now a proxy owns
the port, a roller warms a successor before switching ``active.json``, and the superseded backend retires
only once it is idle.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from pathlib import Path

import pytest

import hermes_cli.always_on_dashboard as ao
import hermes_cli.always_on_rollover as ro
from hermes_cli import web_server_skew_exit as skew


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_HOME", raising=False)
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

    monkeypatch.setattr(dp.sys, "platform", "linux")
    monkeypatch.setattr(dash, "_restart_managed_dashboard_service", lambda reason: False)
    monkeypatch.setattr(dp, "_lock_owned_serve_pids", set)
    monkeypatch.setattr(dash, "_find_stale_dashboard_pids", lambda **kw: [4242])
    rolls = []
    monkeypatch.setattr(ro.route_always_on_backends, "__kwdefaults__",
                        {"roll": lambda: rolls.append(1) or True, "port_for_pid": lambda pid: 9148})
    killed = []
    monkeypatch.setattr(dp, "_kill_pids_posix", lambda pids, k, f: killed.extend(pids))
    result = dp._kill_stale_dashboard_processes(restart_managed=True)
    assert killed == [] and result["killed"] == [] and rolls == [1]


# ── the proxy ────────────────────────────────────────────────────────────────────────────────────────


def test_proxy_holds_a_client_while_no_backend_answers_then_splices_it(home):
    async def scenario():
        async def echo(reader, writer):
            writer.write(b"pong:" + await reader.read(4))
            await writer.drain()
            writer.close()

        backend = await asyncio.start_server(echo, "127.0.0.1", 0)
        backend_port = backend.sockets[0].getsockname()[1]
        dead = socket_free_port()
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

    assert asyncio.run(scenario()) == b"pong:ping"


def test_proxy_closes_a_client_only_after_the_hold(home):
    async def scenario():
        ro.write_active(socket_free_port())
        proxy = ro.Proxy(hold_s=0.5)
        front = await asyncio.start_server(proxy.handle, "127.0.0.1", 0)
        reader, _writer = await asyncio.open_connection("127.0.0.1", front.sockets[0].getsockname()[1])
        started = time.monotonic()
        data = await asyncio.wait_for(reader.read(10), 5)
        front.close()
        return data, time.monotonic() - started

    data, elapsed = asyncio.run(scenario())
    assert data == b"" and elapsed >= 0.4


def socket_free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


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


def test_superseded_backend_that_never_retires_is_stopped_after_the_grace(home):
    host = FakeHost(running={9148, 9149})
    ro.write_active(9149)
    roller = make_roller(home, host)
    assert roller.reap_superseded(grace_s=600) == []  # first sighting starts its clock
    roller.sleep(599)
    assert roller.reap_superseded(grace_s=600) == []
    roller.sleep(2)
    assert roller.reap_superseded(grace_s=600) == [9148]
    assert ("stop", "--no-block", ro.backend_unit(9148)) in host.calls
    assert not any(c[0] == "stop" and ro.backend_unit(9149) in c for c in host.calls)


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


# ── the superseded watchdog (in the backend) ─────────────────────────────────────────────────────────


class _Server:
    should_exit = False


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
                                            poll_s=0.01, max_polls=5)
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
                                   poll_s=0.01, max_polls=4).join(5)
    assert server.should_exit is False  # a live turn holds it


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
