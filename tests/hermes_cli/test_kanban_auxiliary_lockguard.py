"""Auxiliary board readers must survive raw I/O while they hold a WAL view."""
from contextlib import closing
import sqlite3
import subprocess
import sys

import pytest

from hermes_cli import kanban_worktree_gc as gc
from hermes_cli import kanban_placement_forward as placement
from hermes_cli import kanban_lockguard


@pytest.fixture
def board(tmp_path):
    path = tmp_path / "kanban.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE tasks (id TEXT)")
        conn.execute("INSERT INTO tasks VALUES ('one')")
        conn.commit()
    return path


def _assert_reader_survives(conn, path):
    assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
    for suffix in ("", "-shm"):
        with open(str(path) + suffix, "rb") as stream:
            stream.read(16)
    subprocess.run(
        [sys.executable, "-c", "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); "
         "c.execute(\"INSERT INTO tasks (id) VALUES ('two')\"); c.commit(); c.close()", str(path)],
        check=True, timeout=20,
    )
    assert not kanban_lockguard.stranded_sidecars(path)
    assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 2


@pytest.mark.platforms("linux")
def test_gc_reader_survives_stray_close(board):
    with gc._read_board(board) as conn:
        _assert_reader_survives(conn, board)


@pytest.mark.platforms("linux")
def test_placement_reader_survives_stray_close(board, monkeypatch):
    from types import SimpleNamespace
    from hermes_cli import kanban_db as kb

    monkeypatch.setattr(kb, "kanban_db_path", lambda **kwargs: board)

    def read_state(conn, task_id):
        _assert_reader_survives(conn, board)
        return {"target": "hub"}

    monkeypatch.setattr(placement, "state", read_state)
    assert placement.forward_argv(SimpleNamespace(id="one", skills=[]), None, ["hermes"])


def _strand(path):
    for suffix in ("", "-shm"):
        with open(str(path) + suffix, "rb") as stream:
            stream.read(16)
    subprocess.run(
        [sys.executable, "-c", "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); "
         "c.execute(\"INSERT INTO tasks (id) VALUES ('two')\"); c.commit(); c.close()", str(path)],
        check=True, timeout=20,
    )
    assert kanban_lockguard.stranded_sidecars(path)


@pytest.mark.platforms("linux")
def test_recovery_retires_all_alias_caches_and_idle_board_handles(board, monkeypatch):
    from hermes_cli import kanban_session_activity as activity
    alias = board.with_name("alias.db")
    alias.symlink_to(board)
    activity._reset_cache_for_tests()
    reader = None
    try:
        with monkeypatch.context() as m:
            m.setattr(kanban_lockguard, "guard", lambda conn: False)
            reader = kanban_lockguard.connect(board)
            reader.execute("SELECT * FROM tasks").fetchall()
            for path in (board, alias):
                with activity.cached_ro(path) as conn:
                    conn.execute("SELECT * FROM tasks").fetchall()
        _strand(board)
        with activity.cached_ro(board) as conn:
            assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 2
        assert not kanban_lockguard.stranded_sidecars(board)
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            reader.execute("SELECT 1")
    finally:
        if reader is not None:
            reader.close()
        activity._reset_cache_for_tests()


@pytest.mark.platforms("linux")
def test_recovery_does_not_rollback_a_live_transaction(board, monkeypatch):
    from hermes_cli.kanban_generation import DeletedWalGenerationError
    with monkeypatch.context() as m:
        m.setattr(kanban_lockguard, "guard", lambda conn: False)
        reader = kanban_lockguard.connect(board)
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM tasks").fetchall()
    try:
        _strand(board)
        with pytest.raises(DeletedWalGenerationError):
            kanban_lockguard.connect(board)
        assert reader.in_transaction
    finally:
        reader.rollback()
        reader.close()
    with closing(kanban_lockguard.connect(board)) as fresh:
        assert fresh.execute("SELECT count(*) FROM tasks").fetchone()[0] == 2


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("registered", [False, True])
def test_unregistered_stranded_peer_refuses_retries_and_ro_fallback(board, monkeypatch, registered):
    from hermes_cli.kanban_generation import DeletedWalGenerationError
    raw = sqlite3.connect(board)
    raw.execute("SELECT * FROM tasks").fetchall()
    peer = None
    try:
        if registered:
            with monkeypatch.context() as m:
                m.setattr(kanban_lockguard, "guard", lambda conn: False)
                peer = kanban_lockguard.connect(board)
        _strand(board)
        for _ in range(2):
            with pytest.raises(DeletedWalGenerationError):
                kanban_lockguard.connect(board)
        with pytest.raises(DeletedWalGenerationError):
            with gc._read_board(board):
                pass
    finally:
        raw.close()
        if peer is not None:
            peer.close()
    with closing(kanban_lockguard.connect(board)) as conn:
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 2


