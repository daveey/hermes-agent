"""Tool reads must see a foreign parent without importing it into the live board."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from tools import kanban_tools as kt


@pytest.fixture
def boards(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(sync, "sync_config", lambda: {"enabled": True, "boards": ["default"]})
    kb.init_db()
    shadow = home / "kanban" / sync.SHADOW_DIR / "default.db"
    shadow.parent.mkdir(parents=True, exist_ok=True)
    with kbc.connect(shadow) as replica:
        tid = kb.create_task(replica, title="Foreign parent", assignee="default")
        kb.add_comment(replica, tid, author="owner", body="Parent evidence")
        if "created_on" not in {r[1] for r in replica.execute("PRAGMA table_info(tasks)")}:
            replica.execute("ALTER TABLE tasks ADD COLUMN created_on TEXT")
        replica.execute("UPDATE tasks SET created_on = 'owner-gateway' WHERE id = ?", (tid,))
    return tid, shadow


def test_foreign_parent_read_is_labelled_and_does_not_import(boards):
    tid, shadow = boards
    out = json.loads(kt._handle_show({"task_id": tid}))
    assert out.get("source") == "replica", out
    assert out["owner"] == "owner-gateway"
    assert out["task"]["id"] == tid
    assert [c["body"] for c in out["comments"]] == ["Parent evidence"]
    assert "queue" not in out
    assert "queue_hint" not in out
    assert "worker_context" in out
    with kbc.connect() as live:
        assert kb.get_task(live, tid) is None
    with kbc.connect(shadow) as replica:
        assert len(kb.list_comments(replica, tid)) == 1


def test_disabled_sync_does_not_read_foreign_card(boards, monkeypatch):
    tid, _ = boards
    monkeypatch.setattr(sync, "sync_config", lambda: {"enabled": False})
    out = json.loads(kt._handle_show({"task_id": tid}))
    assert "not found" in out["error"]


def test_missing_card_remains_not_found(boards):
    out = json.loads(kt._handle_show({"task_id": "t_missing"}))
    assert "not found" in out["error"]


def test_live_card_wins_over_replica(boards, monkeypatch):
    tid, _ = boards
    monkeypatch.setattr(sync, "new_task_id", lambda conn: tid)
    with kbc.connect() as live:
        assert kb.create_task(live, title="Live card", assignee="default") == tid
    out = json.loads(kt._handle_show({"task_id": tid}))
    assert out.get("source") == "live", out
    assert out["task"]["title"] == "Live card"
    assert "owner" not in out


def test_foreign_comment_is_still_refused(boards):
    tid, shadow = boards
    out = json.loads(kt._handle_comment({"task_id": tid, "body": "must not land"}))
    assert out.get("rejection") == "foreign_card", out
    with kbc.connect(shadow) as replica:
        assert [c.body for c in kb.list_comments(replica, tid)] == ["Parent evidence"]
