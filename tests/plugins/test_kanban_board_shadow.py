"""``/board`` on a shadow-mode board adds the replica's cards, read-only; the replica is never written."""
from __future__ import annotations

from contextlib import closing
import importlib.util
import sys
import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync


def _load_plugin():
    plugin_file = Path(__file__).resolve().parents[2] / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("hermes_kanban_plugin_board_shadow_test", plugin_file)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _cards(body):
    return {t["id"]: t for col in body["columns"] for t in col["tasks"]}


def _column(body, name):
    return next(c for c in body["columns"] if c["name"] == name)


@pytest.fixture
def shadow_board(tmp_path, monkeypatch):
    from plugins.kanban.dashboard import board_rows, board_shadow

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(sync, "sync_config", lambda: {"enabled": True, "boards": ["default"]})
    kb.init_db()
    board_rows.clear()
    board_shadow.clear()
    with closing(kbc.connect()) as live, live:
        live_id = kb.create_task(live, title="Hub card", assignee="default")
        sync.set_mode(live, "shadow")
    shadow = home / "kanban" / sync.SHADOW_DIR / "default.db"
    shadow.parent.mkdir(parents=True, exist_ok=True)
    out = {"live_id": live_id, "shadow": shadow, "mod": _load_plugin()}
    with closing(kbc.connect(shadow)) as replica, replica:
        if "created_on" not in {r[1] for r in replica.execute("PRAGMA table_info(tasks)")}:
            replica.execute("ALTER TABLE tasks ADD COLUMN created_on TEXT")
        ids = {}
        for name, status in (("ready", "ready"), ("running", "running"), ("done", "done")):
            ids[name] = kb.create_task(replica, title=f"Mini {name}", assignee="default")
            replica.execute("UPDATE tasks SET status = ?, created_on = 'mini' WHERE id = ?", (status, ids[name]))
        replica.execute("INSERT INTO tasks (id, title, status, created_at, created_on) VALUES (?, ?, 'ready', 1, 'mini')",
                        (live_id, "Mini copy of hub card"))
    out.update(ids)
    yield out
    board_rows.clear()
    board_shadow.clear()


def _read(env, **kw):
    return env["mod"]._get_board_uncached(None, kw.get("archived", False), None, None, None, kw.get("done_limit", 80))


def test_board_shows_replica_cards_marked_remote_read_only(shadow_board):
    cards = _cards(_read(shadow_board))
    for name, column in (("ready", "ready"), ("running", "running"), ("done", "done")):
        card = cards[shadow_board[name]]
        assert (card["remote"], card["read_only"], card["home_gateway"]) == (True, True, "mini")
        assert card["status"] == column
    assert "remote" not in cards[shadow_board["live_id"]]


def test_board_hub_row_wins_on_id_collision(shadow_board):
    body = _read(shadow_board)
    rows = [t for col in body["columns"] for t in col["tasks"] if t["id"] == shadow_board["live_id"]]
    assert len(rows) == 1 and rows[0]["title"] == "Hub card" and "remote" not in rows[0]


def test_board_done_column_total_counts_remote_cards(shadow_board):
    done = _column(_read(shadow_board), "done")
    assert done["total"] == 1 and done["omitted"] == 0
    capped = _column(_read(shadow_board, done_limit=1), "done")
    assert len(capped["tasks"]) == 1 and capped["total"] == 1


def test_board_without_shadow_mode_is_live_only(shadow_board):
    with closing(kbc.connect()) as live, live:
        live.execute("UPDATE kanban_sync_state SET mode = 'primary'")
    assert set(_cards(_read(shadow_board))) == {shadow_board["live_id"]}


def test_board_read_never_writes_the_replica(shadow_board):
    shadow = shadow_board["shadow"]
    before, mtime = shadow.read_bytes(), shadow.stat().st_mtime_ns
    _read(shadow_board)
    _read(shadow_board, archived=True)
    assert shadow.read_bytes() == before and shadow.stat().st_mtime_ns == mtime
    wal = shadow.with_name(shadow.name + "-wal")
    assert not wal.exists() or wal.stat().st_size == 0