@pytest.mark.platforms("linux")
def test_foreign_thread_handle_refused_until_owner_closes(board, monkeypatch):
    import threading
    from hermes_cli.kanban_generation import DeletedWalGenerationError
    opened, release = threading.Event(), threading.Event()
    errors = []

    def owner():
        try:
            with closing(kanban_lockguard.connect(board)) as conn:
                conn.execute("SELECT * FROM tasks").fetchall()
                opened.set()
                assert release.wait(10)
        except BaseException as exc:
            errors.append(exc)
            opened.set()

    with monkeypatch.context() as m:
        m.setattr(kanban_lockguard, "guard", lambda conn: False)
        thread = threading.Thread(target=owner)
        thread.start()
        assert opened.wait(10)
    try:
        assert not errors
        _strand(board)
        with pytest.raises(DeletedWalGenerationError, match="foreign-thread"):
            kanban_lockguard.connect(board)
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive()
    assert not errors
    with closing(kanban_lockguard.connect(board)) as conn:
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 2


@pytest.mark.platforms("linux")
def test_borrowed_cache_is_not_closed_or_waited_on_during_recovery(board, monkeypatch):
    from hermes_cli import kanban_session_activity as activity
    from hermes_cli.kanban_generation import DeletedWalGenerationError
    activity._reset_cache_for_tests()
    try:
        with monkeypatch.context() as m:
            m.setattr(kanban_lockguard, "guard", lambda conn: False)
            with activity.cached_ro(board) as borrowed:
                borrowed.execute("SELECT * FROM tasks").fetchall()
                _strand(board)
                with pytest.raises(DeletedWalGenerationError, match="borrowed"):
                    kanban_lockguard.connect(board)
                # Still open (never closed under the borrower, even on this same thread).
                assert not borrowed.in_transaction
        with activity.cached_ro(board) as conn:
            assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 2
        assert not kanban_lockguard.stranded_sidecars(board)
    finally:
        activity._reset_cache_for_tests()


@pytest.mark.platforms("linux")
def test_gc_write_handle_survives_stray_close(board, monkeypatch):
    from types import SimpleNamespace
    from hermes_cli import kanban_db as kb
    with closing(sqlite3.connect(board)) as conn:
        conn.execute("ALTER TABLE tasks ADD COLUMN workspace_path TEXT")
    monkeypatch.setattr(gc, "retention_days", lambda: 7)
    monkeypatch.setattr(gc.ws, "_sibling_board_db_files", lambda conn: [board])
    monkeypatch.setattr(kb, "kanban_db_path", lambda: board)
    monkeypatch.setattr(gc.ws, "_git", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=""))
    calls = []

    def collect(conn, **kwargs):
        _assert_reader_survives(conn, board)
        calls.append(True)

    monkeypatch.setattr(gc, "gc_task_worktrees", collect)
    gc._sweep_repo(str(board.parent))
    assert calls == [True]


@pytest.mark.platforms("linux")
def test_imported_backup_guards_source_and_preserves_snapshot(board, tmp_path, monkeypatch):
    from hermes_cli.backup_sqlite import _safe_copy_db
    real_connect = kanban_lockguard.connect
    calls = []

    def checked(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        assert kanban_lockguard.is_guarded(conn)
        _assert_reader_survives(conn, board)
        calls.append(True)
        return conn

    monkeypatch.setattr(kanban_lockguard, "connect", checked)
    destination = tmp_path / "snapshot.db"
    assert _safe_copy_db(board, destination)
    assert calls == [True]
    with closing(sqlite3.connect(destination)) as snapshot:
        assert snapshot.execute("SELECT count(*) FROM tasks").fetchone()[0] == 2


@pytest.mark.platforms("linux")
def test_preopen_probe_does_not_drop_untracked_readers_locks(board):
    from hermes_cli.sqlite_safe_read import read_header_bytes_preopen
    with closing(sqlite3.connect(board)) as reader:
        reader.execute("SELECT * FROM tasks").fetchall()
        assert read_header_bytes_preopen(board, length=16) == b"SQLite format 3\x00"
        subprocess.run(
            [sys.executable, "-c", "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); "
             "c.execute(\"INSERT INTO tasks (id) VALUES ('two')\"); c.commit(); c.close()", str(board)],
            check=True, timeout=20,
        )
        assert not kanban_lockguard.stranded_sidecars(board)
        assert reader.execute("SELECT count(*) FROM tasks").fetchone()[0] == 2
