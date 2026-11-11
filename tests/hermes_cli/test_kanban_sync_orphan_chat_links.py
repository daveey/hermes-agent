"""Reconciliation publishes chat links only for tasks that still exist."""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_sync_client as ksc
from tests.hermes_cli import test_kanban_sync_reconcile as helpers

kanban_home = helpers.kanban_home
live = helpers.live
svc = helpers.svc


@pytest.mark.parametrize("location", ["absent", "local", "replica"])
def test_reconcile_chat_link_task_existence(live, svc, kanban_home, location):
    tid = "t_chat_target"
    if location == "local":
        tid = kb.create_task(live, title="local", assignee="worker")
    elif location == "replica":
        svc.hub("other").ops("default", [{
            "client_op_id": "other-create", "op": "upsert", "table": "tasks", "uid": tid,
            "hlc": "0000000000001-0000-other", "fields": {"title": "remote", "status": "ready", "created_at": 1},
        }])
    helpers._without_capture(live,
        "INSERT INTO task_chat_links (uid, task_id, chat_gateway, chat_profile, chat_session, kind, released, created_at) "
        "VALUES ('chat-link', ?, 'hub', 'default', 'chat', 'shepherd', 0, 1)", (tid,))
    report = ksc.reconcile("default", kanban_home / "kanban.db", hub=svc.hub("hub"))
    if location == "absent":
        assert report["queued"].get("task_chat_links", 0) == 0
        assert not live.execute("SELECT 1 FROM kanban_sync_outbox WHERE table_name = 'task_chat_links'").fetchall()
    else:
        assert report["queued"]["task_chat_links"] == 1
        assert svc.board("default")["rows"]["task_chat_links"]["chat-link"]["task_id"] == tid
    if location == "replica":
        assert kb.get_task(live, tid) is None
