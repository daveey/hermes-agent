"""Turn and process execution observations: entry paths through the session producers."""

import json
import os
import sqlite3
import subprocess
import sys
import time

import pytest

from hermes_cli import kanban_execution_observations as keo
from hermes_cli import process_session_activity as psa
from hermes_cli import turn_session_activity as tsa
from hermes_cli.execution_observations import identity_key
from tools.process_registry import ProcessRegistry, ProcessSession


@pytest.fixture(name="home")
def _home_fixture(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import tools.process_registry as pr

    monkeypatch.setattr(pr, "process_registry", ProcessRegistry())
    monkeypatch.setattr(keo, "local_scope", lambda: ("gw-1", "default"))
    tsa._reset_cache_for_tests()
    psa._reset_cache_for_tests()
    yield tmp_path
    tsa._reset_cache_for_tests()
    psa._reset_cache_for_tests()


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


def _leases(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE IF NOT EXISTS session_turn_leases (conversation_id TEXT PRIMARY KEY,"
                 " holder TEXT NOT NULL, acquired_at REAL NOT NULL, expires_at REAL NOT NULL)")
    conn.executemany("INSERT INTO session_turn_leases VALUES (?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()


def _holder(sid, nonce="n1"):
    return f"pid={os.getpid()}:turn={sid}:{nonce}"


def _only(row, source):
    found = [v for v in row.get("execution_observations", []) if v["source"] == source]
    assert len(found) == 1, row
    return found[0]


# ── turns ───────────────────────────────────────────────────────────────────────────────────────


def test_live_turn_lease_is_observed_with_source_times_and_no_progress(home):
    now = time.time()
    _leases(home / "state.db", [("root", _holder("root"), now - 5, now + 600)])
    row = tsa.annotate_turn_activity([{"id": "tip", "_lineage_root_id": "root"}])[0]

    obs = _only(row, "turn.lease")
    assert obs["state"] == "running" and obs["freshness"] == "current"
    assert obs["identity"] == {"kind": "turn", "work_id": "root", "gateway": "gw-1", "profile": "default",
                               "session": "root", "generation": obs["identity"]["generation"]}
    assert obs["identity"]["generation"].startswith("holder-sha256:") and _holder("root") not in json.dumps(obs)
    assert obs["observed_at"] == now - 5 and obs["valid_until"] == now - 5 + 180  # source clock, bounded
    # A lease renewal is not dated by the store, and no heartbeat is progress.
    assert obs["last_progress"] is None and obs["last_activity"] is None
    assert row["turn_live"] is True


def test_turn_is_unknown_not_renewed_when_source_age_exceeds_bound(home):
    now = time.time()
    # Lease acquired 200s ago: nothing dates its later renewals, so a request cannot revive it.
    _leases(home / "state.db", [("a", _holder("a"), now - 200, now + 600)])
    obs = _only(tsa.annotate_turn_activity([{"id": "a"}])[0], "turn.lease")
    assert obs["state"] == "unknown" and obs["unknown_reason"] == "source_expired"
    assert obs["last_known_state"] == "running" and obs["age_seconds"] >= 200


def test_cached_turn_expires_without_a_new_read(home, monkeypatch):
    now = time.time()
    _leases(home / "state.db", [("a", _holder("a"), now - 170, now + 600)])
    assert _only(tsa.annotate_turn_activity([{"id": "a"}])[0], "turn.lease")["state"] == "running"
    monkeypatch.setattr(tsa.time, "time", lambda: now + 20)
    obs = _only(tsa.annotate_turn_activity([{"id": "a"}])[0], "turn.lease")  # same cached collection
    assert obs["state"] == "unknown" and obs["unknown_reason"] == "source_expired"
    assert obs["observed_at"] == now - 170


def test_turn_profile_comes_from_the_state_db_and_unknown_without_gateway(home, monkeypatch):
    now = time.time()
    _leases(home / "profiles" / "work" / "state.db", [("p", _holder("p"), now - 5, now + 600)])
    assert _only(tsa.annotate_turn_activity([{"id": "p"}])[0], "turn.lease")["identity"]["profile"] == "work"
    tsa._reset_cache_for_tests()
    monkeypatch.setattr(keo, "local_scope", lambda: (None, "default"))
    obs = _only(tsa.annotate_turn_activity([{"id": "p"}])[0], "turn.lease")
    assert obs["state"] == "unknown" and obs["unknown_reason"] == "missing_qualified_identity"


def test_new_lease_instance_never_aliases_the_old_one(home):
    now = time.time()
    _leases(home / "state.db", [("a", _holder("a", "old"), now - 5, now + 600)])
    old = _only(tsa.annotate_turn_activity([{"id": "a"}])[0], "turn.lease")
    home.joinpath("state.db").unlink()
    tsa._reset_cache_for_tests()
    _leases(home / "state.db", [("a", _holder("a", "new"), now - 4, now + 600)])
    new = _only(tsa.annotate_turn_activity([{"id": "a"}])[0], "turn.lease")
    assert identity_key(old["identity"]) != identity_key(new["identity"])


def test_turn_append_keeps_other_observations_and_replaces_only_its_own(home):
    now = time.time()
    _leases(home / "state.db", [("a", _holder("a"), now - 5, now + 600)])
    card = {"source": "kanban.card", "state": "unknown"}
    capacity = {"source": "kanban.capacity", "state": "waiting"}
    row = {"id": "a", "execution_observations": [card, {"source": "turn.lease", "state": "stale"}, capacity]}
    tsa.annotate_turn_activity([row])
    assert [v["source"] for v in row["execution_observations"]] == ["kanban.card", "kanban.capacity", "turn.lease"]
    assert row["execution_observations"][0] is card and row["execution_observations"][1] is capacity
    # The lease ends: only the stale turn evidence goes; the others stay.
    home.joinpath("state.db").unlink()
    tsa._reset_cache_for_tests()
    _leases(home / "state.db", [])
    tsa.annotate_turn_activity([row])
    assert [v["source"] for v in row["execution_observations"]] == ["kanban.card", "kanban.capacity"]
    bare = tsa.annotate_turn_activity([{"id": "z", "execution_observations": [{"source": "turn.lease"}]}])[0]
    assert "execution_observations" not in bare


@pytest.fixture(name="rest")
def _rest_fixture(monkeypatch, _isolate_hermes_home):
    starlette = pytest.importorskip("starlette.testclient")
    import hermes_state
    from hermes_cli import profiles
    from hermes_cli.web_server import _SESSION_HEADER_NAME, _SESSION_TOKEN, app
    from hermes_constants import get_hermes_home
    from hermes_state import SessionDB

    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(profiles, "_get_default_hermes_home", lambda: home)
    monkeypatch.setattr(profiles, "_get_profiles_root", lambda: home / "profiles")
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", home / "state.db")
    monkeypatch.setattr(keo, "local_scope", lambda: ("gw-rest", "default"))
    db = SessionDB(db_path=home / "state.db")
    try:
        db.create_session("busy", source="desktop")
        db.append_message("busy", role="user", content="go")
        assert db.try_acquire_session_turn_lease("busy", _holder("busy"), ttl_seconds=600)
    finally:
        db.close()
    tsa._reset_cache_for_tests()
    client = starlette.TestClient(app)
    client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    yield client
    tsa._reset_cache_for_tests()


def test_api_sessions_row_carries_turn_observation(rest):
    rows = {r["id"]: r for r in rest.get("/api/sessions?order=recent&limit=20").json()["sessions"]}
    obs = _only(rows["busy"], "turn.lease")
    assert obs["state"] == "running" and obs["identity"]["gateway"] == "gw-rest"
    assert obs["identity"]["generation"].startswith("holder-sha256:")


# ── processes ───────────────────────────────────────────────────────────────────────────────────


def _entry(sid, pid, **extra):
    base = {"session_id": sid, "pid": pid, "pid_scope": "host", "task_id": "default",
            "host_start_time": ProcessRegistry._safe_host_start_time(pid), "session_key": "s_own"}
    base.update(extra)
    return {k: v for k, v in base.items() if v is not Ellipsis}


def test_checkpoint_process_identity_is_birth_qualified_and_owned_by_matching_session(home, sleeper):
    live = sleeper()
    entry = _entry("proc_a", live.pid)
    (home / "processes.json").write_text(json.dumps([entry]))
    rows = [{"id": "s_own"}, {"id": "other"}]
    before = time.time()
    psa.annotate_background_activity(rows)

    obs = _only(rows[0], "process.checkpoint")
    assert obs["state"] == "running"
    assert obs["identity"] == {"kind": "process", "work_id": "proc_a", "gateway": "gw-1", "profile": "default",
                               "session": "s_own",
                               "generation": f"pid={live.pid}:start={entry['host_start_time']}"}
    assert obs["owner"] == {"gateway": "gw-1", "profile": "default", "session": "s_own"}
    assert before <= obs["observed_at"] <= time.time() and obs["last_progress"] is None
    assert "execution_observations" not in rows[1]
    # The existing detail list is unchanged by the evidence.
    assert set(rows[0]["background_processes"][0]) <= {"id", "command", "started_at", "pid"}


def test_profile_checkpoint_gets_its_profile(home, sleeper):
    live = sleeper()
    path = home / "profiles" / "work" / "processes.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps([_entry("proc_w", live.pid)]))
    rows = psa.annotate_background_activity([{"id": "s_own"}])
    assert _only(rows[0], "process.checkpoint")["identity"]["profile"] == "work"


def test_pid_only_or_birthless_process_is_unknown(home, sleeper):
    a, b = sleeper(), sleeper()
    (home / "processes.json").write_text(json.dumps([
        _entry(Ellipsis, a.pid, session_id=None),  # no registry id: a pid is not an identity
        _entry("proc_nobirth", b.pid, host_start_time=None)]))
    rows = psa.annotate_background_activity([{"id": "s_own"}])
    obs = rows[0]["execution_observations"]
    assert len(obs) == 2 and all(v["state"] == "unknown" for v in obs)
    assert {v["unknown_reason"] for v in obs} == {"missing_qualified_identity"}


def test_in_process_registry_process_is_observed(home, sleeper):
    import tools.process_registry as pr

    live = sleeper()
    start = ProcessRegistry._safe_host_start_time(live.pid)
    pr.process_registry._running["proc_mem"] = ProcessSession(
        id="proc_mem", command="x", task_id="default", session_key="s_mem", pid=live.pid,
        host_start_time=start, started_at=1.0)
    rows = psa.annotate_background_activity([{"id": "s_mem"}])
    obs = _only(rows[0], "process.registry")
    assert obs["state"] == "running" and obs["identity"]["generation"] == f"pid={live.pid}:start={start}"
    assert obs["observed_at"] > 1.0  # probe time, not the spawn time used as a progress claim
    assert obs["last_activity"] is None


def test_cached_process_probe_ages_and_expires_without_a_new_probe(home, sleeper, monkeypatch):
    live = sleeper()
    (home / "processes.json").write_text(json.dumps([_entry("proc_a", live.pid)]))
    first = _only(psa.annotate_background_activity([{"id": "s_own"}])[0], "process.checkpoint")
    now = first["observed_at"]
    monkeypatch.setattr(psa.time, "time", lambda: now + 200)
    later = _only(psa.annotate_background_activity([{"id": "s_own"}])[0], "process.checkpoint")
    assert later["observed_at"] == now  # not renewed by the request
    assert later["state"] == "unknown" and later["unknown_reason"] == "source_expired"


def test_process_append_preserves_card_observation_and_replaces_own(home, sleeper):
    live = sleeper()
    (home / "processes.json").write_text(json.dumps([_entry("proc_a", live.pid)]))
    card = {"source": "kanban.card", "state": "running"}
    row = {"id": "s_own", "execution_observations": [card, {"source": "process.checkpoint", "state": "old"}]}
    psa.annotate_background_activity([row])
    sources = [v["source"] for v in row["execution_observations"]]
    assert sources == ["kanban.card", "process.checkpoint"] and row["execution_observations"][0] is card
    assert row["execution_observations"][1]["state"] == "running"
    (home / "processes.json").write_text("[]")
    psa._reset_cache_for_tests()
    psa.annotate_background_activity([row])
    assert row["execution_observations"] == [card]
