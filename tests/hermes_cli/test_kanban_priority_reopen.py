"""Legacy terminal priorities are historical until reopened; migration fences membership."""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_operator import reopen_done_task
from hermes_cli import kanban_priority_migration as migration
from tests.hermes_cli import test_kanban_parent_first_dispatch as helpers

conn = helpers.conn


def test_reopen_bounds_ancestor_and_done_descendant_with_audit(conn):
    parent = helpers._ready(conn, "parent")
    child = helpers._ready(conn, "child")
    kb.link_tasks(conn, parent, child)
    for task in (parent, child):
        conn.execute("UPDATE tasks SET status='done', priority=10300 WHERE id=?", (task,))
    conn.commit()
    assert kb.get_task(conn, child).priority == 10300
    assert reopen_done_task(conn, parent, reason="retry", actor="operator") == "ready"
    for task in (parent, child):
        assert kb.get_task(conn, task).priority == 1000
        events = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='reprioritized'", (task,)).fetchall()
        assert any('10300' in row[0] and '1000' in row[0] for row in events)
    assert kb.get_task(conn, child).status == "todo"


@pytest.mark.parametrize("undo", [False, True])
@pytest.mark.parametrize("terminal", ["done", "archived"])
def test_migration_refuses_departed_open_rows_atomically(conn, tmp_path, undo, terminal):
    ids = [helpers._ready(conn, name) for name in ("one", "two")]
    for task in ids:
        kb.edit_task(conn, task, priority=500)
    path = tmp_path / "manifest.json"
    migration.save_manifest(path, migration.plan(conn))
    if undo:
        migration.apply(conn, path)
    before = [kb.get_task(conn, task).priority for task in ids]
    conn.execute("UPDATE tasks SET status=? WHERE id=?", (terminal, ids[1]))
    conn.commit()
    with pytest.raises(ValueError, match="open membership"):
        migration.apply(conn, path, undo=undo)
    assert [kb.get_task(conn, task).priority for task in ids] == before
