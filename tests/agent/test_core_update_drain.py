"""The kanban dispatcher spawns nothing while a live hermes-sync core-update drain hold exists."""

import json
import os
import subprocess
import time

import pytest

from agent import core_update_drain as drain


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(drain, "_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(drain, "_canonical_root", lambda: tmp_path)
    return tmp_path


def _write(home, payload):
    path = home / drain.HOLD_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    return path


def _dead_pid():
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


def test_no_hold_allows_dispatch(home):
    from gateway.kanban_watchers_common import _kanban_dispatch_allowed
    assert drain.active_hold() is None
    assert _kanban_dispatch_allowed() is True


def test_live_hold_blocks_dispatch(home):
    from gateway.kanban_watchers_common import _kanban_dispatch_allowed
    _write(home, {"pid": os.getpid(), "started_at": time.time(), "reason": "core update"})
    assert drain.active_hold()["pid"] == os.getpid()
    assert _kanban_dispatch_allowed() is False


def test_dead_writer_hold_is_ignored(home):
    from gateway.kanban_watchers_common import _kanban_dispatch_allowed
    _write(home, {"pid": _dead_pid(), "started_at": time.time()})
    assert drain.active_hold() is None
    assert _kanban_dispatch_allowed() is True


def test_unparseable_hold_counts_only_briefly(home):
    path = _write(home, "{partial")
    assert drain.active_hold() is not None
    old = time.time() - drain._UNPARSEABLE_GRACE_S - 5
    os.utime(path, (old, old))
    assert drain.active_hold() is None


def test_profile_home_honors_fleet_root_hold(tmp_path, monkeypatch):
    profile = tmp_path / "profiles" / "worker"
    profile.mkdir(parents=True)
    monkeypatch.setattr(drain, "_hermes_home", lambda: profile)
    monkeypatch.setattr(drain, "_canonical_root", lambda: tmp_path)
    _write(tmp_path, {"pid": os.getpid()})
    assert drain.active_hold()["path"] == str(tmp_path / drain.HOLD_RELPATH)
