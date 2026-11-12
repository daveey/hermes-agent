"""Ready-episode identity fences for imported dispatcher observations."""
import sqlite3

import pytest

from hermes_cli import kanban_dependency_history as history
from hermes_cli import kanban_observed_pr_hold as observed


@pytest.fixture
def stores():
    connections = [sqlite3.connect(":memory:") for _ in range(2)]
    for conn in connections:
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, "
                     "kind TEXT, created_at TEXT, payload TEXT, uid TEXT)")
        conn.execute("INSERT INTO task_events VALUES (1,'task','promoted',"
                     "'2026-01-01T00:00:00Z','{}','ready-episode')")
    yield connections
    for conn in connections:
        conn.close()


@pytest.mark.parametrize("case", ["matching", "different", "missing_uid", "no_hold", "consumed", "invalid_clock"])
def test_fresh_replica_must_match_home_episode(stores, monkeypatch, case):
    home, replica = stores
    if case != "no_hold":
        replica.execute("INSERT INTO task_events VALUES (2,'task','respawn_guarded',"
                        "'2026-01-01T00:00:01Z','{\"reason\":\"active_pr\"}','hold')")
    mutations = {
        "different": (replica, "UPDATE task_events SET uid='other-episode' WHERE id=1"),
        "missing_uid": (home, "UPDATE task_events SET uid=NULL WHERE id=1"),
        "consumed": (replica, "INSERT INTO task_events VALUES (3,'task','claimed',"
                     "'2026-01-01T00:00:02Z','{}','claim')"),
        "invalid_clock": (replica, "UPDATE task_events SET created_at='invalid' WHERE id=2"),
    }
    target, sql = mutations.get(case, (replica, "SELECT 1"))
    target.execute(sql)
    monkeypatch.setattr(history, "_with_fresh_replica", lambda conn, since, read, fallback: read(replica))
    assert observed.observed(home, "task") is (case == "matching")


@pytest.mark.parametrize("case", ["no_boundary", "bad_clock", "diagnostic", "wrong_reason", "before_boundary"])
def test_local_observation_requires_ready_boundary(stores, monkeypatch, case):
    home, _ = stores
    home.execute("INSERT INTO task_events VALUES (2,'task','respawn_guarded',"
                 "'2026-01-01T00:00:01Z','{\"reason\":\"active_pr\"}','hold')")
    mutations = {
        "no_boundary": "DELETE FROM task_events WHERE id=1",
        "bad_clock": "UPDATE task_events SET created_at='invalid' WHERE id=2",
        "diagnostic": "UPDATE task_events SET kind='commented' WHERE id=2",
        "wrong_reason": "UPDATE task_events SET payload='{\"reason\":\"other\"}' WHERE id=2",
        "before_boundary": "UPDATE task_events SET created_at='2025-01-01T00:00:00Z' WHERE id=2",
    }
    home.execute(mutations[case])
    monkeypatch.setattr(history, "_with_fresh_replica", lambda *args: False)
    assert not observed.observed(home, "task")
