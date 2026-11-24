"""Explicit tool board intent must not silently redirect a dispatcher worker."""
import json
from contextlib import closing

import pytest


@pytest.mark.parametrize("own_board", ["default", "team", pytest.param("linked", marks=pytest.mark.platforms("posix"))])
def test_explicit_board_pin(monkeypatch, tmp_path, own_board):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools  # register real handlers
    from tools.registry import registry

    for key in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD",
                "HERMES_KANBAN_HOME", "HERMES_DELEGATED_CHILD_CONTEXT",
                "HERMES_DELEGATED_CHILD_OWNER", "HERMES_ORIGIN_SESSION", "HERMES_SESSION_ID"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    paths = {slug: kb.board_own_db_path(slug) for slug in (own_board, "other")}
    if own_board == "linked":
        backing = tmp_path / "board-storage"
        backing.mkdir()
        paths[own_board].parent.parent.mkdir(parents=True)
        paths[own_board].parent.symlink_to(backing, target_is_directory=True)
    for slug in paths:
        kb.create_board(slug)
    from tests.tools.test_kanban_create_overlap import register_chat
    chat = register_chat(tmp_path, "20261123_120000_worker_owner")
    monkeypatch.setenv("HERMES_SESSION_ID", chat)
    with closing(kbc.connect(board=own_board)) as conn:
        tid = kb.create_task(conn, title="worker", assignee="test-worker", session_id=chat)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(paths[own_board]))
    # A stale current-board hint must not override the database pin.
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "other")

    def call(name, **args):
        result = registry.dispatch(name, args)
        assert isinstance(result, str)
        return json.loads(result)

    def rows(slug):
        with closing(kbc.connect(db_path=paths[slug])) as conn:
            return [tuple(row) for row in conn.execute("SELECT id, title FROM tasks ORDER BY id")]

    before = {slug: rows(slug) for slug in paths}
    create = dict(title="child", assignee="test-worker", requires=[], placement="local")
    for result in (call("kanban_create", board="other", **create),
                   call("kanban_show", board="other", task_id=tid)):
        assert result.get("code") == "board_pinned", result
        assert result["pinned_board"] == own_board
        assert result["requested_board"] == "other"
    assert {slug: rows(slug) for slug in paths} == before
    assert call("kanban_show", board=own_board, task_id=tid)["task"]["id"] == tid
    created = call("kanban_create", board=own_board, **create)
    assert created.get("ok"), created
    assert (created["task_id"], "child") in rows(own_board)
    assert rows("other") == before["other"]

    monkeypatch.delenv("HERMES_KANBAN_TASK")
    other = call("kanban_create", board="other", **{**create, "title": "other child"})
    assert other.get("ok"), other
    assert (other["task_id"], "other child") in rows("other")
    assert (other["task_id"], "other child") not in rows(own_board)
