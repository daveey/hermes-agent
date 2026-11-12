"""Cross-gateway priority must survive pickup into a partial local board."""
import sqlite3

import pytest

from hermes_cli import kanban_dispatch_priority as priority

from hermes_cli import kanban_db as kb
from tests.hermes_cli import test_kanban_worker_node as support

svc = support.svc
gws = support.gws


@pytest.mark.parametrize("inherited", [False, True])
def test_foreign_higher_priority_gets_next_slot(gws, inherited):
    home, executor = gws
    home.enter()
    foreign = kb.create_task(home.conn, title="foreign prerequisite", assignee="default",
                             priority=0 if inherited else 900, runs_on=[f"machine:{support.WORKER}"])
    if inherited:
        kb.create_task(home.conn, title="urgent foreign waiter", assignee="default", priority=900,
                       parents=[foreign], runs_on=[f"machine:{support.HUB}"])
    home.round()
    executor.enter()
    local = kb.create_task(executor.conn, title="local background work", assignee="default", priority=100)
    executor.round()
    executor.tick(max_spawn=1)
    assert kb.get_task(executor.conn, foreign).status == "running"
    assert kb.get_task(executor.conn, local).status == "ready"
    assert kb.get_task(executor.conn, foreign).priority == (0 if inherited else 900)


@pytest.fixture
def graph(tmp_path, monkeypatch):
    from hermes_cli import kanban_worker_node as wn
    from hermes_cli import kanban_db_sync as sync

    local = sqlite3.connect(":memory:")
    replica = tmp_path / "replica.db"
    remote = sqlite3.connect(replica)
    schema = """
        CREATE TABLE tasks (id TEXT PRIMARY KEY, priority INTEGER, status TEXT, created_at INTEGER);
        CREATE TABLE task_links (parent_id TEXT, child_id TEXT);
    """
    for conn in (local, remote):
        conn.row_factory = sqlite3.Row
        conn.executescript(schema)
    local.executemany("INSERT INTO tasks VALUES (?, ?, 'ready', ?)", [("local", 100, 1), ("foreign", 0, 2)])
    remote.executemany("INSERT INTO tasks VALUES (?, ?, ?, ?)", [
        ("foreign", 0, "ready", 2), ("waiter", 900, "todo", 3)])
    remote.execute("INSERT INTO task_links VALUES ('foreign', 'waiter')")
    remote.commit()
    monkeypatch.setattr(sync, "board_mode", lambda conn: "primary")

    def open_replica(conn):
        copy = sqlite3.connect(replica)
        copy.row_factory = sqlite3.Row
        return copy

    monkeypatch.setattr(wn, "open_replica", open_replica)
    yield local, remote
    local.close()
    remote.close()


def _order(conn):
    rows = conn.execute("SELECT id FROM tasks WHERE status='ready' ORDER BY priority DESC").fetchall()
    return [r["id"] for r in priority.order_with_remote_waiters(conn, rows)]


def test_remote_donation_is_derived_and_has_no_writes(graph):
    local, remote = graph
    before = local.total_changes
    assert _order(local) == ["foreign", "local"]
    assert local.total_changes == before
    assert local.execute("SELECT priority FROM tasks WHERE id='foreign'").fetchone()[0] == 0
    assert local.execute("SELECT count(*) FROM tasks").fetchone()[0] == 2
    remote.execute("UPDATE tasks SET priority=50 WHERE id='waiter'")
    remote.commit()
    assert _order(local) == ["local", "foreign"]


@pytest.mark.parametrize("status", ["done", "archived"])
def test_local_terminal_waiter_overrides_stale_remote_donation(graph, status):
    local, _ = graph
    local.execute("INSERT INTO tasks VALUES ('waiter', 900, ?, 3)", (status,))
    local.execute("INSERT INTO task_links VALUES ('foreign', 'waiter')")
    assert _order(local) == ["local", "foreign"]


def test_local_unlink_overrides_replica_edge_and_local_link_is_used(graph):
    local, _ = graph
    local.execute("INSERT INTO tasks VALUES ('waiter', 950, 'todo', 3)")
    assert _order(local) == ["local", "foreign"]  # no local edge = already unlinked
    local.execute("INSERT INTO task_links VALUES ('foreign', 'waiter')")
    assert _order(local) == ["foreign", "local"]


