"""A board's reclaim error must not strand durable notices collected on other boards."""
from unittest.mock import patch

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from hermes_state import SessionDB
from tui_gateway import server


def _task(chat_id, board=None):
    with kbc.connect_closing(board=board) as conn:
        tid = kb.create_task(conn, title="reclaim isolation", assignee="worker")
        kbn.add_notify_sub(conn, task_id=tid, platform="tui", chat_id=chat_id)
        kb.complete_task(conn, tid, summary="shipped")
        return tid


@pytest.mark.parametrize("failure", ["corrupt", "archived", "permission"])
def test_reclaim_error_keeps_other_board_and_remote_notices(tmp_path, failure):
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session(session_id="parent", source="desktop")
        db.create_session(session_id="child", source="subagent", parent_session_id="parent",
                          model_config={"_delegate_from": "parent"})
        kb.create_board("zz")
        own = _task("parent")
        stray = _task("child", board="zz")
        real_connect = kbc.connect

        def connect(*args, **kwargs):
            if kwargs.get("board") == "zz":
                if failure == "corrupt":
                    raise kbc.KanbanDbCorruptError(kb.kanban_db_path(board="zz"), None, "test")
                if failure == "archived":
                    raise ValueError("board archived")
                raise PermissionError("board read only")
            return real_connect(*args, **kwargs)

        session = {"session_key": "parent"}
        with patch.object(server, "_get_db", lambda: db), \
                patch.object(server, "_collect_remote_kanban_notifications", return_value=["remote notice"]):
            with patch.object(kbc, "connect", connect):
                texts, claims = server._notif_collect_kanban(session)
            assert any(own in text for text in texts)
            assert "remote notice" in texts
            assert any(c["task_id"] == own and c["texts"] for c in claims)
            assert session["_kanban_stray_keys"] == ("child",)
            retried, retry_claims = server._notif_collect_kanban(session)
            assert any(stray in text for text in retried)
            assert any(c["task_id"] == stray for c in retry_claims)
            assert session["_kanban_stray_keys"] == ()
    finally:
        db.close()
