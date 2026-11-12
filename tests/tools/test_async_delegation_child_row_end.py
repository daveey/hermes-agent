"""A delegate child row ends when its unit reaches a terminal state the child never closed itself.

Hub incident (subagent 20261111_094134_4755e7): a backend restart killed the owner of async delegation
``deleg_5facc89f`` at 10:06:01; the next process recorded the unit ``unknown`` 17 s later, but the child's
session row stayed ``ended_at IS NULL`` for 27 h+. ``recover_abandoned_delegations`` never touched it, and the
startup orphan sweep refused it because SOME live backend's heartbeat predated it. Three producers now end it:

- recovery of a dead owner's unit ends the unit's open child rows (``delegation_unknown``);
- a stall-monitor force-finalize ends them too (``delegation_stalled``);
- the startup sweep ends a ``subagent`` row whose parent row has ended, with no heartbeat guess
  (``parent_ended_reap``), which also closes rows left open before this fix.
"""

from __future__ import annotations

import json
import time

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from hermes_state import SessionDB
from tools import async_delegation as ad

IDLE_S = 6 * 3600


@pytest.fixture(autouse=True)
def _clean_state():
    ad._reset_for_tests()
    yield
    ad._reset_for_tests()


@pytest.fixture
def home(tmp_path):
    token = set_hermes_home_override(str(tmp_path))
    try:
        yield tmp_path
    finally:
        reset_hermes_home_override(token)


def _session_row(home, sid):
    db = SessionDB(home / "state.db")
    try:
        return db.get_session(sid)
    finally:
        db.close()


def _seed_sessions(home, *, child_parent="parent-1", child_source="subagent"):
    db = SessionDB(home / "state.db")
    try:
        db.create_session("parent-1", source="desktop")
        db.create_session("other-parent", source="desktop")
        db.create_session("child-1", source=child_source, parent_session_id=child_parent)
        db.create_session("child-done", source="subagent", parent_session_id="parent-1")
        db.end_session("child-done", "agent_close")
    finally:
        db.close()


def _dead_owner_unit(task_session_ids):
    """A running ledger row whose owner pid is gone."""
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute(
            "INSERT INTO async_delegations (delegation_id, origin_session, state, dispatched_at, updated_at, "
            "delivery_state, delivery_attempts, owner_pid, owner_started_at, task_json, parent_session_id) "
            "VALUES ('deleg-x', 'parent-1', 'running', 1, 1, 'pending', 0, 999999, 1, ?, 'parent-1')",
            (json.dumps({"goal": "g", "is_batch": True, "goals": ["g"], "task_session_ids": task_session_ids}),))


def test_recovered_unknown_unit_ends_its_open_child_row(home, monkeypatch):
    _seed_sessions(home)
    _dead_owner_unit({"0": "child-1", "1": "child-done"})
    monkeypatch.setattr(ad, "_owner_liveness", lambda: (lambda pid, started: False))

    assert ad.recover_abandoned_delegations() == 1

    child = _session_row(home, "child-1")
    assert child["ended_at"] is not None
    assert child["end_reason"] == "delegation_unknown"
    # A child that closed its own row keeps its first reason.
    assert _session_row(home, "child-done")["end_reason"] == "agent_close"
    assert _session_row(home, "parent-1")["ended_at"] is None


@pytest.mark.parametrize("child_parent,child_source", [("other-parent", "subagent"), ("parent-1", "desktop")])
def test_recovery_never_ends_a_row_that_is_not_this_parents_subagent(home, monkeypatch, child_parent, child_source):
    _seed_sessions(home, child_parent=child_parent, child_source=child_source)
    _dead_owner_unit({"0": "child-1"})
    monkeypatch.setattr(ad, "_owner_liveness", lambda: (lambda pid, started: False))

    assert ad.recover_abandoned_delegations() == 1
    assert _session_row(home, "child-1")["ended_at"] is None


def test_dispatch_persists_child_session_ids(home):
    release = []
    handle = ad.dispatch_async_delegation_batch(
        goals=["g"], context=None, toolsets=None, role="leaf", model="m", session_key="k",
        parent_session_id="parent-1", runner=lambda: (release.append(1), {"results": []})[1],
        task_session_ids={"0": "child-1"})
    assert handle["status"] == "dispatched"
    with ad._DB_LOCK, ad._transaction() as conn:
        task = json.loads(conn.execute("SELECT task_json FROM async_delegations WHERE delegation_id=?",
                                       (handle["delegation_id"],)).fetchone()[0])
    assert task["task_session_ids"] == {"0": "child-1"}


