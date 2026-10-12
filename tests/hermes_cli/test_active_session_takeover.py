"""Explicit cross-process takeover of a chat's active-session lease.

A chat whose lease another LIVE process holds is refused (SESSION_NOT_OWNED); the user can still
move it here on purpose ("Take over here"). The takeover replaces the foreign owner's registry entry
with one for this process that names the displaced lease (``took_over_from``), so the old owner can
find out, stand down, and acknowledge.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import active_sessions
from hermes_cli.active_sessions import (
    SESSION_COORDINATION_UNAVAILABLE,
    SESSION_NOT_OWNED,
    acknowledge_takeover,
    active_session_registry_snapshot,
    find_takeover_of,
    take_over_active_session,
    takeover_acknowledged,
    try_acquire_active_session,
)


@pytest.fixture(autouse=True)
def _isolated_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))


def _registry_path() -> Path:
    return Path(os.environ["HERMES_HOME"]) / "runtime" / "active_sessions.json"


def _entries() -> list[dict]:
    return json.loads(_registry_path().read_text())["entries"]


@pytest.fixture
def foreign_owner():
    """A real live process (``sleep``) recorded as the owner of session ``S``."""
    proc = subprocess.Popen(["sleep", "60"])
    try:
        start = active_sessions._process_start_time(proc.pid)
        entry = {
            "lease_id": "foreign-lease", "session_id": "S", "surface": "desktop", "pid": proc.pid,
            "process_start_time": start, "started_at": 1000.0, "updated_at": 1000.0,
            "track_liveness": True, "metadata": {"live_session_id": "other-window"},
        }
        _registry_path().parent.mkdir(parents=True, exist_ok=True)
        _registry_path().write_text(json.dumps({"entries": [entry]}))
        yield proc, entry
    finally:
        proc.kill()
        proc.wait()


def test_foreign_live_owner_refuses_a_plain_acquire(foreign_owner):
    lease, refusal = try_acquire_active_session(
        session_id="S", surface="desktop", config={}, metadata={"live_session_id": "here"},
        track_liveness=True)
    assert lease is None and refusal.reason == SESSION_NOT_OWNED


def test_takeover_replaces_a_foreign_live_owner(foreign_owner):
    proc, _old = foreign_owner
    lease, removed, refusal = take_over_active_session(
        session_id="S", surface="desktop", metadata={"live_session_id": "here"}, track_liveness=True)

    assert refusal is None and lease is not None
    assert [e["lease_id"] for e in removed] == ["foreign-lease"]
    entries = _entries()
    assert len(entries) == 1
    (mine,) = entries
    assert mine["lease_id"] == lease.lease_id
    assert mine["pid"] == os.getpid() and mine["session_id"] == "S"
    assert mine["metadata"]["took_over_from"] == ["foreign-lease"]
    assert isinstance(mine["metadata"]["took_over_at"], float)
    assert mine["metadata"]["live_session_id"] == "here"
    # The old owner is still alive and has not acked yet.
    assert proc.poll() is None
    assert takeover_acknowledged(lease, removed) is False
    # And a plain acquire by the displaced writer now hits the new owner.
    entry_lease, refused = try_acquire_active_session(
        session_id="S", surface="desktop", config={}, metadata={"live_session_id": "other-window"})
    assert entry_lease is None and refused.reason == SESSION_NOT_OWNED


def test_takeover_ignores_the_capacity_cap_when_it_displaces(foreign_owner):
    lease, removed, refusal = take_over_active_session(
        session_id="S", surface="desktop", metadata={"live_session_id": "here"}, track_liveness=True,
        config={"max_concurrent_sessions": 1})
    assert refusal is None and lease is not None and len(removed) == 1


def test_takeover_with_nothing_to_displace_is_a_normal_acquire_including_the_cap():
    _first, refused = try_acquire_active_session(
        session_id="A", surface="tui", config={}, metadata={"live_session_id": "a"})
    assert refused is None
    lease, removed, refusal = take_over_active_session(
        session_id="B", surface="tui", metadata={"live_session_id": "b"}, config={"max_concurrent_sessions": 1})
    assert lease is None and removed == [] and refusal.reason == "MAX_CONCURRENT_SESSIONS"
    lease, removed, refusal = take_over_active_session(
        session_id="B", surface="tui", metadata={"live_session_id": "b"}, config={})
    assert refusal is None and removed == []
    assert "took_over_from" not in (_entries()[-1].get("metadata") or {})


def test_same_writer_takeover_is_reentrant():
    _first, refused = try_acquire_active_session(
        session_id="S", surface="tui", config={}, metadata={"live_session_id": "me"})
    assert refused is None
    lease, removed, refusal = take_over_active_session(
        session_id="S", surface="tui", metadata={"live_session_id": "me"})
    assert refusal is None and lease is not None and removed == []
    entries = _entries()
    assert [e["lease_id"] for e in entries] == [lease.lease_id]
    assert "took_over_from" not in entries[0]["metadata"]


def test_unreadable_registry_fails_closed():
    _registry_path().parent.mkdir(parents=True, exist_ok=True)
    _registry_path().write_text("{not json")
    for tracked in (False, True):
        lease, removed, refusal = take_over_active_session(
            session_id="S", surface="desktop", metadata={"live_session_id": "here"}, track_liveness=tracked)
        assert lease is None and removed == []
        assert refusal.reason == SESSION_COORDINATION_UNAVAILABLE
    assert _registry_path().read_text() == "{not json"  # never overwritten


def test_find_acknowledge_and_acknowledged(foreign_owner):
    lease, removed, _ = take_over_active_session(
        session_id="S", surface="desktop", metadata={"live_session_id": "here"}, track_liveness=True)
    found = find_takeover_of("foreign-lease")
    assert found is not None and found["lease_id"] == lease.lease_id
    assert find_takeover_of(lease.lease_id) is None
    assert find_takeover_of("foreign-lease", state_path=_registry_path())["lease_id"] == lease.lease_id

    assert takeover_acknowledged(lease, removed) is False
    assert acknowledge_takeover("foreign-lease") is True
    assert acknowledge_takeover("foreign-lease") is True  # idempotent
    assert _entries()[0]["metadata"]["handoff_acked"] == ["foreign-lease"]
    assert takeover_acknowledged(lease, removed) is True
    assert acknowledge_takeover("never-displaced") is False


def test_dead_old_owner_counts_as_acknowledged(foreign_owner):
    proc, _ = foreign_owner
    lease, removed, _ = take_over_active_session(
        session_id="S", surface="desktop", metadata={"live_session_id": "here"}, track_liveness=True)
    assert takeover_acknowledged(lease, removed) is False
    proc.kill()
    proc.wait()
    assert takeover_acknowledged(lease, removed) is True


def test_find_takeover_of_tolerates_a_torn_registry():
    _registry_path().parent.mkdir(parents=True, exist_ok=True)
    _registry_path().write_text("{torn")
    assert find_takeover_of("anything") is None


def test_registry_snapshot_shows_only_the_new_owner(foreign_owner):
    lease, _, _ = take_over_active_session(
        session_id="S", surface="desktop", metadata={"live_session_id": "here"}, track_liveness=True)
    assert [e["lease_id"] for e in active_session_registry_snapshot()] == [lease.lease_id]


def test_takeover_from_another_python_process_is_visible_to_it(tmp_path):
    """The displaced owner (a real second process) finds the takeover through find_takeover_of."""
    home = Path(os.environ["HERMES_HOME"])
    holder = subprocess.Popen(
        [sys.executable, "-c", (
            "import sys, time\n"
            "from hermes_cli.active_sessions import try_acquire_active_session, find_takeover_of, "
            "acknowledge_takeover\n"
            "lease, r = try_acquire_active_session(session_id='S', surface='desktop', config={}, "
            "metadata={'live_session_id': 'a'}, track_liveness=True)\n"
            "assert r is None, r\n"
            "print(lease.lease_id, flush=True)\n"
            "deadline = time.monotonic() + 30\n"
            "while time.monotonic() < deadline and find_takeover_of(lease.lease_id) is None:\n"
            "    time.sleep(0.05)\n"
            "print('acked' if acknowledge_takeover(lease.lease_id) else 'missing', flush=True)\n"
        )],
        env={**os.environ, "HERMES_HOME": str(home),
             "PYTHONPATH": os.pathsep.join(filter(None, [str(Path(__file__).resolve().parents[2]),
                                                         os.environ.get("PYTHONPATH", "")]))},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        old_lease_id = holder.stdout.readline().strip()
        assert old_lease_id, holder.stderr.read()
        lease, removed, refusal = take_over_active_session(
            session_id="S", surface="desktop", metadata={"live_session_id": "b"}, track_liveness=True)
        assert refusal is None and [e["lease_id"] for e in removed] == [old_lease_id]
        assert holder.stdout.readline().strip() == "acked"
        holder.wait(timeout=30)
        assert takeover_acknowledged(lease, removed) is True
    finally:
        if holder.poll() is None:
            holder.kill()
        holder.communicate()