def test_board_follows_a_replica_commit(shadow_board):
    first = _cards(_read(shadow_board))
    with closing(kbc.connect(shadow_board["shadow"])) as replica, replica:
        replica.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (shadow_board["ready"],))
    assert first[shadow_board["ready"]]["status"] == "ready"
    assert _cards(_read(shadow_board))[shadow_board["ready"]]["status"] == "blocked"


def test_http_board_route_and_writes_on_remote_card_refused(shadow_board):
    app = FastAPI()
    app.include_router(shadow_board["mod"].router, prefix="/api/plugins/kanban")
    client = TestClient(app)
    res = client.get("/api/plugins/kanban/board")
    assert res.status_code == 200
    card = _cards(res.json())[shadow_board["ready"]]
    assert card["read_only"] is True and card["home_gateway"] == "mini"
    patch = client.patch(f"/api/plugins/kanban/tasks/{shadow_board['ready']}", json={"status": "done"})
    assert patch.status_code == 409 and "foreign_card" in patch.text
    shown = client.get(f"/api/plugins/kanban/tasks/{shadow_board['ready']}")
    assert shown.status_code == 200 and shown.json()["source"] == "replica"


def test_done_history_is_counted_before_cap_and_collisions(shadow_board):
    with closing(kbc.connect(shadow_board["shadow"])) as replica, replica:
        for i in range(6):
            tid = kb.create_task(replica, title=f"History {i}", assignee="default")
            replica.execute("UPDATE tasks SET status='done', completed_at=?, created_on='mini' WHERE id=?", (i, tid))
    with closing(kbc.connect()) as live, live:
        live.execute("UPDATE tasks SET status='done', completed_at=99 WHERE id=?", (shadow_board["live_id"],))
    for cap in (1, 3, 80, 0):
        col = _column(_read(shadow_board, done_limit=cap), "done")
        assert col["total"] == 8
        assert len(col["tasks"]) == (min(cap, 8) if cap else 8)
        assert col["omitted"] == 8 - len(col["tasks"])
        assert col["tasks"][0]["id"] == shadow_board["live_id"]


def test_live_row_outside_payload_wins_and_does_not_consume_cap(shadow_board):
    with closing(kbc.connect()) as live, live:
        live.execute("UPDATE tasks SET status='archived' WHERE id=?", (shadow_board["live_id"],))
    with closing(kbc.connect(shadow_board["shadow"])) as replica, replica:
        replica.execute("UPDATE tasks SET status='done', completed_at=99 WHERE id=?", (shadow_board["live_id"],))
    body = _read(shadow_board, done_limit=1)
    assert shadow_board["live_id"] not in _cards(body)
    col = _column(body, "done")
    assert col["total"] == 1 and col["omitted"] == 0
    assert [t["id"] for t in col["tasks"]] == [shadow_board["done"]]


def test_board_unknown_home_and_owner_cache(shadow_board, monkeypatch):
    from hermes_cli import kanban_shadow_view
    with closing(kbc.connect(shadow_board["shadow"])) as replica, replica:
        replica.execute("UPDATE tasks SET created_on=NULL WHERE id=?", (shadow_board["ready"],))
    assert _cards(_read(shadow_board))[shadow_board["ready"]]["home_gateway"] == "unknown"
    def no_query(*args):
        pytest.fail("cached board must retain home metadata")
    monkeypatch.setattr(kanban_shadow_view, "homes_of", no_query)
    assert _cards(_read(shadow_board))[shadow_board["ready"]]["home_gateway"] == "unknown"


def test_unreadable_replica_keeps_live_board(shadow_board, monkeypatch):
    from plugins.kanban.dashboard import board_read
    original = board_read.materialize
    def unreadable(conn, *args, **kwargs):
        from hermes_cli.kanban_shadow_view import db_file
        if db_file(conn) == str(shadow_board["shadow"]):
            raise RuntimeError("replica rebuilding")
        return original(conn, *args, **kwargs)
    monkeypatch.setattr(board_read, "materialize", unreadable)
    assert set(_cards(_read(shadow_board))) == {shadow_board["live_id"]}


def test_for_board_live_connect_failure_preserves_payload():
    from plugins.kanban.dashboard import board_shadow
    payload = {"sentinel": True}
    def broken():
        raise sqlite3.OperationalError("live unavailable")
    assert board_shadow.for_board(payload, broken, (None, False, None, None, 80), None, None) is payload