def test_transitive_donation_cycle_missing_and_terminal_cutoff(graph):
    local, remote = graph
    remote.execute("UPDATE tasks SET priority=0 WHERE id='waiter'")
    remote.execute("INSERT INTO tasks VALUES ('grandchild', 950, 'blocked', 4)")
    remote.executemany("INSERT INTO task_links VALUES (?, ?)", [
        ("waiter", "grandchild"), ("grandchild", "waiter"), ("foreign", "missing")])
    remote.commit()
    assert _order(local) == ["foreign", "local"]
    remote.execute("UPDATE tasks SET status='done' WHERE id='waiter'")
    remote.commit()
    assert _order(local) == ["local", "foreign"]


def test_priority_cap_waiter_tie_and_creation_order(graph):
    local, remote = graph
    local.execute("UPDATE tasks SET priority=1000 WHERE id='local'")
    remote.execute("UPDATE tasks SET priority=9000 WHERE id='waiter'")
    remote.commit()
    assert _order(local) == ["foreign", "local"]  # equal capped priority, open waiter wins
    local.execute("INSERT INTO tasks VALUES ('local-waiter', 1500, 'todo', 4)")
    local.execute("INSERT INTO task_links VALUES ('local', 'local-waiter')")
    assert _order(local) == ["local", "foreign"]  # both capped and waited-on: older first


def test_local_priority_edit_wins_over_replica(graph):
    local, remote = graph
    remote.execute("UPDATE tasks SET priority=0 WHERE id='waiter'")
    remote.execute("UPDATE tasks SET priority=900 WHERE id='foreign'")
    remote.commit()
    assert _order(local) == ["local", "foreign"]


@pytest.mark.parametrize("mode", [None, "mirror"])
def test_non_primary_does_not_open_replica(graph, monkeypatch, mode):
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_worker_node as wn

    local, _ = graph
    monkeypatch.setattr(sync, "board_mode", lambda conn: mode)
    monkeypatch.setattr(wn, "open_replica", lambda conn: pytest.fail("must not read replica"))
    assert _order(local) == ["local", "foreign"]


def test_no_replica_empty_lane_and_unreadable_replica_fall_back(graph, monkeypatch):
    from hermes_cli import kanban_worker_node as wn

    local, _ = graph
    monkeypatch.setattr(wn, "open_replica", lambda conn: None)
    assert _order(local) == ["local", "foreign"]
    monkeypatch.setattr(wn, "open_replica", lambda conn: pytest.fail("empty lane must not open"))
    assert priority.order_with_remote_waiters(local, []) == []
    empty = sqlite3.connect(":memory:")
    monkeypatch.setattr(wn, "open_replica", lambda conn: empty)
    assert _order(local) == ["local", "foreign"]
    with pytest.raises(sqlite3.ProgrammingError):
        empty.execute("SELECT 1")  # failure still closes replica


@pytest.mark.parametrize("error", [sqlite3.OperationalError("lifecycle busy"), OSError("rotated"),
                                    RuntimeError("unavailable")])
def test_replica_open_failure_preserves_local_order(graph, monkeypatch, error):
    from hermes_cli import kanban_worker_node as wn

    local, _ = graph

    def failed(conn):
        raise error

    monkeypatch.setattr(wn, "open_replica", failed)
    assert _order(local) == ["local", "foreign"]


def test_dispatch_still_claims_local_when_replica_open_fails(gws, monkeypatch):
    from hermes_cli import kanban_worker_node as wn

    _, executor = gws
    executor.enter()
    local = kb.create_task(executor.conn, title="local work", assignee="default")

    def failed(conn):
        raise sqlite3.OperationalError("shadow lifecycle busy")

    monkeypatch.setattr(wn, "open_replica", failed)
    executor.tick(max_spawn=1)
    assert kb.get_task(executor.conn, local).status == "running"


def test_malformed_replica_priority_preserves_local_order(graph):
    local, remote = graph
    remote.execute("UPDATE tasks SET priority='invalid' WHERE id='waiter'")
    remote.commit()
    assert _order(local) == ["local", "foreign"]
