"""Effective priority follows active dependency edges, without rewriting explicit priority."""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_dispatch_queries import lane_rows
from tests.hermes_cli import test_kanban_parent_first_dispatch as helpers

conn = helpers.conn


def order(conn):
    return [row["id"] for row in lane_rows(conn, "ready")]


def test_two_level_priority_and_raise(conn):
    leaf = helpers._ready(conn, "ordinary", priority=100)
    root = helpers._ready(conn, "prerequisite")
    middle = kb.create_task(conn, title="middle", assignee="default", parents=[root])
    child = kb.create_task(conn, title="delivery", assignee="default", priority=240, parents=[middle])
    assert order(conn) == [root, leaf]
    kb.edit_task(conn, leaf, priority=300)
    assert order(conn) == [leaf, root]
    kb.edit_task(conn, child, priority=400)
    assert order(conn) == [root, leaf]
    assert kb.get_task(conn, root).priority == 0
    assert kb.get_task(conn, middle).priority == 0


@pytest.mark.parametrize("status", ["todo", "running", "scheduled", "blocked", "review", "ready"])
def test_link_inherits_before_and_after_park(conn, status):
    leaf = helpers._ready(conn, "ordinary", priority=100)
    root = helpers._ready(conn, "prerequisite")
    child = helpers._ready(conn, "delivery", priority=240)
    kb.link_tasks(conn, root, child)
    conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, child))
    conn.commit()
    result = order(conn)
    assert result.index(root) < result.index(leaf)


@pytest.mark.parametrize("status", ["done", "archived"])
@pytest.mark.parametrize("depth", [1, 2])
def test_terminal_intermediate_stops_inheritance(conn, status, depth):
    leaf = helpers._ready(conn, "ordinary", priority=100)
    root = helpers._ready(conn, "prerequisite")
    middle = kb.create_task(conn, title="middle", assignee="default", parents=[root])
    if depth == 2:
        middle = kb.create_task(conn, title="deeper", assignee="default", parents=[middle])
    kb.create_task(conn, title="delivery", assignee="default", priority=240, parents=[middle])
    conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, middle))
    conn.commit()
    assert order(conn) == [leaf, root]


def test_higher_parent_and_unlink(conn):
    leaf = helpers._ready(conn, "ordinary", priority=300)
    root = helpers._ready(conn, "prerequisite", priority=400)
    child = kb.create_task(conn, title="delivery", assignee="default", priority=240, parents=[root])
    assert order(conn) == [root, leaf]
    kb.edit_task(conn, child, priority=500)
    kb.edit_task(conn, leaf, priority=450)
    assert order(conn) == [root, leaf]
    kb.unlink_tasks(conn, root, child)
    assert order(conn).index(leaf) < order(conn).index(root)


def test_transitive_consumer_parity(conn):
    from hermes_cli import kanban_queue_position as queue
    from hermes_cli import kanban_worker_node as worker

    leaf = helpers._ready(conn, "ordinary", priority=100)
    root = helpers._ready(conn, "prerequisite")
    middle = kb.create_task(conn, title="middle", assignee="default", parents=[root])
    kb.create_task(conn, title="delivery", assignee="default", priority=240, parents=[middle])
    assert [t.id for t in kb.list_tasks(conn, status="ready")] == [root, leaf]
    assert queue._ahead(conn, kb.get_task(conn, root)) == 0
    have = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    for col, ddl in (("created_on", "TEXT"), ("lease_holder", "TEXT"), ("lease_expires", "INTEGER")):
        if col not in have:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {col} {ddl}")
    conn.execute("UPDATE tasks SET created_on='other-gw'")
    conn.commit()
    assert [r["id"] for r in worker.candidates(conn, "me")] == [root, leaf]


def test_highest_of_multiple_waiters(conn):
    leaf = helpers._ready(conn, "ordinary", priority=200)
    root = helpers._ready(conn, "prerequisite", priority=-10)
    kb.create_task(conn, title="low delivery", assignee="default", priority=50, parents=[root])
    kb.create_task(conn, title="high delivery", assignee="default", priority=240, parents=[root])
    assert order(conn) == [root, leaf]


def test_negative_explicit_priority_not_clamped(conn):
    first = helpers._ready(conn, "first", priority=-10)
    second = helpers._ready(conn, "second", priority=-5)
    assert order(conn) == [second, first]


def test_cycle_in_imported_edges_terminates(conn):
    root = helpers._ready(conn, "prerequisite")
    middle = kb.create_task(conn, title="middle", assignee="default", priority=240, parents=[root])
    conn.execute("INSERT INTO task_links(parent_id, child_id) VALUES (?, ?)", (middle, root))
    conn.commit()
    # An accidental UNION ALL must fail promptly rather than hang the test worker.
    steps = 0

    def budget():
        nonlocal steps
        steps += 1
        return int(steps > 1000)

    conn.set_progress_handler(budget, 100)
    try:
        assert order(conn) == [root]
    finally:
        conn.set_progress_handler(None, 0)
