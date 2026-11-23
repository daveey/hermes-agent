"""Post-commit cues never publish a savepoint or turn success into replay. [daveey]"""
import sqlite3

import pytest

from hermes_cli import fact_source_signal as signals
from hermes_cli import kanban_db_connect as kbc
from tests.hermes_cli import test_kanban_task_change as task_helpers
from tests.hermes_state import test_session_change_cursor as session_helpers

home = task_helpers.home
board = task_helpers.board
store = session_helpers.store


def test_task_cue_only_after_outer_commit(board, monkeypatch):
    seen = []
    def publish(path):
        assert not board.in_transaction
        with sqlite3.connect(path) as reader:
            seen.append(reader.execute("SELECT title FROM tasks WHERE id='cue'").fetchone()[0])
    monkeypatch.setattr(signals, "publish_commit", publish)
    with kbc.write_txn(board):
        with kbc.write_txn(board, allow_nested=True):
            board.execute("INSERT INTO tasks(id,title,assignee,status,created_at) VALUES('cue','yes','w','ready',1)")
        assert seen == []
    assert seen == ["yes"]
    with pytest.raises(ValueError):
        with kbc.write_txn(board):
            board.execute("UPDATE tasks SET title='rolled-back' WHERE id='cue'")
            raise ValueError("rollback")
    assert seen == ["yes"]


def test_session_cue_reads_committed_state_and_not_rollback(store, monkeypatch):
    db, reader, _path = store
    db.create_session("a", "cli")
    seen = []
    monkeypatch.setattr(signals, "publish_commit", lambda p: seen.append(
        reader.execute("SELECT input_tokens FROM sessions WHERE id='a'").fetchone()[0]))
    db._execute_write(lambda conn: conn.execute("UPDATE sessions SET input_tokens=2 WHERE id='a'"))
    assert seen == [2]
    def rollback(conn):
        conn.execute("UPDATE sessions SET input_tokens=3 WHERE id='a'")
        raise ValueError("rollback")
    with pytest.raises(ValueError):
        db._execute_write(rollback)
    assert seen == [2]


@pytest.mark.platforms("linux")
def test_signal_failure_preserves_success_and_does_not_follow_symlink(tmp_path, caplog):
    path = tmp_path / "state.db"
    target = tmp_path / "unrelated"
    target.write_text("unchanged")
    before = target.stat().st_mtime_ns
    signals.cue_path(path).symlink_to(target)
    signals.publish_commit(path)
    assert target.stat().st_mtime_ns == before
    assert "post-commit fact cue unavailable" in caplog.text


@pytest.mark.platforms("linux")
def test_signal_does_not_recreate_removed_profile(tmp_path, caplog):
    signals.publish_commit(tmp_path / "gone-profile" / "state.db")
    assert not (tmp_path / "gone-profile").exists()
    assert "post-commit fact cue unavailable" in caplog.text
