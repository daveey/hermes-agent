"""Invalidation of roster membership never invalidates/fakes activity clocks."""
import sqlite3
import time

from tests.tui_gateway import test_process_subagent_observation_rpc as rpc

runtime = rpc.runtime
sleeper = rpc.sleeper


def test_owner_add_remove_and_reassignment(runtime):
    from tui_gateway import server
    db = server._get_db()
    child = rpc._child(runtime, owner="new-owner")
    assert rpc._roster(runtime) == {}
    db.create_session("new-owner", source="desktop")
    assert set(rpc._roster(runtime)) == {"new-owner"}
    child["owner_agent_session_id"] = "parent-a"
    assert set(rpc._roster(runtime)) == {"parent-a"}
    runtime.active.clear()
    assert rpc._roster(runtime) == {}


def test_external_commit_invalidates_membership(runtime):
    from tui_gateway import server
    db = server._get_db()
    rpc._child(runtime)
    assert set(rpc._roster(runtime)) == {"parent-a"}
    with sqlite3.connect(db.db_path) as conn:
        conn.execute("DELETE FROM sessions WHERE id = ?", ("parent-a",))
    assert rpc._roster(runtime) == {}


def test_new_store_same_version_never_inherits_owner(runtime, tmp_path, monkeypatch):
    from hermes_state import SessionDB
    from tui_gateway import server
    rpc._child(runtime)
    assert set(rpc._roster(runtime)) == {"parent-a"}
    other = SessionDB(db_path=tmp_path / "other.db")
    try:
        other.create_session("foreign", source="desktop")
        monkeypatch.setattr(server, "_get_db", lambda: other)
        assert rpc._roster(runtime) == {}
    finally:
        other.close()


def test_due_time_and_fresh_activity_project_on_cached_membership(runtime, monkeypatch):
    now = time.time()
    child = rpc._child(runtime, activity=now, started=now)
    monkeypatch.setattr(time, "time", lambda: now + 179)
    before = rpc._roster(runtime)["parent-a"]["execution_observations"][0]
    assert before["state"] == "running"
    monkeypatch.setattr(time, "time", lambda: now + 181)
    expired = rpc._roster(runtime)["parent-a"]["execution_observations"][0]
    assert expired["state"] == "unknown" and expired["observed_at"] == now
    child["agent"].activity = now + 181
    fresh = rpc._roster(runtime)["parent-a"]["execution_observations"][0]
    assert fresh["state"] == "running" and fresh["observed_at"] == now + 181
    child["agent"].session_id = "replacement-generation"
    assert rpc._roster(runtime)["parent-a"]["execution_observations"][0]["identity"]["work_id"] == "replacement-generation"


def test_route_namespace_change_invalidates_without_db_write(runtime, monkeypatch):
    from tui_gateway import server
    db = server._get_db()
    route = "agent:main:local:dm:owner"
    db.create_session("route-owner", source="local", session_key=route)
    rpc._register(runtime, "proc_route", 0, session_key=route)
    monkeypatch.setattr(server, "_route_namespaces", lambda _: {"main"})
    assert set(rpc._roster(runtime)) == {"route-owner"}
    monkeypatch.setattr(server, "_route_namespaces", lambda _: {"other"})
    assert rpc._roster(runtime) == {}


def test_stored_owners_do_not_discover_route_namespaces(runtime, monkeypatch):
    from tui_gateway import server
    rpc._child(runtime)
    calls = []
    monkeypatch.setattr(server, "_route_namespaces", lambda _: calls.append(1) or {"main"})
    assert set(rpc._roster(runtime)) == {"parent-a"}
    assert calls == []
    rpc._register(runtime, "proc_route", 0, session_key="agent:main:local:dm:missing")
    rpc._roster(runtime)
    assert calls == [1]


def test_process_exit_and_new_owner_are_not_memoized(runtime, sleeper):
    from tui_gateway import server
    server._get_db().create_session("other-owner", source="desktop")
    rpc._register(runtime, "proc_live", sleeper().pid, parent_session_id="parent-a")
    assert rpc._roster(runtime)["parent-a"]["background"] == 1
    process = runtime.registry._running["proc_live"]
    process.parent_session_id = "other-owner"
    assert set(rpc._roster(runtime)) == {"other-owner"}
    process.exited = True
    assert rpc._roster(runtime) == {}


def test_pending_transaction_result_is_not_reused_after_rollback(runtime):
    from tui_gateway import server
    from tui_gateway.roster_owner_reads import resolve_owners
    db = server._get_db()
    owners = [("subagents", ("parent-a", ""), {})]
    def resolve(db, ids, namespaces):
        return ids[0] if db._conn.execute("SELECT id FROM sessions WHERE id = ?", (ids[0],)).fetchone() else ""
    assert resolve_owners(db, owners, set(), resolve)[("parent-a", "")] == "parent-a"
    db._conn.execute("BEGIN")
    try:
        db._conn.execute("DELETE FROM sessions WHERE id = ?", ("parent-a",))
        assert resolve_owners(db, owners, set(), resolve)[("parent-a", "")] == ""
    finally:
        db._conn.rollback()
    assert resolve_owners(db, owners, set(), resolve)[("parent-a", "")] == "parent-a"


def test_change_during_resolve_is_not_published(runtime):
    from tui_gateway import server
    from tui_gateway.roster_owner_reads import resolve_owners
    db = server._get_db()
    calls = []
    def resolve(db, ids, namespaces):
        calls.append(ids)
        db.create_session(f"changed-{len(calls)}", source="desktop")
        return ids[0]
    owners = [("subagents", ("parent-a", ""), {})]
    resolve_owners(db, owners, {"main"}, resolve)
    resolve_owners(db, owners, {"main"}, resolve)
    assert len(calls) == 2
