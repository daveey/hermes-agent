"""GET /boards: an unchanged poll reuses each board's counts and skips the projects db.

The Desktop polls the board switcher continuously. Each board's counts are keyed on the
retained observer's ``board_rows.version`` (generation, file identity, schema_version,
data_version), so a poll with nothing committed opens no connection, and the next poll
after any commit re-reads exactly the board that changed.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import projects_db as pdb
from plugins.kanban.dashboard import board_counts, board_rows
from tests.plugins.test_kanban_board_read_cache import _create

pytest_plugins = ["tests.plugins.test_kanban_board_read_cache"]

NAMED = ("alpha", "beta", "gamma", "delta", "epsilon")


def _reset():
    getattr(board_counts, "clear", lambda: None)()  # parent tree (RED run) has no counts cache
    board_rows.clear()


@pytest.fixture
def boards(board):
    _reset()
    mod, _client, _reads = board
    _create("default task")
    for i, slug in enumerate(NAMED):
        kb.create_board(slug)
        with closing(kbc.connect(board=slug)) as conn:
            for n in range(i + 1):
                kb.create_task(conn, title=f"{slug}-{n}", assignee="w")
    yield mod
    _reset()


@pytest.fixture
def probe(monkeypatch):
    """Count every SQLite open, every count-query connection (by board path) and every
    statement it runs, and every projects db open."""
    seen = {"sqlite": 0, "count_paths": [], "count_sql": [], "projects": 0}
    real_sqlite = sqlite3.connect
    real_open_ro = board_counts.open_ro
    real_pconnect = pdb.connect

    def sqlite_connect(*a, **k):
        seen["sqlite"] += 1
        return real_sqlite(*a, **k)

    def open_ro(path, *a, **k):
        seen["count_paths"].append(str(path))
        conn = real_open_ro(path, *a, **k)
        conn.set_trace_callback(seen["count_sql"].append)
        return conn

    def pconnect(*a, **k):
        seen["projects"] += 1
        return real_pconnect(*a, **k)

    monkeypatch.setattr(sqlite3, "connect", sqlite_connect)
    monkeypatch.setattr(board_counts, "open_ro", open_ro)
    monkeypatch.setattr(pdb, "connect", pconnect)

    def reset():
        seen.update(sqlite=0, count_paths=[], count_sql=[], projects=0)
    seen["reset"] = reset
    return seen


def _list(mod) -> dict:
    return mod.list_boards(include_archived=False)


def _counts(resp: dict) -> dict[str, dict]:
    return {b["slug"]: b["counts"] for b in resp["boards"]}


def _path(slug: str) -> str:
    return str(kb.kanban_db_path(board=slug))


def _expected() -> dict[str, dict]:
    out = {"default": {"ready": 1}}
    out.update({slug: {"ready": i + 1} for i, slug in enumerate(NAMED)})
    return out


def test_unchanged_poll_opens_nothing_and_runs_no_count_query(boards, probe):
    mod = boards
    first = _list(mod)
    assert len(first["boards"]) == 6 and _counts(first) == _expected()
    assert not any(b["project_id"] for b in first["boards"])
    probe["reset"]()
    second = _list(mod)
    assert second == first
    assert probe["count_paths"] == [] and probe["count_sql"] == []
    assert probe["sqlite"] == 0
    assert probe["projects"] == 0


def test_commit_shows_on_next_poll_and_only_that_board_is_reread(boards, probe):
    mod = boards
    _list(mod)
    with closing(kbc.connect(board="gamma")) as conn:
        conn.execute("UPDATE tasks SET status = 'blocked' WHERE title = 'gamma-0'")
        kb.create_task(conn, title="gamma-new", assignee="w")
    probe["reset"]()
    resp = _list(mod)
    expected = _expected()
    expected["gamma"] = {"ready": 3, "blocked": 1}
    assert _counts(resp) == expected
    assert probe["count_paths"] == [_path("gamma")]
    probe["reset"]()
    assert _counts(_list(mod)) == expected and probe["count_paths"] == []


def test_replaced_board_file_reads_fresh_and_removed_board_is_dropped(boards, probe):
    mod = boards
    _list(mod)
    old_ino = Path(_path("delta")).stat().st_ino
    kb.remove_board("delta", archive=False)
    kb.remove_board("beta", archive=False)
    kb.create_board("delta")
    with closing(kbc.connect(board="delta")) as conn:
        tid = kb.create_task(conn, title="fresh", assignee="w")
        conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (tid,))
    assert Path(_path("delta")).stat().st_ino != old_ino
    probe["reset"]()
    counts = _counts(_list(mod))
    assert "beta" not in counts
    assert counts["delta"] == {"blocked": 1}
    assert _path("delta") in probe["count_paths"]


def test_unobservable_board_is_read_every_call(boards, probe, monkeypatch):
    mod = boards
    monkeypatch.setattr(board_rows, "version", lambda path: None)
    assert _counts(_list(mod)) == _expected()
    probe["reset"]()
    assert _counts(_list(mod)) == _expected()
    assert len(probe["count_paths"]) == 6
    with closing(kbc.connect(board="alpha")) as conn:
        conn.execute("UPDATE tasks SET status = 'done'")
    expected = _expected()
    expected["alpha"] = {"done": 1}
    assert _counts(_list(mod)) == expected
    assert not getattr(board_counts, "_cached", {})  # an unobservable board stores nothing


def test_failed_count_query_is_not_remembered(boards, monkeypatch):
    mod = boards
    real = board_counts.open_ro
    fail = {"left": 1}

    def flaky(path, *a, **k):
        if fail["left"] and str(path) == _path("epsilon"):
            fail["left"] -= 1
            raise sqlite3.OperationalError("database is locked")
        return real(path, *a, **k)

    monkeypatch.setattr(board_counts, "open_ro", flaky)
    assert _counts(_list(mod))["epsilon"] == {}
    assert _counts(_list(mod)) == _expected()


def test_bookkeeping_commit_with_same_counts_still_rereads(boards, probe):
    """data_version is in the key: any commit re-reads, never a stale count from an
    older data_version even if a write left schema and file identity unchanged."""
    mod = boards
    _list(mod)
    with closing(kbc.connect(board="alpha")) as conn:
        conn.execute("UPDATE tasks SET status = 'running'")
    probe["reset"]()
    assert _counts(_list(mod))["alpha"] == {"running": 1}
    assert probe["count_paths"] == [_path("alpha")]


def test_project_scoped_board_still_gets_project_name(boards, probe):
    mod = boards
    with pdb.connect_closing() as pconn:
        pid = pdb.create_project(pconn, name="Proj X")
    kb.create_board("scoped", project_id=pid)
    _list(mod)
    probe["reset"]()
    resp = _list(mod)
    scoped = next(b for b in resp["boards"] if b["slug"] == "scoped")
    assert scoped["project_id"] == pid and scoped["project_name"] == "Proj X"
    assert probe["projects"] == 1
    others = [b for b in resp["boards"] if b["slug"] != "scoped"]
    assert all(b["project_id"] is None and b["project_name"] is None for b in others)


def test_mutating_a_response_does_not_corrupt_the_next(boards):
    mod = boards
    for _ in range(2):  # miss, then hit
        resp = _list(mod)
        for b in resp["boards"]:
            b["counts"]["ready"] = 999
            b["counts"]["bogus"] = 1
    assert _counts(_list(mod)) == _expected()
