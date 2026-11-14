"""Cold dashboard reads must not join writer initialization or miss blocked boards."""
from contextlib import closing
import logging
import sqlite3
import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from plugins.kanban.dashboard.board_prewarm import prewarm_boards
from tests.plugins.test_kanban_board_read_cache import _create, _full_reads, _titles

pytest_plugins = ["tests.plugins.test_kanban_board_read_cache"]


def test_board_list_counts_do_not_initialize_writers(board, monkeypatch):
    _mod, client, _ = board
    _create("visible")
    kb.create_board("waiting")
    with closing(kbc.connect(board="waiting")) as conn:
        tid = kb.create_task(conn, title="blocked", assignee="w")
        conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (tid,))
    initialized = []

    def forbidden(*args, **kwargs):
        initialized.append(True)
        raise AssertionError("read-only counts entered writer initialization")

    monkeypatch.setattr(kbc, "connect", forbidden)
    response = client.get("/api/plugins/kanban/boards")
    assert response.status_code == 200
    counts = {b["slug"]: b["counts"] for b in response.json()["boards"]}
    assert counts["waiting"] == {"blocked": 1}
    assert counts["default"] == {"ready": 1}
    assert initialized == []


def test_counts_accept_legacy_status_only_schema_and_do_not_create(board, tmp_path, monkeypatch):
    mod, _, _ = board
    path = tmp_path / "legacy.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE tasks (status TEXT)")
        conn.executemany("INSERT INTO tasks VALUES (?)", [("blocked",), ("blocked",), ("done",)])
        conn.commit()
    monkeypatch.setattr(kb, "kanban_db_path", lambda **kw: path)
    assert mod._board_counts("legacy") == {"blocked": 2, "done": 1}
    with closing(sqlite3.connect(path)) as conn:
        assert [r[0] for r in conn.execute("SELECT name FROM sqlite_master")] == ["tasks"]
    missing = tmp_path / "missing.db"
    monkeypatch.setattr(kb, "kanban_db_path", lambda **kw: missing)
    assert mod._board_counts("missing") == {}
    assert not missing.exists()


def test_prewarm_covers_blocked_board_poll_without_warming_every_board(board, monkeypatch):
    mod, client, reads = board
    sql_reads = _full_reads(monkeypatch)
    _create("default")
    for slug in ("waiting", "quiet"):
        kb.create_board(slug)
        with closing(kbc.connect(board=slug)) as conn:
            tid = kb.create_task(conn, title=slug, assignee="w")
            if slug == "waiting":
                conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (tid,))
    prewarm_boards(mod)
    assert {args[2] for args in reads} == {"default", "waiting"}
    assert len(sql_reads) == 2
    assert _titles(client.get("/api/plugins/kanban/board?board=waiting").json()) == {"waiting"}
    assert len(sql_reads) == 2


def test_failed_prewarm_is_observable(board, monkeypatch, caplog):
    mod, _, _ = board

    def fail(*args):
        raise RuntimeError("cold read failure")

    monkeypatch.setattr(mod, "_get_board_uncached", fail)
    with caplog.at_level(logging.WARNING):
        prewarm_boards(mod)
    assert "kanban board prewarm failed" in caplog.text


def test_counts_connection_is_query_only_and_closed(board, monkeypatch):
    from plugins.kanban.dashboard import board_counts
    mod, _, _ = board
    _create("visible")
    opened = []
    real = board_counts.open_ro

    def capture(path):
        conn = real(path)
        opened.append(conn)
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM tasks")
        return conn

    monkeypatch.setattr(board_counts, "open_ro", capture)
    assert mod._board_counts("default") == {"ready": 1}
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")


def test_discovery_failure_preserves_default_warmup(board, monkeypatch, caplog):
    mod, client, _reads = board
    sql_reads = _full_reads(monkeypatch)
    _create("default")

    def fail(*args, **kwargs):
        raise OSError("unreadable board directory")

    monkeypatch.setattr(kb, "list_boards", fail)
    with caplog.at_level(logging.WARNING):
        prewarm_boards(mod)
    assert "kanban board prewarm discovery failed" in caplog.text
    assert _titles(client.get("/api/plugins/kanban/board").json()) == {"default"}
    assert len(sql_reads) == 1
