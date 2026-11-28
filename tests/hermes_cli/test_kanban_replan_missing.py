"""An incomplete replica never licenses a duplicate replan parent."""
from hermes_cli import kanban_auto_replan as replan
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from tests.hermes_cli import test_kanban_auto_replan as helpers

kanban_home = helpers.kanban_home


def test_unobserved_parent_is_not_cancelled(kanban_home, monkeypatch):
    with kbc.connect_closing() as conn:
        sync.apply_sync_schema_steps(conn)
        conn.commit()
        tid = helpers._card(conn)
        parent = replan.hold(conn, tid)
        assert kb.delete_task(conn, parent)
        # Model a partial file: no local row, replica row or authenticated deletion.
        conn.execute("DELETE FROM kanban_sync_tombstones WHERE table_name='tasks' AND row_uid=?", (parent,))
        conn.commit()
        monkeypatch.setattr(sync, 'sync_enabled', lambda: True)
        assert replan._parent_status(conn, parent) == 'unknown'
        current = replan.state(conn, tid)
        assert current is not None and current.parent == parent
        assert replan.hold(conn, tid) == parent
        assert replan.claim_refused(conn, tid)
        assert len(helpers.helpers._events(conn, tid, replan.REQUESTED)) == 1
        # A positive deletion receipt, unlike absence, permits replacement.
        conn.execute("INSERT INTO kanban_sync_tombstones(table_name,row_uid,deleted_at) VALUES ('tasks',?,1)", (parent,))
        conn.commit()
        replacement = replan.hold(conn, tid)
        assert replacement and replacement != parent
        assert kb.parent_ids(conn, tid) == [replacement]
