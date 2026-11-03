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


def test_a_released_inherited_link_on_an_open_card_is_wanted_again(tmp_path, monkeypatch):
    """Hub chat 20261016_123957_6212f7 handed work to worker-created t_01a19084a3b36956 (todo), yet its outcome read
    unresolved: a named-profile gateway on an older core held the host sync lock, did not want inherited ``created``
    links and released it. Reconcile kept the stored flag, so the chat never got its card back. [daveey]"""
    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(home, True)
    with kbc.connect_closing(home / "kanban.db") as conn:
        sync.ensure_identity(conn, "default", "here")
        parent = kb.create_task(conn, title="parent", session_id="human")
        links.reconcile(conn, "default", "here", ledger=False)
        child = kb.create_task(conn, title="legacy", session_id=WORKER)
        with kb.write_txn(conn):
            conn.execute("UPDATE task_events SET payload=? WHERE task_id=? AND kind='created'",
                         (json.dumps({"creator_task_id": parent}), child))
        links.reconcile(conn, "default", "here", ledger=False)
        uid = links.link_uid(child, "here", None, "human", "created")
        with kb.write_txn(conn):  # what the foreign reconcile did
            conn.execute("UPDATE task_chat_links SET released=1 WHERE uid=?", (uid,))
        assert links.reconcile(conn, "default", "here", ledger=False)["changed"] == 1
        assert conn.execute("SELECT released FROM task_chat_links WHERE uid=?", (uid,)).fetchone()[0] == 0
        queued = [r[0] for r in conn.execute("SELECT row_uid FROM kanban_sync_outbox WHERE table_name='task_chat_links'")]
        assert uid in queued  # pushed, so the Board replica (links_reader=replica) answers the chat again


def test_a_picked_up_foreign_cards_inherited_link_keeps_the_owners_answer(tmp_path, monkeypatch):
    """Only cards this gateway owns are healed: a foreign copy's released link stays the owner's call. [daveey]"""
    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(home, True)
    with kbc.connect_closing(home / "kanban.db") as conn:
        sync.ensure_identity(conn, "default", "here")
        child = kb.create_task(conn, title="foreign", session_id=WORKER)
        uid = links.link_uid(child, "here", None, "human", "created")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET created_on='elsewhere' WHERE id=?", (child,))
            kb._append_event(conn, child, "creator_chats_resolved", {"creator_task_id": "t_gone"})
            conn.execute("INSERT INTO task_chat_links (uid,task_id,chat_gateway,chat_session,kind,released,created_at) "
                         "VALUES (?,?,'here','human','created',1,1)", (uid, child))
        links.reconcile(conn, "default", "here", ledger=False)
        assert conn.execute("SELECT released FROM task_chat_links WHERE uid=?", (uid,)).fetchone()[0] == 1
