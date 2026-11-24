"""Explicit-home kwargs on canonical readers: skip process-home identity resolvers."""
from __future__ import annotations

from hermes_cli import kanban_db
from hermes_cli.kanban_db_connect import connect
from hermes_cli import kanban_session_activity as ksa


def test_read_session_tasks_uses_passed_scope_without_card_scope(tmp_path, monkeypatch):
    path = tmp_path / "kanban.db"
    conn = connect(path)
    try:
        kanban_db.create_task(conn, title="HOME-CARD", session_id="chat", initial_status="running")
        conn.commit()
    finally:
        conn.close()

    called = []

    def boom(p):
        called.append(p)
        raise AssertionError(f"card_scope:{p}")

    monkeypatch.setattr("hermes_cli.kanban_execution_observations.card_scope", boom)
    ksa._read_session_tasks(path, ["chat"], ("running",), scope=("gw-home", "researcher"))
    assert called == []
    import pytest

    with pytest.raises(AssertionError, match="card_scope:"):
        ksa._read_session_tasks(path, ["chat"], ("running",))
    assert called == [path]
