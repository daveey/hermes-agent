"""Kanban dashboard plugin: which chat session manages each card. [daveey]

The desktop's left-pane Blocked section groups cards by the session managing
them: the card's latest ``tui`` notify subscriber (a shepherding chat), else
the session that created it. ``/session-labels`` names those sessions and
follows compression to the live tip.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn


def _load_plugin_router():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("hermes_kanban_plugin_managing_test", plugin_file)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.router


@pytest.fixture
def client(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()

    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    return TestClient(app)


def _cards(client):
    board = client.get("/api/plugins/kanban/board").json()
    return {t["id"]: t for col in board["columns"] for t in col["tasks"]}


def test_managing_session_is_latest_tui_subscriber_else_creator(client):
    with kbc.connect() as conn:
        created_only = kb.create_task(conn, title="mine", session_id="sess-creator")
        shepherded = kb.create_task(conn, title="handed over", session_id="sess-creator")
        orphan = kb.create_task(conn, title="from the cli")
        kbn.add_notify_sub(conn, task_id=shepherded, platform="tui", chat_id="sess-old")
        kbn.add_notify_sub(conn, task_id=shepherded, platform="slack", chat_id="C123")
        kbn.add_notify_sub(conn, task_id=shepherded, platform="tui", chat_id="sess-shepherd")
        conn.execute(
            "UPDATE kanban_notify_subs SET created_at = CASE chat_id WHEN 'sess-old' THEN 100 "
            "WHEN 'sess-shepherd' THEN 200 ELSE 300 END")
        conn.commit()

    cards = _cards(client)
    assert cards[created_only]["managing_session_id"] == "sess-creator"
    assert cards[shepherded]["managing_session_id"] == "sess-shepherd"
    assert cards[orphan]["managing_session_id"] is None


def test_session_labels_follow_compression_to_the_tip(client):
    from hermes_state import SessionDB

    db = SessionDB()
    try:
        db.create_session("root", source="tui")
        db.set_session_title("root", "Ship the thing")
        db.end_session("root", "compression")
        db.create_session("tip", source="tui", parent_session_id="root")
    finally:
        db.close()

    r = client.get("/api/plugins/kanban/session-labels", params={"ids": "root,tip,missing,root"})
    assert r.status_code == 200, r.text
    sessions = r.json()["sessions"]
    assert set(sessions) == {"root", "tip"}
    assert sessions["root"]["tip_id"] == "tip"
    assert sessions["tip"]["tip_id"] == "tip"
    # The tip has no title of its own yet: fall back to the id we were handed.
    assert sessions["root"]["title"] == "Ship the thing"
