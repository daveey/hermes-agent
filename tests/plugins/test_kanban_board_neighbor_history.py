"""Graph invalidation must not scan an unchanged neighbour's histories."""
from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from plugins.kanban.dashboard import board_read as br
from tests.plugins.test_kanban_board_incremental import board as board  # noqa: PLC0414 -- pytest fixture reexport
from tests.plugins.test_kanban_board_delta import conn as conn  # noqa: PLC0414 -- pytest fixture reexport
from tests.plugins.test_kanban_board_incremental import _cold, _read, _stable


@pytest.mark.parametrize("sealed", [False, True])
def test_child_edit_refreshes_graph_not_parent_history(board, monkeypatch, sealed):
    conn = kbc.connect()
    try:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        child = kb.create_task(conn, title="child", assignee="worker")
        kb.link_tasks(conn, parent, child)
        for i in range(40):
            conn.execute("INSERT INTO task_events(task_id, kind, payload, created_at) VALUES (?, 'note', ?, ?)",
                         (parent, str(i), i))
        conn.commit()
        _read(board)
        if sealed:
            conn.execute("UPDATE tasks SET title='seal parent' WHERE id=?", (parent,))
            conn.commit()
            _read(board)
        scanned, graphs = [], []
        for name in ("_same_event_histories", "extend_histories"):
            real = getattr(br, name)

            def observe(c, histories, real=real):
                scanned.extend(histories)
                return real(c, histories)

            monkeypatch.setattr(br, name, observe)
        real_rows = br._rows_by_task

        def rows(c, table, ids):
            scanned.extend(ids)
            return real_rows(c, table, ids)

        monkeypatch.setattr(br, "_rows_by_task", rows)
        real_graph = kb.task_graph_contexts
        monkeypatch.setattr(kb, "task_graph_contexts",
                            lambda c, ids: graphs.extend(ids) or real_graph(c, ids))
        body = None
        for i in range(3):
            scanned.clear()
            graphs.clear()
            conn.execute("UPDATE tasks SET title=? WHERE id=?", (f"child {i}", child))
            conn.commit()
            body = _stable(_read(board))
            assert parent in graphs
            assert parent not in scanned, scanned
        assert body == _cold(board)
    finally:
        conn.close()


@pytest.mark.parametrize("mutation", ["update", "replace", "heartbeat", "delete"])
def test_own_history_mutations_still_reach_delta(conn, mutation):
    from tests.plugins.test_kanban_board_delta import Board, _same

    parent = kb.create_task(conn, title="parent", assignee="worker")
    child = kb.create_task(conn, title="child", assignee="worker")
    kb.link_tasks(conn, parent, child)
    for i in range(40):
        conn.execute("INSERT INTO task_events(task_id, kind, payload, created_at) VALUES (?, 'note', ?, ?)",
                     (parent, str(i), i))
    conn.commit()
    view = Board(conn)
    view.full()
    old = view.rows["diag_inputs"][1][parent]
    event_id = old[0]["id"]
    if mutation == "update":
        conn.execute("UPDATE task_events SET payload='new payload' WHERE id=?", (event_id,))
    elif mutation == "replace":
        conn.execute("INSERT OR REPLACE INTO task_events(id,task_id,kind,payload,created_at) "
                     "VALUES (?,?,'note','replacement',0)", (event_id, parent))
    elif mutation == "delete":
        conn.execute("DELETE FROM task_events WHERE id=?", (event_id,))
    else:
        conn.execute("INSERT INTO task_events(task_id,kind,payload,created_at) "
                     "VALUES (?,'heartbeat','{}',100)", (parent,))
    conn.execute("UPDATE tasks SET title='child changed too' WHERE id=?", (child,))
    conn.commit()
    view.refresh()
    fresh = view.rows["diag_inputs"][1][parent]
    assert [dict(r) for r in fresh] != [dict(r) for r in old]
    cold = view.cold()["diag_inputs"][1][parent]
    assert [dict(r) for r in fresh] == [dict(r) for r in cold]
    _same(view)
