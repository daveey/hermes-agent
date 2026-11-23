"""Real SQLite producer and existing sidebar annotation entry paths."""
import json
import sqlite3
from types import SimpleNamespace

import pytest

from hermes_cli import subagent_session_activity as activity
from hermes_cli.execution_observations import identity_key, select
from hermes_cli.turn_process_execution_observations import registry_subagent
from hermes_state import SessionDB


@pytest.fixture
def stored(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    db = SessionDB(home / "state.db")
    db.create_session("parent", source="cli")
    db.create_session("child", source="subagent", parent_session_id="parent")
    now = 200000.0
    with sqlite3.connect(home / "state.db") as conn:
        conn.execute("UPDATE sessions SET started_at=?, last_activity_at=? WHERE id='child'", (now - 100, now))
    monkeypatch.setattr(activity.time, "time", lambda: now + 5)
    monkeypatch.setattr(activity, "local_scope", lambda: ("edge", "default"))
    activity._reset_cache_for_tests()
    yield SimpleNamespace(home=home, db=db, now=now)
    db.close()
    activity._reset_cache_for_tests()


def _observations(rows):
    return [o for o in rows[0].get("execution_observations", []) if o.get("source") == "subagent.activity"]


def test_real_registry_and_sqlite_records_are_byte_identical_and_merge(stored):
    child = SimpleNamespace(session_id="child", _parent_session_id="parent",
                            get_activity_summary=lambda: {"last_activity_ts": stored.now})
    registry = registry_subagent({"agent": child, "owner_agent_session_id": "obsolete-owner",
                                  "subagent_id": "registry-only", "delegation_id": "batch",
                                  "started_at": stored.now - 90, "status": "running"},
                                 gateway="edge", profile="default", now=stored.now + 5)
    (persisted,) = _observations(activity.annotate_subagent_activity([{"id": "parent"}]))
    assert json.dumps(registry, sort_keys=True) == json.dumps(persisted, sort_keys=True)
    assert identity_key(registry["identity"]) == identity_key(persisted["identity"])
    assert select([registry, persisted], identity=registry["identity"], now=stored.now + 5)["state"] == "running"
    assert registry["identity"]["generation"] == "session=child"
    assert registry["last_progress"] is None


@pytest.mark.parametrize("age,state", [(5, "running"), (170, "running"), (181, "unknown")])
def test_wedged_child_ages_without_refetch(stored, monkeypatch, age, state):
    activity.annotate_subagent_activity([{"id": "parent"}])
    monkeypatch.setattr(activity.time, "time", lambda: stored.now + age)
    (obs,) = _observations(activity.annotate_subagent_activity([{"id": "tip", "_lineage_ids": ["parent"]}]))
    assert obs["state"] == state
    assert obs["observed_at"] == stored.now
    if state == "unknown":
        assert obs["unknown_reason"] == "source_expired"


def test_finished_children_absent_and_profile_ids_never_alias(stored):
    path = stored.home / "profiles" / "work" / "state.db"
    path.parent.mkdir(parents=True)
    other = SessionDB(path)
    other.create_session("parent", source="cli")
    other.create_session("child", source="subagent", parent_session_id="parent")
    other.touch_session_activity("child", stored.now)
    default, work = activity.annotate_subagent_activity([{"id": "parent"}, {"id": "parent", "profile": "work"}])
    a, b = default["execution_observations"][0], work["execution_observations"][0]
    assert identity_key(a["identity"]) != identity_key(b["identity"])
    stored.db.end_session("child", "completed")
    activity._reset_cache_for_tests()
    assert not _observations(activity.annotate_subagent_activity([{"id": "parent"}]))
    other.close()


def test_read_only_batched_cache_and_no_wal_growth(stored, monkeypatch):
    real = sqlite3.connect
    queries = []
    modes = []

    def connect(database, **kwargs):
        modes.append(database)
        conn = real(database, **kwargs)
        conn.set_trace_callback(queries.append)
        return conn

    monkeypatch.setattr(activity.sqlite3, "connect", connect)
    wal = stored.home / "state.db-wal"
    before = wal.stat().st_size if wal.exists() else 0
    activity.annotate_subagent_activity([{"id": "parent"}] * 600)
    activity.annotate_subagent_activity([{"id": "parent"}])
    assert len([q for q in queries if q.startswith("SELECT")]) == 1
    assert all(uri.endswith("?mode=ro") for uri in modes)
    assert (wal.stat().st_size if wal.exists() else 0) == before


def test_failed_and_missing_stores_are_unknown_not_empty(stored):
    row = activity.annotate_subagent_activity([{"id": "parent"}])[0]
    activity._reset_cache_for_tests()
    # Closed/unavailable store, with retained source clocks from the previous read.
    stored.db.close()
    (stored.home / "state.db").rename(stored.home / "gone.db")
    activity.annotate_subagent_activity([row])
    observations = row["execution_observations"]
    assert any(o.get("unknown_reason") == "subagent_store_unavailable" for o in observations)
    assert any(o.get("observed_at") == stored.now for o in observations)
    assert not (stored.home / "state.db").exists()


def test_sidebar_stamps_real_entry_path(stored, monkeypatch):
    from hermes_cli.web_routers import profiles

    monkeypatch.setattr(profiles, "_cron_profile_home", lambda name: (name, stored.home))
    monkeypatch.setattr(profiles, "_read_profile_db", lambda name, home, errors, read: read(stored.db))
    response = profiles.get_profiles_session_stamps(ids="parent", profile="default")
    (obs,) = _observations(response["sessions"])
    assert obs["state"] == "running" and obs["identity"]["work_id"] == "child"


def test_project_tree_real_annotate_entry_path(stored):
    from tui_gateway.methods_projects import _annotate_tree_activity

    rows = _annotate_tree_activity([{"id": "parent", "profile": "default"}])
    (obs,) = _observations(rows)
    assert obs["state"] == "running" and obs["identity"]["session"] == "parent"


def test_session_list_real_entry_path(stored, monkeypatch):
    from hermes_cli.web_routers import sessions
    from hermes_cli.session_list_flight import SESSION_LIST_CACHE

    monkeypatch.setattr(sessions, "_session_db_path_for_profile", lambda profile: stored.home / "state.db")
    monkeypatch.setattr(sessions, "_open_session_db_for_profile",
                        lambda profile, read_only: SessionDB(stored.home / "state.db", read_only=True))
    SESSION_LIST_CACHE.clear()
    response = sessions.get_sessions(limit=20, offset=0, min_messages=0, archived="exclude", order="recent",
                                     source=None, sources=None, exclude_sources="cron", cwd_prefix=None,
                                     full=False, profile=None)
    parent = next(row for row in response["sessions"] if row["id"] == "parent")
    assert _observations([parent])[0]["identity"]["work_id"] == "child"
    SESSION_LIST_CACHE.clear()


def test_managed_worker_owner_reserved_child_and_activity_receipt_are_visible(stored):
    import hermes_state_runtime as runtime
    from tests.hermes_state import test_runtime_worker_children as children

    db = stored.db
    epoch = runtime.begin_runtime_epoch(db, instance_id="boot")
    runtime.register_worker_execution(db, epoch=epoch, execution_id="worker", session_id="parent", generation=0,
                                      kind="compute", adoption_secret="fixture-secret")
    child = children._reserve(db, epoch, 1)["child"]
    runtime.mutate_worker_execution(db, epoch=epoch, execution_id=child["execution_id"],
                                    session_id=child["session_id"], generation=0, sequence=1,
                                    operation="session.activity", payload={"ts": stored.now, "description": "tool", "provenance": None})
    activity._reset_cache_for_tests()
    observations = _observations(activity.annotate_subagent_activity([{"id": "parent"}]))
    managed = next(o for o in observations if o["identity"]["work_id"] == child["session_id"])
    assert managed["state"] == "running" and managed["observed_at"] == stored.now
    assert managed["identity"]["generation"] == "session=" + child["session_id"]


def test_legacy_registry_missing_child_id_and_invalid_clocks_are_unknown():
    record = {"subagent_id": "legacy", "owner_agent_session_id": "parent", "started_at": 1000, "status": "running"}
    observation = registry_subagent(record, gateway="edge", profile="default", now=1005)
    assert observation["state"] == "unknown"
    assert observation["identity"].get("work_id") is None
    from hermes_cli.turn_process_execution_observations import subagent
    for clock in (None, float("nan"), float("inf"), 9000, "1000", True):
        obs = subagent({"child_session_id": "child", "parent_session_id": "parent", "activity_at": clock,
                        "live": True}, gateway="edge", profile="default", now=1005)
        assert obs["state"] == "unknown" and obs["observed_at"] is None


@pytest.mark.parametrize("endpoint", ["/api/profiles/sessions", "/api/profiles/sessions/sidebar"])
def test_profile_lists_http_entry_paths(stored, monkeypatch, endpoint):
    from fastapi.testclient import TestClient
    from hermes_cli import profiles as profile_config
    from hermes_cli.web_routers import profiles
    from hermes_cli.web_server import _SESSION_HEADER_NAME, _SESSION_TOKEN, app

    (stored.home / "config.yaml").write_text("{}\n")
    stored.db.append_message("parent", role="user", content="hello")
    monkeypatch.setattr(profile_config, "_get_default_hermes_home", lambda: stored.home)
    monkeypatch.setattr(profile_config, "_get_profiles_root", lambda: stored.home / "profiles")
    monkeypatch.setattr(profiles, "_SIDEBAR_CACHE_TTL_SECONDS", 0.0)
    with TestClient(app) as client:
        client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
        from tests.waiting import wait_until

        def ready():
            nonlocal response
            response = client.get(endpoint, params={"profile": "default", "recents_profile": "default"})
            # A new selector is built off-request; never accept another failure.
            assert response.status_code in (200, 503), response.text
            if response.status_code == 503:
                assert response.json() == {"detail": "sessions projection unavailable"}
            return response.status_code == 200

        response = client.get(endpoint, params={"profile": "default", "recents_profile": "default"})
        assert wait_until(ready, timeout=10), response.text
        assert response.status_code == 200, response.text
        data = response.json()
        rows = data["recents"]["sessions"] if endpoint.endswith("sidebar") else data["sessions"]
        parent = next(row for row in rows if row["id"] == "parent")
        (obs,) = _observations([parent])
        assert obs["state"] == "running" and obs["identity"]["work_id"] == "child"
