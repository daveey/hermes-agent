"""``subagent.roster`` / ``subagent.list`` read the observation scope once per request, not once per owner.

The roster is a 0.9 Hz Desktop poll. With live children or background processes it projected one observation per
owner, each re-reading ``local_scope()`` (kanban config, hub.db gateway name, active profile): about 0.7 ms per
owner on the hub, 0.59% of a core during a six-owner burst. The scope is process-wide within one request, so one
read serves every row; it is still read on every request (no cross-request cache). [daveey]
"""
import time

from hermes_cli import kanban_execution_observations as keo
from tests.tui_gateway import test_process_subagent_observation_rpc as rpc

runtime, sleeper = rpc.runtime, rpc.sleeper
_child, _register, _roster, _subagents = rpc._child, rpc._register, rpc._roster, rpc._subagents


def _count_scope(monkeypatch, answer=("gw-1", "default")):
    calls = []

    def scope():
        calls.append(1)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(keo, "local_scope", scope)
    return calls


def test_roster_reads_scope_once_for_many_owners(runtime, sleeper, monkeypatch):
    from tui_gateway import server

    db = server._get_db()
    for i in range(4):
        db.create_session(f"conv-{i}", source="tui")
        _child(runtime, f"c{i}", owner=f"conv-{i}", activity=time.time())
    route = "agent:main:local:dm:desktop-chat-9"
    db.create_session("local-bg", source="local", session_key=route)
    for i in range(2):
        _register(runtime, f"proc_{i}", sleeper().pid, session_key=route)
    calls = _count_scope(monkeypatch)

    rows = _roster(runtime)
    assert set(rows) == {"conv-0", "conv-1", "conv-2", "conv-3", "local-bg"}
    observed = [o for r in rows.values() for o in r["execution_observations"]]
    assert len(observed) == 6 and all(o["identity"].get("gateway") == "gw-1" for o in observed)
    assert len(calls) == 1, f"local_scope read {len(calls)} times for one roster request"
    _roster(runtime)
    assert len(calls) == 2, "the next request must read the scope again (no cross-request cache)"


def test_subagent_list_reads_scope_once_for_many_children(runtime, monkeypatch):
    for i in range(3):
        _child(runtime, f"c{i}", activity=time.time())
    calls = _count_scope(monkeypatch)
    rows = _subagents(runtime)
    assert set(rows) == {"c0", "c1", "c2"}
    assert all(r["execution_observations"][0]["identity"].get("gateway") == "gw-1" for r in rows.values())
    assert len(calls) == 1


def test_roster_without_rows_in_this_profile_reads_no_scope(runtime, monkeypatch):
    _child(runtime, owner="someone-elses-session", activity=time.time())
    calls = _count_scope(monkeypatch)
    assert _roster(runtime) == {}
    assert calls == []


def test_scope_failure_leaves_observations_unscoped_not_dropped(runtime, sleeper, monkeypatch):
    from tui_gateway import server

    route = "agent:main:local:dm:desktop-chat-2"
    server._get_db().create_session("local-x", source="local", session_key=route)
    _register(runtime, "proc_x", sleeper().pid, session_key=route)
    _child(runtime, "c1", activity=time.time())
    calls = _count_scope(monkeypatch, RuntimeError("scope read failed"))
    rows = _roster(runtime)
    assert len(calls) == 1
    for sid in ("parent-a", "local-x"):
        (obs,) = rows[sid]["execution_observations"]
        assert obs["identity"].get("gateway") is None
    (row,) = _subagents(runtime).values()
    assert row["execution_observations"][0]["identity"].get("gateway") is None
