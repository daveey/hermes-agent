"""Who dispatches this host: presence probe, ``kanban stats`` and the cap source name the lock holder. [daveey]

hermes-hub 2026-11-02: a profile CLI with ``dispatch_in_gateway=false`` warned "the task will sit in
'ready'" while the root gateway dispatched fine, and the at-capacity line never said that the cap
(8) came from the profile gateway's memory-derived default rather than the root's configured 16.
"""
if not __name__.startswith("tests."): raise ImportError("Use scripts/run_tests.sh; unittest discovery bypasses test isolation")  # unittest-discovery guard

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.kanban_watchers_common import _acquire_singleton_lock, _release_singleton_lock


@pytest.fixture
def root(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "kanban").mkdir(parents=True)
    profile = home / "profiles" / "askeve-reviewer"
    profile.mkdir(parents=True)
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return home


def _no_gateway(monkeypatch):
    from gateway import status
    monkeypatch.setattr(status, "resolve_gateway_liveness",
                        lambda **kw: SimpleNamespace(pid=None, probe_error=None))


def test_presence_reports_host_dispatcher_held_by_another_home(root, monkeypatch):
    from gateway import kanban_singleton as ks
    from hermes_cli import kanban as cli
    monkeypatch.setenv("HERMES_HOME", str(root))
    lease = ks.SingletonLease(root / "kanban" / ".dispatcher.lock", name="dispatcher", root=True)
    assert lease.try_acquire()
    try:
        # The CLI runs as the profile: its own gateway is down and its config disables dispatch.
        monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / "askeve-reviewer"))
        monkeypatch.setattr(cli, "_kanban_config", lambda: {"dispatch_in_gateway": False})
        _no_gateway(monkeypatch)
        running, message = cli._check_dispatcher_presence()
        assert running, message
        assert "root gateway" in message and f"HERMES_HOME={root}" in message
    finally:
        lease.release()


def test_presence_still_warns_when_no_one_holds_the_host_lock(root, monkeypatch):
    from hermes_cli import kanban as cli
    monkeypatch.setenv("HERMES_HOME", str(root))
    _no_gateway(monkeypatch)
    running, message = cli._check_dispatcher_presence()
    assert not running and "No gateway is running" in message


def test_stats_names_host_lock_holders(root, monkeypatch, capsys):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban as cli
    from gateway import kanban_singleton as ks
    monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / "askeve-reviewer"))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(root / "kanban.db"))
    kb.init_db()
    lease = ks.SingletonLease(root / "kanban" / ".sync.lock", name="sync", root=False, grace_seconds=0.0)
    assert lease.try_acquire()
    try:
        assert cli._cmd_stats(argparse.Namespace(json=True)) == 0
        locks = json.loads(capsys.readouterr().out)["host_locks"]
        assert locks["dispatcher"] is None
        assert locks["sync"]["hermes_home"] == str(root / "profiles" / "askeve-reviewer")
        assert locks["sync"]["root"] is False
        assert cli._cmd_stats(argparse.Namespace(json=False)) == 0
        out = capsys.readouterr().out
        assert "Host locks:" in out and "not the root gateway" in out
    finally:
        lease.release()


@pytest.mark.parametrize("configured", [None, 16])
def test_at_capacity_names_where_the_cap_came_from(root, monkeypatch, configured):
    from hermes_cli import kanban_db_dispatch as kbd
    home = root / "profiles" / "askeve-reviewer"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(kbd, "configured_max_in_progress", lambda: configured)
    res = kbd.DispatchResult()
    res.at_capacity = "max_in_progress=8/8"
    line = kbd.describe_suppression([res])
    assert "at_capacity(max_in_progress=8/8)" in line
    want = "kanban.max_in_progress in" if configured else "memory-derived default"
    assert f"cap_source={want}" in line and f"{home}/config.yaml" in line
    assert "cap_source=" in kbd.stuck_warning(6, [res])


def test_lock_holder_unaffected_by_unheld_sidecar(root, monkeypatch):
    """A stale holder sidecar from a dead gateway is not reported as a holder on Linux."""
    from gateway import kanban_singleton as ks
    if not Path("/proc/locks").exists():
        pytest.skip("needs /proc/locks")
    path = root / "kanban" / ".dispatcher.lock"
    handle, state = _acquire_singleton_lock(path)
    assert state == "held"
    _release_singleton_lock(handle)
    ks._write_json(Path(f"{path}.holder.json"), {"pid": 1, "hermes_home": "/x"})
    assert ks.holder_info(path) is None
