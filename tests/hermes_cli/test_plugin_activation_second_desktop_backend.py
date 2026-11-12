"""A second Desktop-owned backend with no role record still gets the activation (t_01a1ce929e118cf2).

daveey-mini ran the supervised always-on SSH backend (:9147) and a local Desktop pool child. Both
are Desktop-owned, so both contend for the single ``desktop-serve`` record; the loser published
nothing and hermes-sync logged only ``backend serve:9119 ok`` while :9147 kept old hooks. Here the
loser is a REAL second process that publishes only its per-process entry.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from gateway import host_backends as hb
from gateway import host_rendezvous as hr
from hermes_cli import plugins_activation
from tests.hermes_cli.test_plugin_activation_serve_fanout import _listener

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def second_backend(monkeypatch, tmp_path):
    """Start a real process that publishes an entry for ``port`` and stays alive."""
    monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(tmp_path / "locks"))
    procs, servers = [], []

    def start(token: str = "always-on-token"):
        server, seen = _listener()
        servers.append(server)
        code = ("import sys\nfrom gateway import host_backends as hb\n"
                f"r = hb.publish('desktop-serve', host='127.0.0.1', port={server.server_address[1]}, "
                f"token={token!r})\nprint('ready' if r else 'refused', flush=True)\nsys.stdin.read()\n")
        proc = subprocess.Popen([sys.executable, "-c", code], cwd=REPO, text=True,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                env={**os.environ, "PYTHONPATH": str(REPO)})
        procs.append(proc)
        assert proc.stdout.readline().strip() == "ready"
        return proc, server.server_address[1], seen

    yield start
    for proc in procs:
        proc.stdin.close()
        proc.wait(timeout=30)
    for server in servers:
        server.shutdown()


def test_the_backend_that_lost_the_desktop_serve_record_is_still_activated(second_backend, tmp_path):
    pool, pool_seen = _listener()
    try:
        assert hr.publish_record(hr.ROLE_DESKTOP_SERVE, host="127.0.0.1", port=pool.server_address[1],
                                 token="pool-child-token") is not None
        proc, port, seen = second_backend()

        answer = plugins_activation.notify_serve_backend("probe", tmp_path)

        assert pool_seen == [{"token": "pool-child-token", "name": "probe", "home": str(tmp_path)}]
        assert seen == [{"token": "always-on-token", "name": "probe", "home": str(tmp_path)}]
        assert [(b["role"], b["port"], b["pid"]) for b in answer["backends"]] == [
            ("desktop-serve", pool.server_address[1], os.getpid()), ("desktop-serve", port, proc.pid)]
        assert answer["ok"] is True and "backend_error" not in answer
    finally:
        hr.clear_record(hr.ROLE_DESKTOP_SERVE)
        pool.shutdown()


def test_after_the_record_holder_exits_the_survivor_is_still_found(second_backend, tmp_path):
    # The mini at 23:03: host-desktop-serve.lock, no host-desktop-serve.json, :9147 alive.
    proc, port, seen = second_backend()

    records = plugins_activation.serve_backend_records()

    assert [(r.role, r.port, r.pid) for r in records] == [("desktop-serve", port, proc.pid)]
    plugins_activation.notify_serve_backends("probe", tmp_path)
    assert seen == [{"token": "always-on-token", "name": "probe", "home": str(tmp_path)}]


def test_an_entry_with_a_torn_token_is_skipped(second_backend):
    proc, _port, _seen = second_backend()
    hb.token_path(proc.pid).write_text("torn-restart-token")

    assert plugins_activation.serve_backend_records() == []


def test_the_entry_goes_away_when_its_backend_exits(second_backend):
    proc, _port, _seen = second_backend()
    proc.stdin.close()
    proc.wait(timeout=30)

    assert hb.read_entries() == []
    assert not hb.entry_path(proc.pid).exists()


def test_a_desktop_backend_that_loses_the_role_record_still_publishes_its_entry(monkeypatch, tmp_path):
    """``web_server._publish_host_rendezvous`` for the always-on SSH backend while a pool child
    holds ``desktop-serve``: no role record, but its own entry makes it reachable."""
    from hermes_cli import web_server

    monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setenv("HERMES_DESKTOP", "1")
    monkeypatch.setenv("HERMES_DASHBOARD_SESSION_TOKEN", "always-on-token")
    monkeypatch.setattr(web_server, "_SESSION_TOKEN", "always-on-token")
    monkeypatch.setattr(hr, "claim_host_lock", lambda role: (hr.HostLockOutcome.HELD_BY_OTHER, None))
    monkeypatch.setattr(hr, "register_exit_cleanup", lambda callback: None)
    try:
        web_server._publish_host_rendezvous("127.0.0.1", 9147)

        assert hr.read_record(hr.ROLE_DESKTOP_SERVE) is None
        records = plugins_activation.serve_backend_records()
        assert [(r.role, r.port, r.pid) for r in records] == [(hr.ROLE_DESKTOP_SERVE, 9147, os.getpid())]
        assert hb.token_for(records[0]) == "always-on-token"
    finally:
        hb.clear()
