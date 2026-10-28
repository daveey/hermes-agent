"""Regression tests: a healthy kanban.db is never quarantined because of a stale
in-process view, SQLITE_IOERR* is transient, and quarantine never close()s a live
database file (which cancels the process's POSIX locks and strands it on a deleted
WAL generation)."""

from __future__ import annotations

import gc
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import sqlite_safe_read as ssr


@pytest.fixture(autouse=True)
def _release_reader_fds():
    """Leave no reader fd on a deleted sidecar behind for later test modules' /proc scans."""
    yield
    gc.collect()  # finalize unreferenced connections so their last close unlinks -wal/-shm first
    ssr.release_unlinked_fds()


def _board(tmp_path: Path, tasks: int = 8) -> Path:
    db = tmp_path / "kanban.db"
    kb._INITIALIZED_PATHS.discard(str(db.resolve()))
    kb.init_db(db_path=db)
    with kbc.connect_closing(db_path=db) as conn:
        for i in range(tasks):
            kb.create_task(conn, title=f"t{i}")
    kb._INITIALIZED_PATHS.discard(str(db.resolve()))
    return db


def _sidecars(db: Path) -> list[str]:
    return sorted(p.name for p in db.parent.iterdir() if p.name.startswith(db.name + "-"))


def _sibling_open_write_close(db: Path) -> None:
    subprocess.run(
        [sys.executable, "-c",
         "import sqlite3,sys;c=sqlite3.connect(sys.argv[1]);c.execute('create table if not exists z(a)');"
         "c.execute('insert into z values(1)');c.commit();c.close()", str(db)],
        check=True,
    )


class _IoErr(sqlite3.OperationalError):
    sqlite_errorcode = 10 | (2 << 8)  # SQLITE_IOERR_SHORT_READ
    sqlite_errorname = "SQLITE_IOERR_SHORT_READ"


# --- 1. IOERR is transient --------------------------------------------------------------

def test_ioerr_extended_codes_are_transient():
    for extended in (10, 10 | (1 << 8), 10 | (2 << 8), 10 | (15 << 8), 10 | (23 << 8)):
        exc = sqlite3.OperationalError("disk I/O error")
        exc.sqlite_errorcode = extended
        assert kbc._is_transient_sqlite_error(exc)
    corrupt = sqlite3.DatabaseError("database disk image is malformed")
    corrupt.sqlite_errorcode = 11
    assert not kbc._is_transient_sqlite_error(corrupt)


def test_transient_ioerr_retries_then_succeeds_and_never_quarantines(tmp_path, monkeypatch):
    db = _board(tmp_path)
    monkeypatch.setattr(kbc, "_PROBE_TRANSIENT_BACKOFF_SECONDS", (0.0,))
    real = kbc._run_integrity_check
    calls = {"n": 0}

    def flaky(conn):
        calls["n"] += 1
        if calls["n"] < 3:
            raise _IoErr("disk I/O error")
        return real(conn)

    monkeypatch.setattr(kbc, "_run_integrity_check", flaky)
    kbc.connect_closing(db_path=db).__enter__().close()
    assert calls["n"] == 3
    assert not list(tmp_path.glob("kanban.db.corrupt.*"))


def test_persistent_ioerr_propagates_raw_without_quarantine(tmp_path, monkeypatch):
    db = _board(tmp_path)
    monkeypatch.setattr(kbc, "_PROBE_TRANSIENT_BACKOFF_SECONDS", (0.0,))

    def always(conn):
        raise _IoErr("disk I/O error")

    monkeypatch.setattr(kbc, "_run_integrity_check", always)
    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        kbc.connect(db_path=db)
    assert not list(tmp_path.glob("kanban.db.corrupt.*"))


def test_ioerr_raised_as_databaseerror_is_not_quarantined(tmp_path, monkeypatch):
    db = _board(tmp_path)

    def boom(path):
        exc = sqlite3.DatabaseError("disk I/O error")
        exc.sqlite_errorcode = 10 | (3 << 8)
        raise exc

    monkeypatch.setattr(kbc, "_probe_integrity", boom)
    with pytest.raises(sqlite3.DatabaseError):
        kbc._probe_for_corruption(db.resolve())
    assert not list(tmp_path.glob("kanban.db.corrupt.*"))


