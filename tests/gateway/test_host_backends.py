"""Per-process serve backend entries (``gateway.host_backends``, t_01a1ce929e118cf2).

The role record holds one owner per role. On daveey-mini the supervised always-on SSH backend
(:9147) lost ``desktop-serve`` to a local Desktop pool child, published nothing, and hermes-sync's
activations never reached its chats. Each backend now also publishes its own entry; these tests
use a real rendezvous dir, real processes and real liveness proofs.
"""
from __future__ import annotations

import dataclasses
import json
import os
import stat
import subprocess
import sys

import pytest

from gateway import host_backends as hb
from gateway import host_rendezvous as hr


@pytest.fixture
def lock_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setattr(hr, "register_exit_cleanup", lambda callback: None)
    yield tmp_path / "locks"
    hb.clear()


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_publish_writes_a_private_entry_and_token_that_read_back(lock_dir):
    record = hb.publish(hr.ROLE_DESKTOP_SERVE, host="127.0.0.1", port=9147, token="always-on-token")

    assert record is not None and record.pid == os.getpid()
    assert hb.read_entries() == [record]
    assert hb.read_token(record) == "always-on-token"
    assert hb.token_is_consistent(record) and hb.token_for(record) == "always-on-token"
    if sys.platform != "win32":
        assert stat.S_IMODE(hb.backends_dir().stat().st_mode) == 0o700
        assert stat.S_IMODE(hb.token_path(record.pid).stat().st_mode) == 0o600
        assert stat.S_IMODE(hb.entry_path(record.pid).stat().st_mode) == 0o600


@pytest.mark.parametrize("kwargs", [
    {"role": hr.ROLE_DESKTOP_SERVE, "port": 9147, "token": ""},
    {"role": hr.ROLE_DESKTOP_SERVE, "port": None, "token": "t"},
    {"role": hr.ROLE_GATEWAY, "port": 9147, "token": "t"},
])
def test_an_undialable_backend_publishes_nothing(lock_dir, kwargs):
    role = kwargs.pop("role")
    assert hb.publish(role, host="127.0.0.1", **kwargs) is None
    assert hb.read_entries() == []


def test_a_dead_entry_is_pruned_with_its_token(lock_dir):
    record = hb.publish(hr.ROLE_DESKTOP_SERVE, host="127.0.0.1", port=9147, token="t")
    dead = _dead_pid()
    payload = {**record.to_json(), "pid": dead}
    hb.entry_path(dead).write_text(json.dumps(payload))
    hb.token_path(dead).write_text("old-token")

    assert [r.pid for r in hb.read_entries()] == [os.getpid()]
    assert not hb.entry_path(dead).exists() and not hb.token_path(dead).exists()


def test_an_entry_whose_name_is_not_its_pid_is_ignored(lock_dir):
    record = hb.publish(hr.ROLE_DESKTOP_SERVE, host="127.0.0.1", port=9147, token="t")
    hb.entry_path(record.pid).rename(hb.backends_dir() / "1.json")

    assert hb.read_entries() == []


def test_clear_removes_only_this_process_entry(lock_dir):
    hb.publish(hr.ROLE_SERVE, host="127.0.0.1", port=9119, token="t")
    other = hb.backends_dir() / "999999.json"
    other.write_text("{}")

    hb.clear()

    assert not hb.entry_path(os.getpid()).exists() and other.exists()


def test_token_for_never_hands_out_another_process_role_token(lock_dir):
    hr.publish_record(hr.ROLE_DESKTOP_SERVE, host="127.0.0.1", port=58497, token="pool-child-token")
    entry = hb.publish(hr.ROLE_DESKTOP_SERVE, host="127.0.0.1", port=9147, token="always-on-token")
    try:
        # Same role, other pid, torn entry token: the pool child's role token must not be used.
        foreign = dataclasses.replace(entry, pid=entry.pid + 1)
        assert hb.token_for(foreign) == ""
        assert hb.token_for(entry) == "always-on-token"
        # A record whose entry token does not match it falls back to its own role token only.
        assert hb.token_for(hr.read_record(hr.ROLE_DESKTOP_SERVE)) == "pool-child-token"
    finally:
        hr.clear_record(hr.ROLE_DESKTOP_SERVE)
