"""Blocked-upstream presentation survives both remote card transports."""
import json
import sqlite3

import pytest

from hermes_cli import kanban_chat_links as links
from hermes_cli import kanban_origin_ledger as ledger
from hermes_cli import kanban_replica_projection as projection
from hermes_cli import kanban_session_activity as activity
from tests.hermes_cli import test_kanban_chat_links as fixtures
from tests.hermes_cli import test_kanban_origin_ledger as origin_fixtures
from tests.hermes_cli import kanban_sync_support as support
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

world = origin_fixtures.world


@pytest.mark.parametrize("present", [True, False])
def test_origin_ledger_projection_keeps_optional_ancestor(world, present):
    path, replica, binding = world
    expected = {"id": "parent", "status": "blocked"}
    origin_fixtures.publish(replica, binding, **({"blocked_upstream": expected} if present else {}))
    origin_fixtures.accept(world)
    row = ledger.project(path, replica=replica, gateway="origin", profile="default")[0]
    if present:
        assert row["blocked_upstream"] == expected
    else:
        assert "blocked_upstream" not in row


@pytest.mark.parametrize("status", ["blocked", "triage", "running"])
def test_owner_publication_stamps_current_ancestor(tmp_path, monkeypatch, status):
    home = support.kanban_home(tmp_path, monkeypatch)
    conn = kbc.connect(home / "kanban.db")
    try:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        child = kb.create_task(conn, title="child", assignee="worker")
        with kbc.write_txn(conn):
            conn.execute("UPDATE tasks SET status='todo' WHERE id=?", (child,))
            conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, parent))
            conn.execute("INSERT INTO task_links(parent_id, child_id) VALUES (?, ?)", (parent, child))
            task = ledger.observe_owner_task(conn, {"id": child, "status": "todo",
                                                   "blocked_upstream": {"id": "obsolete", "status": "blocked"}}, now=1000)
        if status == "running":
            assert "blocked_upstream" not in task
        else:
            assert task["blocked_upstream"] == {"id": parent, "status": status}
        assert ledger._owner_evidence(task)["task_hash"] == task["origin_observation"]["task_hash"]
    finally:
        conn.close()


@pytest.mark.parametrize("status", ["todo", "done", "archived", "blocked", "triage"])
def test_replica_stamp_precedence_and_ineligible_cards(status):
    from hermes_cli.kanban_replica_upstream import enrich

    with sqlite3.connect(":memory:") as conn:
        conn.executescript("CREATE TABLE tasks(id TEXT, status TEXT);"
                           "CREATE TABLE task_links(parent_id TEXT, child_id TEXT);"
                           "INSERT INTO tasks VALUES ('graph-parent', 'triage');"
                           "INSERT INTO task_links VALUES ('graph-parent', 'child');")
        conn.execute("INSERT INTO tasks VALUES ('child', ?)", (status,))
        stamp = {"id": "owner-parent", "status": "blocked"}
        row = {"id": "child", "status": status, "blocked_upstream": json.dumps(stamp)}
        result = enrich(conn, [row])[0]
    if status == "todo":
        assert result["blocked_upstream"] == stamp
    else:
        assert "blocked_upstream" not in result


@pytest.mark.parametrize("row_factory", [None, sqlite3.Row])
@pytest.mark.parametrize("with_episode", [False, True])
def test_task_info_supports_caller_row_factory(row_factory, with_episode):
    from hermes_cli.kanban_session_activity_card_details import _task_info

    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = row_factory
        conn.execute("CREATE TABLE tasks(id TEXT, status TEXT)")
        conn.execute("INSERT INTO tasks VALUES ('parent', 'triage')")
        if with_episode:
            conn.execute("ALTER TABLE tasks ADD COLUMN containment_episode TEXT")
            conn.execute("UPDATE tasks SET containment_episode='episode'")
        assert _task_info(conn, None, "parent") == {
            "id": "parent", "status": "triage", "episode": "episode" if with_episode else None,
        }
        assert conn.row_factory is row_factory


@pytest.mark.parametrize("transport", ["stamped", "graph", "old"])
def test_replica_session_tasks_preserves_optional_ancestor(tmp_path, monkeypatch, transport):
    path = tmp_path / "replica.db"
    fixtures._replica(path, [("child", "child", "todo", fixtures.OTHER)],
                      [("child", fixtures.ME, "s1", "created", 0)])
    expected = {"id": "parent", "status": "blocked"}
    with sqlite3.connect(path) as conn:
        if transport == "stamped":
            conn.execute("ALTER TABLE tasks ADD COLUMN blocked_upstream TEXT")
            conn.execute("UPDATE tasks SET blocked_upstream=?", (json.dumps(expected),))
        elif transport == "graph":
            conn.execute("CREATE TABLE task_links(parent_id TEXT, child_id TEXT)")
            conn.execute("INSERT INTO tasks(id, status) VALUES ('parent', 'blocked')")
            conn.execute("INSERT INTO task_links VALUES ('parent', 'child')")
    monkeypatch.setattr(activity, "_remote_links", lambda **kw: [])
    monkeypatch.setattr(activity, "_board_db_paths", list)
    monkeypatch.setattr(activity, "_remote_cards", lambda ids, statuses, stale_ok: links.replica_cards(
        ids, fixtures.ME, statuses, replicas=[("default", path)]))
    links._reset_for_tests()
    rows = activity.session_tasks(["s1"])
    assert len(rows) == 1 and rows[0]["source"] == "replica"
    if transport == "old":
        assert "blocked_upstream" not in rows[0]
    else:
        assert rows[0]["blocked_upstream"] == expected


@pytest.mark.parametrize("present", [True, False])
def test_hub_inventory_projection_carries_optional_field(present):
    task = {"id": "child", "status": "todo"}
    expected = {"id": "parent", "status": "blocked"}
    if present:
        task["blocked_upstream"] = expected
    rows = projection.card_rows(("remote", "default", "default", "child"), task,
                               sids=["s1"], closed=False, synced=1, stale=False, me="local",
                               origin_profile="default", legacy_record=None, historical={})
    row = rows[0][1]
    if present:
        assert row["blocked_upstream"] == expected
    else:
        assert "blocked_upstream" not in row
