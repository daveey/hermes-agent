"""Shadow pulls must not rebuild the independent origin-ledger projection."""
import sqlite3

import pytest

from hermes_cli import kanban_session_activity as activity


@pytest.fixture
def stores(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ledger = tmp_path / "kanban" / "origin-ledger.db"
    ledger.parent.mkdir()
    board, shadow = tmp_path / "board.db", tmp_path / "shadow.db"
    connections = []
    for path in (board, shadow, ledger):
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT)")
        conn.execute("INSERT INTO tasks VALUES ('task', 'running')")
        conn.execute("CREATE TABLE kanban_sync_state (pulled INTEGER)")
        conn.execute("INSERT INTO kanban_sync_state VALUES (1)")
        conn.commit()
        connections.append(conn)
    monkeypatch.setattr(activity, "_board_db_paths", lambda: [board])
    monkeypatch.setattr(activity, "_replica_paths", lambda: [shadow])
    monkeypatch.setattr(activity, "_hub_db_path", lambda: None)
    monkeypatch.setattr(activity, "_origin_scope", lambda: ("gateway", "default"))
    # Isolate projection work, not its invalidation: read the two actual input
    # databases on every rebuild and publish their current values.
    calls = []

    def project():
        calls.append(True)
        result = []
        for path in (board, ledger):
            with sqlite3.connect(path) as conn:
                result.extend({"status": row[0]} for row in conn.execute("SELECT status FROM tasks"))
        return result

    monkeypatch.setattr(activity, "_project_remote_links", project)
    activity._reset_cache_for_tests()
    yield connections, calls
    activity._reset_cache_for_tests()
    for conn in connections:
        conn.close()


def test_shadow_commits_refresh_counts_without_reprojecting_ledger(stores):
    (_, shadow, _), calls = stores
    first = activity._remote_links()
    counts_key = activity._activity_fingerprint()
    assert activity._remote_links() == first
    assert len(calls) == 1
    for sql in ("UPDATE kanban_sync_state SET pulled=2",
                "UPDATE tasks SET status='done'", "DELETE FROM tasks"):
        shadow.execute(sql)
        shadow.commit()
        new_counts_key = activity._activity_fingerprint()
        assert new_counts_key != counts_key  # retain immediate replica freshness
        counts_key = new_counts_key
        assert activity._remote_links() == first
        assert len(calls) == 1


@pytest.mark.parametrize("input_index", [0, 2], ids=["local-board", "origin-ledger"])
def test_projection_inputs_invalidate_on_commit_and_delete_not_rollback(stores, input_index):
    connections, calls = stores
    writer = connections[input_index]
    first = activity._remote_links()
    writer.execute("UPDATE tasks SET status='done'")
    writer.rollback()
    assert activity._remote_links() == first
    assert len(calls) == 1
    writer.execute("UPDATE tasks SET status='done'")
    writer.commit()
    assert sorted(row["status"] for row in activity._remote_links()) == ["done", "running"]
    assert len(calls) == 2
    writer.execute("DELETE FROM tasks")
    writer.commit()
    assert activity._remote_links() == [{"status": "running"}]
    assert len(calls) == 3