# --- 2. quarantine never close()s the live file ----------------------------------------

def test_backup_does_not_cancel_locks_or_strand_the_process(tmp_path):
    """A live connection that the registry cannot see (untracked, like a read-only
    cache) keeps its WAL generation across a quarantine copy: a sibling's last close
    must not unlink -wal/-shm out from under it."""
    db = _board(tmp_path)
    live = sqlite3.connect(db)  # untracked on purpose
    live.execute("PRAGMA journal_mode=wal")
    live.execute("SELECT count(*) FROM tasks").fetchone()
    assert {"kanban.db-wal", "kanban.db-shm"} <= set(_sidecars(db))

    backup = kbc._backup_corrupt_db(db)
    assert backup is not None and backup.exists()
    assert (tmp_path / (backup.name + "-wal")).exists() or (tmp_path / (backup.name + "-shm")).exists()

    _sibling_open_write_close(db)
    assert {"kanban.db-wal", "kanban.db-shm"} <= set(_sidecars(db)), "sibling close unlinked a live generation"
    assert kbc._stranded_sidecars(db.resolve()) == []
    assert live.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    live.close()


def _deleted_sidecar_fds(db: Path) -> list[str]:
    base = str(db.resolve())
    out = []
    for name in os.listdir("/proc/self/fd"):
        try:
            target = os.readlink(f"/proc/self/fd/{name}")
        except OSError:
            continue
        if target in (base + "-wal (deleted)", base + "-shm (deleted)"):
            out.append(target)
    return out


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="needs /proc")
def test_reader_fds_never_pin_or_fake_a_deleted_generation(tmp_path):
    """The quarantine copy's cached pread fds must not keep a dead -wal/-shm generation open
    once SQLite unlinks it: that leaks disk and makes every /proc fd scan (kbc and
    kanban_lockguard.stranded_sidecars) report a strand that does not exist. While the
    generation is still live the sweep must NOT close them (that would cancel locks)."""
    from hermes_cli import kanban_lockguard as klg

    db = _board(tmp_path)
    live = sqlite3.connect(db)  # untracked on purpose
    live.execute("PRAGMA journal_mode=wal")
    live.execute("SELECT count(*) FROM tasks").fetchone()
    assert kbc._backup_corrupt_db(db) is not None
    held = set(ssr.lock_safe_fds())  # sweeps; sidecars still linked -> kept open
    assert held and all(os.fstat(fd).st_nlink > 0 for fd in held)
    assert not [fd for fd in held if os.readlink(f"/proc/self/fd/{fd}").endswith(("-wal", "-shm"))], \
        "quarantine left an in-process reader fd on a sidecar"
    _sibling_open_write_close(db)
    assert {"kanban.db-wal", "kanban.db-shm"} <= set(_sidecars(db)), "sweep cancelled live locks"

    live.close()  # last connection: SQLite checkpoints and unlinks -wal/-shm
    assert _sidecars(db) == []
    assert klg.stranded_sidecars(db) == []
    assert kbc._stranded_sidecars(db.resolve()) == []
    assert _deleted_sidecar_fds(db) == [], "reader fds still pin the deleted generation"


def test_raw_open_close_is_the_hazard_this_guards_against(tmp_path):
    """Control: the old behaviour (plain open()/close() of main + sidecars) DOES strand
    the process, so the test above is meaningful."""
    db = _board(tmp_path)
    live = sqlite3.connect(db)
    live.execute("PRAGMA journal_mode=wal")
    live.execute("SELECT count(*) FROM tasks").fetchone()
    for name in ("kanban.db", "kanban.db-shm", "kanban.db-wal"):
        with open(tmp_path / name, "rb") as handle:
            handle.read(16)
    _sibling_open_write_close(db)
    assert _sidecars(db) == []
    assert kbc._stranded_sidecars(db.resolve()), "expected deleted WAL/SHM generation held by this process"
    live.close()


