"""Backfill worker ownership without replaying historical events. [daveey]"""
import json

import pytest

from hermes_cli import kanban_chat_links as links
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as notify
from hermes_cli import kanban_sync_client as sync
from tests.hermes_cli import kanban_sync_support as support

WORKER = "local-" + "cd" * 32


@pytest.mark.parametrize("session", [WORKER, "kanban-placement-old"])
def test_reconcile_backfills_worker_created_links_without_replay(tmp_path, monkeypatch, session):
    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(home, True)
    with kbc.connect_closing(home / "kanban.db") as conn:
        sync.ensure_identity(conn, "default", "here")
        parent = kb.create_task(conn, title="parent", session_id="human")
        notify.add_notify_sub(conn, task_id=parent, platform="tui", chat_id="human", delivery_mode="wake")
        links.reconcile(conn, "default", "here", ledger=False)
        child = kb.create_task(conn, title="legacy", session_id=session)
        with kb.write_txn(conn):
            conn.execute("UPDATE task_events SET payload=? WHERE task_id=? AND kind='created'",
                         (json.dumps({"creator_task_id": parent}), child))
            kb._append_event(conn, child, "completed", {"summary": "old result"})
            conn.execute("INSERT INTO task_chat_links (uid,task_id,chat_gateway,chat_session,kind,released,created_at) "
                         "VALUES (?,?,'here',?,'created',0,1)",
                         (links.link_uid(child, "here", None, WORKER, "created"), child, WORKER))
        links.reconcile(conn, "default", "here", ledger=False)
        assert [tuple(r) for r in conn.execute(
            "SELECT chat_session,kind FROM task_chat_links WHERE task_id=? AND released=0", (child,))] == [
                ("human", "created")]
        subs = notify.list_notify_subs(conn, child)
        assert len(subs) == 1
        latest = conn.execute("SELECT MAX(id) FROM task_events WHERE task_id=?", (child,)).fetchone()[0]
        assert subs[0]["last_event_id"] == latest
        assert notify.unseen_events_for_sub(conn, task_id=child, platform="tui", chat_id="human")[1] == []
        links.reconcile(conn, "default", "here", ledger=False)
        assert notify.list_notify_subs(conn, child)[0]["last_event_id"] == latest
        assert conn.execute("SELECT MAX(id) FROM task_events WHERE task_id=?", (child,)).fetchone()[0] == latest
        with kb.write_txn(conn):
            kb._append_event(conn, child, "completed", {"summary": "future result"})
        assert len(notify.unseen_events_for_sub(conn, task_id=child, platform="tui", chat_id="human")[1]) == 1
