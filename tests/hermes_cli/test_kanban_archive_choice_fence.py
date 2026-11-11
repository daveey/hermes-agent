"""Archive choice fences and decision evidence share the transition transaction."""

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = connect()
    task = kb.create_task(conn, title="Synthetic archive choice", assignee="default")
    try:
        yield conn, task
    finally:
        conn.close()


def test_archive_precondition_refusal_writes_nothing(board, monkeypatch):
    conn, task = board
    before = conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]
    monkeypatch.setattr(
        kb, "_cleanup_workspace", lambda *a: pytest.fail("refusal cleaned workspace")
    )
    checks = []

    def refuse(c):
        checks.append(c.in_transaction)
        return "ask changed"

    assert not kb.archive_task(
        conn, task, precondition=refuse, comment=("user-steps", "Archive selected")
    )
    assert checks == [True]
    assert kb.get_task(conn, task).status != "archived"
    assert conn.execute("SELECT COUNT(*) FROM task_comments").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == before


def test_archive_commits_choice_comment_and_receipt_before_cleanup(board, monkeypatch):
    conn, task = board

    def cleanup(c, tid):
        assert not c.in_transaction
        assert kb.get_task(c, tid).status == "archived"
        assert (
            c.execute(
                "SELECT body FROM task_comments WHERE task_id=?", (tid,)
            ).fetchone()[0]
            == "Archive selected"
        )

    monkeypatch.setattr(kb, "_cleanup_workspace", cleanup)
    assert kb.archive_task(
        conn,
        task,
        precondition=lambda c: None,
        comment=("user-steps", "Archive selected"),
        event_extra={"choice_receipt": {"operation": "test-op"}},
    )
    event = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='archived'", (task,)
    ).fetchone()[0]
    assert "test-op" in event
    assert not kb.archive_task(
        conn, task, precondition=lambda c: None, comment=("user-steps", "Duplicate")
    )
    assert conn.execute("SELECT COUNT(*) FROM task_comments").fetchone()[0] == 1


def test_failed_archive_rolls_back_comment_state_and_receipt(board, monkeypatch):
    conn, task = board
    original = kb._append_event

    def broken(c, tid, kind, *args, **kwargs):
        if kind == "archived":
            raise RuntimeError("injected receipt failure")
        return original(c, tid, kind, *args, **kwargs)

    monkeypatch.setattr(kb, "_append_event", broken)
    with pytest.raises(RuntimeError, match="injected"):
        kb.archive_task(
            conn,
            task,
            precondition=lambda c: None,
            comment=("user-steps", "Archive selected"),
        )
    assert kb.get_task(conn, task).status != "archived"
    assert conn.execute("SELECT COUNT(*) FROM task_comments").fetchone()[0] == 0
