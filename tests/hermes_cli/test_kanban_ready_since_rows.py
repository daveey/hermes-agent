"""Linked-card rows and card reads carry ``ready_since`` (ms), so the session panel can say how long a ready card
has waited for a worker. Boards without the Board-synced column read None, never fail."""

from __future__ import annotations

import sqlite3

import pytest

from hermes_cli import kanban_chat_links as links
from hermes_cli import kanban_session_activity as activity
from tests.hermes_cli import test_kanban_chat_links as fixtures
from tests.hermes_cli import test_kanban_session_outcome_tasks as outcome
from tests.hermes_cli.test_kanban_session_outcome_tasks import _connect, _insert_task
from tests.plugins import test_kanban_link_tasks as card_api

READY_MS = 1_795_209_437_664

board_root = outcome.kanban_root


@pytest.fixture
def board_api(tmp_path, monkeypatch):
    from pathlib import Path

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from hermes_cli import kanban_db as kb
    (tmp_path / ".hermes").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    app = FastAPI()
    app.include_router(card_api._load_plugin_router(), prefix="/api/plugins/kanban")
    return TestClient(app)


@pytest.mark.parametrize("has_column", [True, False])
def test_local_session_task_row_carries_ready_since(board_root, has_column):
    conn = _connect(board_root / "kanban.db")
    extra = {}
    if has_column:
        conn.execute("ALTER TABLE tasks ADD COLUMN ready_since INTEGER")
        extra["ready_since"] = READY_MS
    _insert_task(conn, "t_ready", "ready", "s1", **extra)
    conn.commit()
    conn.close()
    rows = activity.session_tasks(["s1"])
    assert [r["id"] for r in rows] == ["t_ready"]
    assert rows[0]["ready_since"] == (READY_MS if has_column else None)


@pytest.mark.parametrize("has_column", [True, False])
def test_replica_row_carries_ready_since(tmp_path, monkeypatch, has_column):
    path = tmp_path / "replica.db"
    fixtures._replica(path, [("child", "child", "ready", fixtures.OTHER)], [("child", fixtures.ME, "s1", "created", 0)])
    if has_column:
        with sqlite3.connect(path) as conn:
            conn.execute("ALTER TABLE tasks ADD COLUMN ready_since INTEGER")
            conn.execute("UPDATE tasks SET ready_since=?", (READY_MS,))
    monkeypatch.setattr(activity, "_remote_links", lambda **kw: [])
    monkeypatch.setattr(activity, "_board_db_paths", list)
    monkeypatch.setattr(activity, "_remote_cards", lambda ids, statuses, stale_ok: links.replica_cards(
        ids, fixtures.ME, statuses, replicas=[("default", path)]))
    links._reset_for_tests()
    rows = activity.session_tasks(["s1"])
    assert len(rows) == 1 and rows[0]["source"] == "replica"
    assert rows[0]["ready_since"] == (READY_MS if has_column else None)


def test_card_read_carries_ready_since(board_api):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="Ready card")
        with kbc.write_txn(conn):
            if "ready_since" not in {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}:
                conn.execute("ALTER TABLE tasks ADD COLUMN ready_since INTEGER")
            conn.execute("UPDATE tasks SET ready_since=? WHERE id=?", (READY_MS, tid))
    finally:
        conn.close()
    detail = board_api.get(f"/api/plugins/kanban/tasks/{tid}").json()
    assert detail["task"]["ready_since"] == READY_MS
