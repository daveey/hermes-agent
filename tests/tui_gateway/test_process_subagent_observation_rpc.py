"""Optional v1 ``execution_observations`` on the authorized RPC paths: ``process.list``, ``subagent.list``
and ``subagent.roster``, through the real dispatcher, registries and projector."""

import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_execution_observations as keo
from hermes_cli import process_session_activity as psa
from tools.process_registry import ProcessRegistry, ProcessSession


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    from hermes_state import SessionDB
    from tools import async_delegation, delegate_tool_registry as reg
    from tui_gateway import server
    import tools.process_registry as pr

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    registry = ProcessRegistry()
    monkeypatch.setattr(pr, "process_registry", registry)
    monkeypatch.setattr(keo, "local_scope", lambda: ("gw-1", "default"))
    frames, arrived = [], threading.Event()

    def write(frame):
        frames.append(frame)
        arrived.set()
        return True

    transport = SimpleNamespace(write=write)
    owner = {"session_key": "parent-a", "history": [], "transport": transport}
    monkeypatch.setattr(server, "_sessions", {"ui-owner": owner})
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("parent-a", source="tui")
    monkeypatch.setattr(server, "_get_db", lambda: db)
    registries = (reg._active_subagents, reg._recent_subagents, async_delegation._records)
    saved = [(r, dict(r)) for r in registries]
    for r in registries:
        r.clear()
    psa._reset_cache_for_tests()

    def call(method, **params):
        if method == "subagent.roster":  # sessionless: the same entry the existing roster tests use
            resp = server.handle_request({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        else:
            arrived.clear()
            frames.clear()
            resp = server.dispatch({"id": 1, "method": method, "params": params}, transport=transport)
            if resp is None:  # long handlers answer on the worker pool, through the transport
                assert arrived.wait(10), "no response frame"
                resp = frames[-1]
        assert "error" not in resp, resp
        return resp["result"]

    yield SimpleNamespace(call=call, registry=registry, owner=owner, transport=transport, active=reg._active_subagents)
    db.close()
    psa._reset_cache_for_tests()
    for r, snapshot in saved:
        r.clear()
        r.update(snapshot)


@pytest.fixture
def sleeper():
    procs = []

    def spawn():
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        procs.append(proc)
        return proc

    yield spawn
    for proc in procs:
        proc.kill()
        proc.wait()


def _register(rt, proc_id, pid, *, session_key="parent-a", **extra):
    kwargs = {"host_start_time": ProcessRegistry._safe_host_start_time(pid), **extra}
    rt.registry._running[proc_id] = ProcessSession(id=proc_id, command="sleep 60 --token=hunter2", task_id="default",
                                                   session_key=session_key, pid=pid, started_at=time.time(), **kwargs)


class _Child:
    def __init__(self, activity):
        self.session_id = "child-session"
        self.activity = activity

    def get_activity_summary(self):
        return {"last_activity_ts": self.activity}


def _child(rt, sid="c1", *, activity=None, started=None, delegation="d1", owner="parent-a", status="running", depth=0):
    started = time.time() - 5 if started is None else started
    rt.active[sid] = {"subagent_id": sid, "depth": depth, "delegation_id": delegation, "started_at": started,
                      "status": status, "owner_agent_session_id": owner, "owner_session_id": "ui-owner",
                      "owner_transport": rt.transport, "owner_session_record": rt.owner, "tool_count": 3,
                      "last_tool": "bash", "agent": _Child(activity)}
    return rt.active[sid]


def test_nonlaunch_profile_row_rpc_and_roster_share_one_process_identity():
    import runpy
    from pathlib import Path

    sample = runpy.run_path(str(Path(__file__).parents[1] / "fixtures" / "process_profile_parity.py"))["sample"]()
    stamp = sample["row"]["execution_observations"][0]
    roster = sample["roster"][0]["execution_observations"][0]
    rpc = sample["processes"][0]["execution_observations"][0]
    assert stamp["state"] == roster["state"] == rpc["state"] == "running"
    assert stamp["identity"] == roster["identity"] == rpc["identity"]
    assert stamp["identity"]["profile"] == "work"
    assert sample["row"]["background_active"] == sample["roster"][0]["background"] == 1


# ── process.list ────────────────────────────────────────────────────────────────────────────────


def test_process_list_row_carries_birth_qualified_observation_and_legacy_fields(runtime, sleeper):
    live = sleeper()
    _register(runtime, "proc_a", live.pid)
    before = time.time()
    (row,) = runtime.call("process.list", session_id="ui-owner")["processes"]

    assert row["session_id"] == "proc_a" and row["status"] == "running" and row["pid"] == live.pid
    (obs,) = row["execution_observations"]
    start = ProcessRegistry._safe_host_start_time(live.pid)
    assert obs["state"] == "running" and obs["freshness"] == "current" and obs["source"] == "process.registry"
    assert obs["identity"] == {"kind": "process", "work_id": "proc_a", "gateway": "gw-1", "profile": "default",
                               "session": "parent-a", "generation": f"pid={live.pid}:start={start}"}
    assert before <= obs["observed_at"] <= time.time() and obs["valid_until"] == obs["observed_at"] + 180
    assert obs["last_progress"] is None and obs["last_activity"] is None
    assert "hunter2" not in repr(obs)  # command text never enters the observation


def test_process_list_foreign_process_is_not_listed_or_observed(runtime, sleeper):
    live = sleeper()
    _register(runtime, "proc_other", live.pid, session_key="parent-b")
    assert runtime.call("process.list", session_id="ui-owner")["processes"] == []


def test_process_list_pid_reuse_birth_mismatch_is_unknown(runtime, sleeper):
    live = sleeper()
    _register(runtime, "proc_reused", live.pid, host_start_time=ProcessRegistry._safe_host_start_time(live.pid) + 1)
    (row,) = runtime.call("process.list", session_id="ui-owner")["processes"]
    (obs,) = row["execution_observations"]
    assert obs["state"] == "unknown" and obs["unknown_reason"] == "missing_qualified_identity"
    assert "generation" not in obs["identity"] and obs["observed_at"] is None  # a failed probe never dates running


def test_process_list_non_host_pid_with_a_start_tick_has_no_host_proof(runtime, sleeper):
    live = sleeper()  # a real host pid and start tick, but registered as a sandbox/container pid
    _register(runtime, "proc_box", live.pid, pid_scope="sandbox")
    (row,) = runtime.call("process.list", session_id="ui-owner")["processes"]
    (obs,) = row["execution_observations"]
    assert obs["state"] == "unknown" and "generation" not in obs["identity"]


def test_process_list_exited_process_is_never_dated_running_from_a_cached_probe(runtime, sleeper):
    live = sleeper()
    _register(runtime, "proc_x", live.pid)
    first = runtime.call("process.list", session_id="ui-owner")["processes"][0]
    assert first["execution_observations"][0]["state"] == "running"  # populates the 3s cache
    runtime.registry._running["proc_x"].exited = True
    (row,) = runtime.call("process.list", session_id="ui-owner")["processes"]
    assert row["status"] == "exited"
    assert row["execution_observations"][0]["state"] == "unknown"


def test_process_list_observation_expires_without_a_new_probe(runtime, sleeper, monkeypatch):
    live = sleeper()
    _register(runtime, "proc_a", live.pid)
    first = runtime.call("process.list", session_id="ui-owner")["processes"][0]["execution_observations"][0]
    monkeypatch.setattr(psa.time, "time", lambda: first["observed_at"] + 181)
    again = runtime.call("process.list", session_id="ui-owner")["processes"][0]["execution_observations"][0]
    assert again["observed_at"] == first["observed_at"]  # cached probe, not renewed by the request
    assert again["state"] == "unknown" and again["unknown_reason"] == "source_expired"


def test_process_list_adapter_failure_keeps_legacy_rows(runtime, sleeper, monkeypatch):
    live = sleeper()
    _register(runtime, "proc_a", live.pid)
    monkeypatch.setattr(psa, "_owned_processes", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    (row,) = runtime.call("process.list", session_id="ui-owner")["processes"]
    assert row["session_id"] == "proc_a" and row["execution_observations"][0]["state"] == "unknown"


# ── subagent.list / subagent.roster ─────────────────────────────────────────────────────────────


def _subagents(rt):
    return {r["subagent_id"]: r for r in rt.call("subagent.list", session_id="ui-owner")["subagents"]}


def _roster(rt):
    return {e["session_id"]: e for e in rt.call("subagent.roster")["sessions"]}


def test_real_rpc_and_persisted_annotation_use_identical_child_records(runtime, monkeypatch):
    from hermes_cli import subagent_session_activity as activity
    from tui_gateway import server

    now = time.time()
    record = _child(runtime, activity=now - 3, started=now - 600)
    record["agent"]._parent_session_id = "parent-a"
    db = server._get_db()
    assert db is not None
    db.create_session("child-session", source="subagent", parent_session_id="parent-a")
    db.touch_session_activity("child-session", now - 3)
    monkeypatch.setattr(activity, "_profile_paths", lambda: [("default", Path(db.db_path))])
    monkeypatch.setattr(activity, "local_scope", lambda: ("gw-1", "default"))
    monkeypatch.setattr(activity.time, "time", lambda: now)
    activity._reset_cache_for_tests()
    try:
        persisted = activity.annotate_subagent_activity([{"id": "parent-a"}])[0]["execution_observations"]
        assert persisted == _subagents(runtime)["c1"]["execution_observations"]
        assert persisted == _roster(runtime)["parent-a"]["execution_observations"]
    finally:
        activity._reset_cache_for_tests()


@pytest.mark.parametrize("path", ["list", "roster"])
def test_running_child_is_dated_by_its_own_activity_clock_on_both_paths(runtime, path):
    now = time.time()
    _child(runtime, activity=now - 3, started=now - 600)  # spawned long ago, still active
    if path == "list":
        row = _subagents(runtime)["c1"]
        assert row["tool_count"] == 3 and row["last_tool"] == "bash" and row["delegation_id"] == "d1"
    else:
        row = _roster(runtime)["parent-a"]
        assert row["subagents"] == 1 and row["background"] == 0
    (obs,) = row["execution_observations"]
    assert obs["state"] == "running" and obs["freshness"] == "current" and obs["source"] == "subagent.activity"
    assert obs["identity"]["kind"] == "subagent" and obs["identity"]["work_id"] == "child-session"
    assert obs["identity"]["session"] == "parent-a" and obs["identity"]["gateway"] == "gw-1"
    assert obs["identity"]["generation"] == "session=child-session"
    assert obs["observed_at"] == now - 3 and obs["last_activity"] == now - 3 and obs["valid_until"] == now - 3 + 180
    assert obs["last_progress"] is None


def test_wedged_child_expires_without_a_new_fetch_and_tool_counts_are_not_progress(runtime):
    now = time.time()
    record = _child(runtime, activity=now - 200, started=now - 400)
    obs = _subagents(runtime)["c1"]["execution_observations"][0]
    assert obs["state"] == "unknown" and obs["unknown_reason"] == "source_expired" and obs["observed_at"] == now - 200
    record["tool_count"] += 5
    record["last_tool"] = "write"
    obs = _subagents(runtime)["c1"]["execution_observations"][0]
    assert obs["observed_at"] == now - 200 and obs["last_progress"] is None and obs["state"] == "unknown"


def test_child_without_a_usable_activity_clock_falls_back_to_spawn_time_and_expires(runtime):
    now = time.time()
    _child(runtime, "fresh", activity=None, started=now - 10)
    _child(runtime, "future", activity=now + 500, started=now - 10, delegation="d2")  # future clock is rejected
    _child(runtime, "old", activity=None, started=now - 500, delegation="d3")
    rows = _subagents(runtime)
    assert rows["fresh"]["execution_observations"][0]["observed_at"] == now - 10
    assert rows["fresh"]["execution_observations"][0]["state"] == "running"
    assert rows["future"]["execution_observations"][0]["observed_at"] == now - 10
    assert rows["old"]["execution_observations"][0]["unknown_reason"] == "source_expired"


def test_child_missing_delegation_or_owner_or_not_running_is_unknown(runtime):
    now = time.time()
    _child(runtime, "nodeleg", delegation=None, activity=now - 1)
    _child(runtime, "noowner", owner=None, activity=now - 1)
    _child(runtime, "done", status="completed", activity=now - 1)
    rows = _subagents(runtime)
    assert rows["nodeleg"]["execution_observations"][0]["state"] == "running"  # delegation id is not child identity
    assert rows["noowner"]["execution_observations"][0]["state"] == "unknown"
    assert rows["done"]["execution_observations"][0]["unknown_reason"] == "subagent_not_running"


def test_reused_child_id_never_aliases_the_previous_generation(runtime):
    now = time.time()
    _child(runtime, activity=now - 2, started=now - 100, delegation="d-old")
    old = _subagents(runtime)["c1"]["execution_observations"][0]["identity"]
    record = _child(runtime, activity=now - 1, started=now - 50, delegation="d-new")
    record["agent"].session_id = "new-child-session"
    new = _subagents(runtime)["c1"]["execution_observations"][0]["identity"]
    assert old["work_id"] != new["work_id"] and old["generation"] != new["generation"]


def test_one_broken_child_beside_a_fresh_child_stays_unknown_without_hiding_it(runtime):
    now = time.time()
    _child(runtime, "ok", activity=now - 1)
    runtime.active["bad"] = {"subagent_id": "bad", "depth": 0, "owner_agent_session_id": "parent-a", "status": "running",
                             "owner_session_id": "ui-owner", "owner_transport": runtime.transport,
                             "owner_session_record": runtime.owner, "agent": object()}
    rows = _subagents(runtime)
    assert rows["ok"]["execution_observations"][0]["state"] == "running"
    assert rows["bad"]["execution_observations"][0]["state"] == "unknown"
    assert _roster(runtime)["parent-a"]["subagents"] == 2


def test_roster_carries_process_observation_for_a_stored_owner_route(runtime, sleeper):
    live = sleeper()
    route = "agent:main:local:dm:desktop-chat-1"
    from tui_gateway import server
    server._get_db().create_session("local-abc123", source="local", session_key=route)
    _register(runtime, "proc_route", live.pid, session_key=route)
    row = _roster(runtime)["local-abc123"]
    assert row["subagents"] == 0 and row["background"] == 1
    (obs,) = row["execution_observations"]
    assert obs["source"] == "process.registry" and obs["state"] == "running"
    assert obs["identity"]["session"] == "local-abc123" and obs["identity"]["work_id"] == "proc_route"


def test_roster_other_profile_conversation_still_leaks_nothing(runtime):
    _child(runtime, owner="someone-elses-session", activity=time.time())
    assert _roster(runtime) == {}


def test_subagent_snapshot_stays_scoped_to_its_session_with_observations(runtime):
    _child(runtime, "mine", activity=time.time())
    runtime.active["theirs"] = {"subagent_id": "theirs", "depth": 0, "status": "running",
                                "owner_session_id": "someone-else", "owner_agent_session_id": "other",
                                "agent": _Child(time.time())}
    assert set(_subagents(runtime)) == {"mine"}