def test_header_probe_is_lock_safe(tmp_path):
    db = _board(tmp_path)
    live = sqlite3.connect(db)
    live.execute("PRAGMA journal_mode=wal")
    live.execute("SELECT count(*) FROM tasks").fetchone()
    assert ssr.read_header_lock_safe(db, 16) == b"SQLite format 3\x00"
    kbc._validate_sqlite_header(db)
    _sibling_open_write_close(db)
    assert {"kanban.db-wal", "kanban.db-shm"} <= set(_sidecars(db))
    live.close()


def test_lock_safe_helpers_copy_and_hash_match(tmp_path):
    src = tmp_path / "a.bin"
    src.write_bytes(os.urandom(3_000_000))
    dst = tmp_path / "b.bin"
    assert ssr.copy_file_lock_safe(src, dst, chunk=65536)
    assert dst.read_bytes() == src.read_bytes()
    dst2 = tmp_path / "c.bin"
    assert ssr.copy_file_out_of_process(src, dst2)
    assert dst2.read_bytes() == src.read_bytes()
    assert not ssr.copy_file_out_of_process(tmp_path / "missing", tmp_path / "d.bin")
    import hashlib

    assert ssr.sha256_lock_safe(src) == hashlib.sha256(src.read_bytes()).hexdigest()
    assert ssr.sha256_lock_safe(tmp_path / "missing") is None


# --- 3. quarantine only on a genuine integrity failure ---------------------------------

def test_stale_view_failure_does_not_quarantine_healthy_board(tmp_path, monkeypatch):
    """The in-process probe reports corruption (stale generation) but a fresh process
    finds the file healthy: no quarantine, no REINDEX, transient error."""
    db = _board(tmp_path)

    def stale(path):
        return ["row 7 missing from index idx_tasks_status", "wrong # of entries in index idx_tasks_status"]

    monkeypatch.setattr(kbc, "_probe_integrity", stale)
    with pytest.raises(kbc.KanbanDbUnreliableViewError):
        kbc.connect(db_path=db)
    assert not list(tmp_path.glob("kanban.db.corrupt.*"))
    # still perfectly healthy for a fresh process
    assert sqlite3.connect(db).execute("PRAGMA integrity_check").fetchall() == [("ok",)]


def test_stranded_process_is_never_quarantined(tmp_path, monkeypatch):
    db = _board(tmp_path)
    monkeypatch.setattr(kbc, "_probe_integrity", lambda p: ["Tree 5 page 5: bad ptr"])
    monkeypatch.setattr(kbc, "_stranded_sidecars", lambda p: [str(p) + "-wal"])
    called = []
    monkeypatch.setattr(kbc, "_independent_integrity", lambda p: called.append(p) or ("messages", ["x"]))
    with pytest.raises(kbc.KanbanDbUnreliableViewError, match="deleted WAL/SHM generation"):
        kbc.connect(db_path=db)
    assert not called, "stranded detection must short-circuit the expensive independent check"
    assert not list(tmp_path.glob("kanban.db.corrupt.*"))


def test_genuine_corruption_is_still_quarantined(tmp_path):
    db = tmp_path / "kanban.db"
    db.write_bytes(b"SQLite format 3\x00" + b"\xff" * 8192)
    kb._INITIALIZED_PATHS.discard(str(db.resolve()))
    with pytest.raises(kbc.KanbanDbCorruptError) as info:
        kbc.connect(db_path=db)
    assert info.value.backup_path is not None and info.value.backup_path.exists()
    assert db.exists(), "original must be preserved"


def test_genuine_index_corruption_still_auto_repairs(tmp_path):
    from tests.hermes_cli.test_kanban_db_repair import _build_board_db, _corrupt_index

    db = tmp_path / "kanban.db"
    _build_board_db(db)
    with closing(sqlite3.connect(db)) as conn:
        index = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL LIMIT 1").fetchone()[0]
    _corrupt_index(db, index)
    kbc.connect_closing(db_path=db).__enter__().close()
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    assert list(tmp_path.glob("kanban.db.corrupt.*.bak"))
