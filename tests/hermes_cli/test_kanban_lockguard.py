"""Kanban connections survive a stray in-process close of the board files (kanban-759104eb56).

POSIX advisory locks are per process: any raw ``open()``/``close()`` of ``kanban.db`` and its
``-shm`` inside a process cancels every lock SQLite holds there (sqlite.org/howtocorrupt §2.2).
The next sibling process that closes the board then believes it is the last user, checkpoints
and unlinks ``-wal``/``-shm``, and this process is stranded on the deleted generation: every
later read fails with ``disk I/O error`` or reports false corruption until restart.

These tests reproduce that sequence on a scratch board through each production opener:
``kanban_db_connect.connect`` (tools, dispatcher, dashboard), ``kanban_session_activity._open_ro``
and ``cached_ro`` (session-list counts) and the generic ``kanban_lockguard.connect`` used by the
remaining read-only helpers.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="needs /proc and OFD locks")


def _make_wal_board(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT NOT NULL, session_id TEXT)")
    conn.execute("INSERT INTO tasks VALUES ('t1', 'running', 's1')")
    conn.commit()
    conn.close()


def _sibling_write_and_close(path: Path, task_id: str) -> None:
    """Another process writes a row and closes: if it thinks it is the last user it checkpoints and
    unlinks -wal/-shm (what the gateway/CLI/worker processes do all day)."""
    code = (
        "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); "
        "c.execute(\"INSERT INTO tasks (id, status, session_id) VALUES (?, 'ready', 's2')\", (sys.argv[2],)); "
        "c.commit(); c.close()"
    )
    subprocess.run([sys.executable, "-c", code, str(path), task_id], check=True, timeout=30)


def _stray_raw_copy(path: Path) -> None:
    """What the corrupt-backup quarantine (and any tool copying ~/.hermes) does in-process: plain
    open/read/close of the db and its sidecars, cancelling this process's POSIX locks."""
    for suffix in ("", "-wal", "-shm"):
        src = Path(str(path) + suffix)
        if src.exists():
            shutil.copy2(src, str(path) + ".copy" + suffix)


def _deleted_sidecars(path: Path) -> list[str]:
    """Open fds of this process that point at a deleted ``-wal``/``-shm`` of the board at ``path``.

    Match the board's own sidecar paths exactly, not a ``-wal``/``-shm`` substring: unrelated deleted
    fds (pytest's O_TMPFILE capture files read back as ``<TMPDIR>/#<inode> (deleted)``) would otherwise
    count whenever a random temp dir name contains ``-shm`` (CI saw ``r-shmza7pj``)."""
    base = os.path.realpath(path)
    wanted = {f"{base}{suffix} (deleted)" for suffix in ("-wal", "-shm")}
    out = []
    for fd in os.listdir("/proc/self/fd"):
        try:
            target = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            continue
        if target in wanted:
            out.append(target)
    return out


def _assert_survives(conn: sqlite3.Connection, path: Path) -> None:
    rows = conn.execute("SELECT count(*) FROM tasks").fetchone()[0]
    assert rows == 1
    _stray_raw_copy(path)
    _sibling_write_and_close(path, "t2")
    assert Path(str(path) + "-wal").exists(), "a sibling's close deleted -wal under a live connection"
    assert Path(str(path) + "-shm").exists(), "a sibling's close deleted -shm under a live connection"
    assert _deleted_sidecars(path) == [], "this process is stranded on a deleted WAL/SHM generation"
    # The live connection still sees the sibling's committed write (not a stale snapshot / IOERR).
    assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 2
    # And a second sibling write after more stray I/O is still visible.
    _stray_raw_copy(path)
    _sibling_write_and_close(path, "t3")
    assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 3


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HUB_STATE", str(tmp_path / "hub-state"))
    path = tmp_path / "kanban.db"
    _make_wal_board(path)
    return path


def test_control_raw_sqlite_connection_is_stranded(board):
    """Control: an unguarded connection IS stranded by this sequence (proves the repro is real)."""
    conn = sqlite3.connect(f"file:{board}?mode=ro", uri=True)
    try:
        conn.execute("SELECT count(*) FROM tasks").fetchone()
        _stray_raw_copy(board)
        _sibling_write_and_close(board, "t2")
        assert not Path(str(board) + "-wal").exists() or _deleted_sidecars(board)
    finally:
        conn.close()


def test_deleted_sidecars_ignores_unrelated_deleted_files(tmp_path):
    """Regression: a deleted file under a directory whose name contains ``-shm``/``-wal`` is not a
    stranded board sidecar (CI flake: random TMPDIR ``r-shmza7pj`` held pytest's capture files)."""
    import tempfile

    odd = tmp_path / "r-shmx-walx"
    odd.mkdir()
    with tempfile.TemporaryFile(dir=odd), open(odd / "other.db-shm", "w") as stray:
        os.unlink(stray.name)
        assert _deleted_sidecars(odd / "kanban.db") == []


def test_session_activity_open_ro_survives_stray_close(board):
    from hermes_cli import kanban_session_activity as ksa

    conn = ksa._open_ro(board)
    try:
        _assert_survives(conn, board)
    finally:
        conn.close()


