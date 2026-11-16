"""Synthetic K-scale replay, not a replacement for the frozen live whole-RPC gate."""
import cProfile
import json
import pstats
import time

import pytest

from tests.tui_gateway import test_process_subagent_observation_rpc as rpc

runtime = rpc.runtime
sleeper = rpc.sleeper
_REAL_SCOPE = rpc.keo.local_scope


@pytest.mark.parametrize("k", [2, 8, 32])
def test_nonempty_roster_replay(runtime, sleeper, monkeypatch, k):
    from tui_gateway import server
    from tui_gateway.roster_owner_reads import _memos
    db = server._get_db()
    monkeypatch.setattr(rpc.keo, "local_scope", _REAL_SCOPE)
    for i in range(k):
        sid = f"replay-{i}"
        db.create_session(sid, source="desktop", system_prompt="roster replay payload " * 10000)
        rpc._register(runtime, f"proc_{i}", sleeper().pid, parent_session_id=sid)
    # Same registry + SQLite + projector, old membership path vs new. Never live traffic.
    new_resolve = server._stored_owner
    def old_resolve(db, ids, namespaces):
        if ids[0]:
            return ids[0] if db.get_session(ids[0]) else ""
        return new_resolve(db, ids, namespaces)
    import tui_gateway.roster_owner_reads as reads
    gate = reads.resolve_owners
    def baseline_gate(db, owners, namespaces, resolve):
        # The original handler unconditionally discovered namespaces, even when
        # every owner supplied a durable id (this fixture's launch-profile case).
        namespaces = server._route_namespaces(None)
        return {ids: old_resolve(db, ids, namespaces) for _, ids, _ in owners}
    results = []
    for label in ("before", "after"):
        _memos.clear()
        monkeypatch.setattr(reads, "resolve_owners", baseline_gate if label == "before" else gate)
        runtime.call("subagent.roster")
        start = time.process_time()
        for _ in range(467):
            assert len(runtime.call("subagent.roster")["sessions"]) == k
        unprofiled_cpu = time.process_time() - start
        profiler = cProfile.Profile(timer=time.process_time)
        start = time.process_time()
        profiler.enable()
        for _ in range(467):
            rows = runtime.call("subagent.roster")["sessions"]
            assert len(rows) == k and all(row["background"] == 1 for row in rows)
        profiler.disable()
        cpu = time.process_time() - start
        stats = pstats.Stats(profiler)
        components = {name: sum(v[3] for key, v in stats.stats.items() if key[2] == name)
                      for name in ("get_session", "get_session_list_row", "resolve_owners", "_route_namespaces",
                                   "_observation_scope", "_roster_observations", "_live_work_owners")}
        results.append({"mode": label, "k": k, "calls": 467, "cpu_s": unprofiled_cpu, "profiled_cpu_s": cpu, "components": components})
    print("ROSTER_REPLAY " + json.dumps(results, sort_keys=True))
