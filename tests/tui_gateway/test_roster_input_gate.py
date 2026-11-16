"""Nonempty roster input-cost replay and invalidation checks."""
import cProfile
import pstats
import time

from tests.tui_gateway import test_process_subagent_observation_rpc as rpc

runtime = rpc.runtime
_REAL_SCOPE = rpc.keo.local_scope


def test_roster_does_not_hydrate_session_payloads(runtime, monkeypatch):
    from tui_gateway import server
    db = server._get_db()
    rpc._child(runtime)
    calls = []
    original = db.get_session
    def read(sid):
        calls.append(sid)
        return original(sid)
    monkeypatch.setattr(db, "get_session", read)
    assert len(runtime.call("subagent.roster")["sessions"]) == 1
    assert calls == [], "membership must not flush accounting or hydrate prompts/config"


def test_roster_stable_owners_skip_resolution(runtime, monkeypatch):
    from tui_gateway import server as methods
    rpc._child(runtime)
    calls = []
    original = methods._stored_owner
    def resolve(*args):
        calls.append(1)
        return original(*args)
    monkeypatch.setattr(methods, "_stored_owner", resolve)
    for _ in range(3):
        assert len(runtime.call("subagent.roster")["sessions"]) == 1
    assert len(calls) == 1


def test_roster_component_profile(runtime, monkeypatch):
    import os
    monkeypatch.setattr(rpc.keo, "local_scope", _REAL_SCOPE)
    for i in range(2):
        rpc._register(runtime, f"proc_{i}", os.getpid(), parent_session_id="parent-a")
    from tui_gateway import server
    db = server._get_db()
    for i in range(8):
        sid = f"roster-owner-{i}"
        db.create_session(sid, source="desktop")
        rpc._child(runtime, f"c{i}", owner=sid, activity=time.time())
    profiler = cProfile.Profile()
    start = time.process_time()
    profiler.enable()
    for _ in range(100):
        assert len(runtime.call("subagent.roster")["sessions"]) == 9
    profiler.disable()
    print("CPU", time.process_time() - start)
    pstats.Stats(profiler).sort_stats("cumtime").print_stats(35)