def test_session_activity_cached_ro_survives_stray_close(board):
    from hermes_cli import kanban_session_activity as ksa

    ksa._reset_cache_for_tests()
    try:
        with ksa.cached_ro(board) as conn:
            _assert_survives(conn, board)
    finally:
        ksa._reset_cache_for_tests()


def test_kanban_db_connect_survives_stray_close(board):
    from hermes_cli import kanban_db_connect as kbc

    conn = kbc.connect(board)
    try:
        conn.execute("DELETE FROM tasks WHERE id NOT IN ('t1')")
        _assert_survives(conn, board)
    finally:
        conn.close()


def test_generic_guarded_connect_survives_stray_close(board):
    from hermes_cli import kanban_lockguard

    conn = kanban_lockguard.connect(f"file:{board}?mode=ro", uri=True, timeout=1)
    try:
        _assert_survives(conn, board)
    finally:
        conn.close()


def test_guarded_last_close_still_ends_the_generation(board):
    """The guard must not keep -wal around forever: a true last close still checkpoints and
    removes the sidecars (release-before-close), so a later file replace never pairs with a
    stale WAL."""
    from hermes_cli import kanban_lockguard

    conn = kanban_lockguard.connect(str(board), timeout=1)
    conn.execute("INSERT INTO tasks VALUES ('t9', 'ready', 's9')")
    conn.commit()
    assert Path(str(board) + "-wal").exists()
    conn.close()
    assert not Path(str(board) + "-wal").exists()
    assert not Path(str(board) + "-shm").exists()


def test_guarded_connection_does_not_block_other_writers(board):
    """The guard is a read lock on SQLite's SHARED range: sibling writers keep committing while
    a guarded reader is open, and the reader's own process can write through another handle."""
    from hermes_cli import kanban_lockguard

    reader = kanban_lockguard.connect(f"file:{board}?mode=ro", uri=True, timeout=1)
    writer = kanban_lockguard.connect(str(board), timeout=1)
    try:
        reader.execute("SELECT 1 FROM tasks").fetchall()
        writer.execute("INSERT INTO tasks VALUES ('w1', 'ready', 's')")
        writer.commit()
        _sibling_write_and_close(board, "w2")
        assert reader.execute("SELECT count(*) FROM tasks").fetchone()[0] == 3
    finally:
        reader.close()
        writer.close()


def test_rollback_journal_db_is_not_guarded(tmp_path):
    """A read lock on the SHARED range would block every rollback-journal writer: only WAL boards
    get the guard."""
    from hermes_cli import kanban_lockguard

    path = tmp_path / "plain.db"
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE t (x)")
    c.commit()
    c.close()
    reader = kanban_lockguard.connect(str(path), timeout=1)
    try:
        assert not kanban_lockguard.is_guarded(reader)
        _w = subprocess.run(
            [sys.executable, "-c",
             "import sqlite3,sys; c=sqlite3.connect(sys.argv[1], timeout=2); "
             "c.execute('INSERT INTO t VALUES (1)'); c.commit()", str(path)],
            timeout=30, capture_output=True, text=True, check=False)
        assert _w.returncode == 0, _w.stderr
    finally:
        reader.close()


def test_stranded_cached_reader_reopens(board, monkeypatch):
    """Detection + recovery: a cached reader stranded on a deleted generation (opened unguarded,
    as on old code) is detected on its next use and reopened, instead of failing (or reading stale
    rows) until restart."""
    from hermes_cli import kanban_lockguard
    from hermes_cli import kanban_session_activity as ksa

    ksa._reset_cache_for_tests()
    try:
        with monkeypatch.context() as m:
            m.setattr(kanban_lockguard, "guard", lambda conn, **kwargs: False)  # the pre-fix, unguarded reader
            with ksa.cached_ro(board) as conn:
                assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
        _stray_raw_copy(board)
        _sibling_write_and_close(board, "t2")
        assert kanban_lockguard.stranded_sidecars(board), "repro did not strand the process"
        with ksa.cached_ro(board) as conn:
            assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 2
        assert kanban_lockguard.stranded_sidecars(board) == []
    finally:
        ksa._reset_cache_for_tests()


def test_kanban_modules_open_boards_only_through_the_guard():
    """Every hermes_cli/kanban*.py board connection must go through the guard; a raw
    sqlite3.connect() to a board is exactly the unguarded handle that gets stranded."""
    root = Path(__file__).resolve().parents[2] / "hermes_cli"
    allowed = {
        # state.db / hub.db / in-memory / offline copies, not live boards
        "kanban_worker_recovery.py", "kanban_transfer.py",
        "kanban_origin_ledger.py", "kanban_lockguard.py",
        # Origin-local receipt and decision journals, never Kanban board connections.
        "kanban_origin_forward.py", "kanban_origin_forward_decision.py",
        # connects only inside its own short-lived child (`python -m hermes_cli.kanban_db_recover`);
        # in-process callers (kanban_ops, the dispatcher) reach it only through spawn_recovery()
        "kanban_db_recover.py",
    }
    offenders = []
    for path in sorted(root.glob("kanban*.py")):
        if path.name in allowed:
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if "sqlite3.connect(" in line and "noqa: kanban-lockguard" not in line \
                    and not line.lstrip().startswith(("#", '"', "'", "``")):
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    assert offenders == []