def test_stall_force_finalize_ends_open_child_rows(home, monkeypatch):
    _seed_sessions(home)
    record = {"delegation_id": "deleg-s", "status": "stalling", "parent_session_id": "parent-1", "goal": "g",
              "is_batch": True, "goals": ["g"], "task_session_ids": {"0": "child-1"}, "dispatched_at": time.time(),
              "_interrupted_at": time.time(), "_stall_quiet_seconds": 500.0, "_stall_threshold_seconds": 450.0,
              "_stall_in_tool": False}
    ad._persist_dispatch(record)
    with ad._records_lock:
        ad._records["deleg-s"] = record

    ad._finalize("deleg-s", lambda rec: ad._stalled_result("deleg-s", rec), "stalled")

    child = _session_row(home, "child-1")
    assert child["end_reason"] == "delegation_stalled"
    assert child["ended_at"] is not None


# ── startup sweep: a subagent whose parent has ended ─────────────────────────


def _backdate(db, sid, ts):
    db._conn.execute("UPDATE sessions SET started_at = ? WHERE id = ?", (ts, sid))
    db._conn.commit()


@pytest.fixture
def db(tmp_path):
    d = SessionDB(tmp_path / "sweep.db")
    yield d
    d.close()


def _hub_like(db, *, parent_ended: bool):
    """Parent + idle subagent child, with a live backend heartbeat that predates the child (the hub's
    many-restarts-a-day shape that kept the orphan sweep from ever closing the child)."""
    now = time.time()
    stale = now - 8 * 3600
    db.create_session("parent", source="desktop")
    db.create_session("child", source="subagent", parent_session_id="parent")
    _backdate(db, "parent", stale - 3600)
    _backdate(db, "child", stale)
    if parent_ended:
        db.end_session("parent", "startup_orphan_reap")
    db.register_backend_heartbeat(backend_id="other-backend", pid=4242, started_at=stale - 60,
                                  last_heartbeat=now, profile="default", host="hub")


def test_heartbeat_guess_alone_keeps_the_child_open(db):
    """The incident's producer: the orphan sweep defers to an unrelated live backend."""
    _hub_like(db, parent_ended=True)
    assert db.sweep_orphaned_sessions(max_idle_seconds=IDLE_S, sources=("subagent",)) == []
    assert db.get_session("child")["ended_at"] is None


def test_subagent_of_ended_parent_is_closed_without_heartbeat_guess(db):
    _hub_like(db, parent_ended=True)
    assert db.end_subagents_of_ended_parents(max_idle_seconds=IDLE_S) == ["child"]
    row = db.get_session("child")
    assert row["end_reason"] == "parent_ended_reap"
    assert row["ended_at"] is not None


def test_subagent_of_open_parent_is_left_alone(db):
    _hub_like(db, parent_ended=False)
    assert db.end_subagents_of_ended_parents(max_idle_seconds=IDLE_S) == []
    assert db.get_session("child")["ended_at"] is None


def test_recent_subagent_of_ended_parent_is_left_alone(db):
    db.create_session("parent", source="desktop")
    db.create_session("child", source="subagent", parent_session_id="parent")
    db.end_session("parent", "agent_close")
    assert db.end_subagents_of_ended_parents(max_idle_seconds=IDLE_S) == []
    assert db.get_session("child")["ended_at"] is None


def test_excluded_live_subagent_is_left_alone(db):
    _hub_like(db, parent_ended=True)
    assert db.end_subagents_of_ended_parents(max_idle_seconds=IDLE_S, exclude_ids=("child",)) == []


def test_startup_sweep_closes_subagent_of_ended_parent_despite_live_heartbeat(db, monkeypatch):
    """Gateway wiring: the startup sweep ends the child the heartbeat guess spared, with no killed-subagent
    notice (that reason is reserved for ``startup_orphan_reap``)."""
    from tui_gateway import server
    _hub_like(db, parent_ended=True)
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_SESSION_TTL_S", float(IDLE_S))
    monkeypatch.setattr(server, "_sessions", {})

    assert server._sweep_orphaned_session_rows() == []  # nothing for the heartbeat-gated orphan sweep
    assert db.get_session("child")["end_reason"] == "parent_ended_reap"
