"""GET /boards counts of a changed board move only the changed tasks (card E stage7).

A changed board used to be re-counted with a whole-table ``GROUP BY status``. The counts now
follow the P2 change cursor: the next read takes the changed ids and their statuses in one
snapshot. Every case compares against the exact ``GROUP BY`` of the live file.
"""
from contextlib import closing
from unittest import mock

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_task_change as changes
from hermes_cli.kanban_generation import open_ro
from plugins.kanban.dashboard import board_counts, board_rows
from tests.plugins import test_kanban_board_read_cache as helpers

board = helpers.board


@pytest.fixture(autouse=True)
def reset():
    board_rows.clear()
    board_counts.clear()
    yield
    board_rows.clear()
    board_counts.clear()


def _exact() -> dict:
    with closing(open_ro(kb.kanban_db_path())) as conn:
        return {s: n for s, n in conn.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status")}


def _counts(mod) -> dict:
    return next(b for b in mod.list_boards(include_archived=False)["boards"] if b["slug"] == "default")["counts"]


@pytest.fixture
def statements(monkeypatch):
    """Every statement a count connection runs."""
    seen: list[str] = []
    real = board_counts.open_ro

    def traced(path, *a, **k):
        conn = real(path, *a, **k)
        conn.set_trace_callback(lambda q: seen.append(" ".join(q.split())))
        return conn

    monkeypatch.setattr(board_counts, "open_ro", traced)
    return seen


def _whole_table(seen: list[str]) -> list[str]:
    return [q for q in seen if q.startswith("SELECT") and "FROM tasks" in q and "WHERE" not in q]


def _seed(n: int) -> list[str]:
    with closing(kbc.connect()) as conn:
        return [kb.create_task(conn, title=f"t{i}", assignee="w") for i in range(n)]


def test_changed_board_reads_only_changed_tasks(board, statements):
    mod, _client, _reads = board
    ids = _seed(6)
    assert _counts(mod) == _exact() == {"ready": 6}
    statements.clear()
    with closing(kbc.connect()) as conn:
        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (ids[0],))
        conn.commit()
    assert _counts(mod) == _exact() == {"ready": 5, "blocked": 1}
    assert statements, "the changed board was read"
    assert not _whole_table(statements), statements


def test_create_delete_status_moves_and_null_status_stay_exact(board):
    mod, _client, _reads = board
    ids = _seed(5)
    _counts(mod)
    with closing(kbc.connect()) as conn:
        conn.execute("UPDATE tasks SET status='done' WHERE id IN (?, ?)", (ids[0], ids[1]))
        conn.execute("DELETE FROM tasks WHERE id=?", (ids[2],))
        conn.commit()
        kb.create_task(conn, title="new", assignee="w")
    assert _counts(mod) == _exact()
    with closing(kbc.connect()) as conn:
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (ids[0],))  # back again
        conn.execute("UPDATE tasks SET title='only title' WHERE id=?", (ids[3],))
        conn.commit()
    assert _counts(mod) == _exact()
    with closing(kbc.connect()) as conn:
        conn.execute("DELETE FROM tasks")
        conn.commit()
    assert _counts(mod) == _exact() == {}


def test_heartbeat_only_commit_does_not_read_tasks(board, statements):
    mod, _client, _reads = board
    ids = _seed(2)
    _counts(mod)
    statements.clear()
    with closing(kbc.connect()) as conn:
        conn.execute("UPDATE tasks SET last_heartbeat_at=5 WHERE id=?", (ids[0],))
        conn.commit()
    assert _counts(mod) == {"ready": 2}
    assert statements == []


def test_full_cursor_rereads_whole_board(board, statements):
    mod, _client, _reads = board
    ids = _seed(3)
    _counts(mod)
    real = changes.changed_since

    def full(conn, cursor, **kw):
        _c, fresh = real(conn, cursor, **kw)
        return changes.FULL, fresh

    with closing(kbc.connect()) as conn:
        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (ids[1],))
        conn.commit()
    statements.clear()
    with mock.patch.object(changes, "changed_since", full):
        assert _counts(mod) == _exact() == {"ready": 2, "blocked": 1}
    assert _whole_table(statements)


def test_many_changes_take_the_whole_board_read(board, statements, monkeypatch):
    mod, _client, _reads = board
    _seed(4)
    _counts(mod)
    monkeypatch.setattr(board_counts, "DELTA_MAX", 2)
    with closing(kbc.connect()) as conn:
        conn.execute("UPDATE tasks SET status='done'")
        conn.commit()
    statements.clear()
    assert _counts(mod) == _exact() == {"done": 4}
    assert _whole_table(statements)


def test_failed_delta_read_stores_nothing_and_recovers(board, monkeypatch):
    mod, _client, _reads = board
    ids = _seed(3)
    _counts(mod)
    with closing(kbc.connect()) as conn:
        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (ids[0],))
        conn.commit()
    real = board_counts.open_ro
    left = {"n": 1}

    class Failing:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, *a):
            if "WHERE id IN" in sql:
                raise board_counts.sqlite3.OperationalError("disk I/O error")
            return self._conn.execute(sql, *a)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    def flaky(path, *a, **k):
        conn = real(path, *a, **k)
        if left["n"]:
            left["n"] -= 1
            return Failing(conn)
        return conn

    monkeypatch.setattr(board_counts, "open_ro", flaky)
    assert _counts(mod) == {}
    assert not board_counts._cached
    assert _counts(mod) == _exact() == {"ready": 2, "blocked": 1}
