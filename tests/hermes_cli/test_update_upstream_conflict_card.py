"""A real conflicting git merge creates one repair owner, not only a log line."""
from hermes_cli import kanban_db as kb
from hermes_cli.config import save_config
from hermes_cli.kanban_db_connect import connect_closing
import pytest

from tests.hermes_cli.test_update_fork_merge_upstream import repo as _repo_fixture, run, sh


@pytest.fixture
def conflict_repo(tmp_path, monkeypatch):
    return _repo_fixture.__wrapped__(tmp_path, monkeypatch)


def test_conflict_files_one_card_and_refreshes_it(conflict_repo, tmp_path, monkeypatch):
    from tests.hermes_cli.test_update_upstream_conflict import _gateway_identity
    _gateway_identity(tmp_path, monkeypatch, "hub")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "board.db"))
    save_config({"platform_toolsets": {"cli": ["terminal", "file"]}, "terminal": {"backend": "local"}})
    up, _fork, live, gate = conflict_repo
    assert not run(live, True, ("a.txt", "upstream conflict\n"))
    upstream = sh("git", "rev-parse", "HEAD", cwd=up)
    with connect_closing() as conn:
        rows = conn.execute("SELECT id, body, idempotency_key FROM tasks").fetchall()
        assert len(rows) == 1
        task_id, body, key = rows[0]
        assert upstream in body and "a.txt" in body
        assert key.startswith("upstream-merge-conflict:")
    assert not run(live, True)
    with connect_closing() as conn:
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
        assert conn.execute("SELECT id FROM tasks").fetchone()[0] == task_id
    assert not run(live, True, ("a.txt", "new upstream conflict\n"))
    newer = sh("git", "rev-parse", "HEAD", cwd=up)
    with connect_closing() as conn:
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
        assert newer in conn.execute("SELECT body FROM task_comments WHERE task_id = ?", (task_id,)).fetchone()[0]
        # A finished owner cannot swallow the next occurrence.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (task_id,))
    assert not run(live, True)
    with connect_closing() as conn:
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM tasks WHERE idempotency_key = ?", (key,)).fetchone()[0] == 1
    assert gate.staged() == []


def test_clean_merge_files_no_conflict_card(conflict_repo, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "board.db"))
    save_config({"platform_toolsets": {"cli": ["terminal", "file"]}, "terminal": {"backend": "local"}})
    _up, _fork, live, _gate = conflict_repo
    assert run(live, True, ("b.txt", "clean upstream\n"))
    with connect_closing() as conn:
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
