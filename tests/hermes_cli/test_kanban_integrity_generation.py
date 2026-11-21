"""Generation-bound integrity stamp: a stamp may only skip the full scan for the exact DB/WAL generation it certified.

Behavioral tests through the public ``connect`` entry point (full guard path), plus the stamp helpers where a race
has to be injected deterministically.
"""
import json
import os
import shutil
import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    for key in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_RUN_ID"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(kbc, "_INTEGRITY_STAMP_TTL_SECONDS", 6 * 3600.0)
    path = home / "kanban.db"
    kbc.init_db(path)
    kbc._INITIALIZED_PATHS.discard(str(path.resolve()))
    yield path
    kbc._INITIALIZED_PATHS.discard(str(path.resolve()))


@pytest.fixture
def checks(monkeypatch):
    """Count full integrity scans."""
    calls = []
    real = kbc._run_integrity_check

    def counted(conn):
        calls.append(1)
        return real(conn)

    monkeypatch.setattr(kbc, "_run_integrity_check", counted)
    return calls


def _forget(path):
    kbc._INITIALIZED_PATHS.discard(str(path.resolve()))


def _stamp(path):
    return path.with_name(path.name + ".integrity-ok")


def _stamped(path):
    """Run a full connect that must scan and leave a stamp; return with the process cache cleared."""
    _forget(path)
    kbc._guard_existing_db_is_healthy(path)
    _forget(path)
    assert _stamp(path).exists(), "clean scan of a quiescent DB must stamp"
    assert kbc._integrity_stamp_fresh(path.resolve())
    return path


def _fresh(path):
    return kbc._integrity_stamp_fresh(path.resolve())


def _connect_scans(path, checks):
    _forget(path)
    before = len(checks)
    kbc.connect(path).close()
    return len(checks) - before


def test_valid_stamp_skips_scan_on_unchanged_db(board, checks):
    _stamped(board)
    assert _connect_scans(board, checks) == 0


def test_same_inode_corruption_after_stamp_fails_closed(board, checks):
    _stamped(board)
    ino = board.stat().st_ino
    # a real tasks-table page must be damaged: overwrite every non-header page
    size = board.stat().st_size
    assert size >= 8192
    with open(board, "r+b") as f:
        f.seek(4096)
        f.write(b"\xff" * (size - 4096))
    assert board.stat().st_ino == ino
    _forget(board)
    with pytest.raises((kbc.KanbanDbCorruptError, sqlite3.DatabaseError)):
        kbc.connect(board).close()


def test_same_inode_corruption_with_restored_mtime_fails_closed(board, checks):
    _stamped(board)
    st = board.stat()
    with open(board, "r+b") as f:
        f.seek(4096)
        f.write(b"\xff" * (st.st_size - 4096))
        f.flush()
        os.fsync(f.fileno())
    os.utime(board, ns=(st.st_atime_ns, st.st_mtime_ns))
    after = board.stat()
    assert (after.st_ino, after.st_size, after.st_mtime_ns) == (st.st_ino, st.st_size, st.st_mtime_ns)
    assert not _fresh(board), "ctime must invalidate an in-place write that restored mtime"
    _forget(board)
    with pytest.raises((kbc.KanbanDbCorruptError, sqlite3.DatabaseError)):
        kbc.connect(board).close()


def test_same_size_garbage_in_place_restored_mtime_not_fresh(board):
    _stamped(board)
    st = board.stat()
    with open(board, "r+b") as f:
        f.seek(st.st_size - 16)
        f.write(b"\x00" * 16)
    os.utime(board, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert not _fresh(board)


def test_wal_only_ordinary_mutation_invalidates(board, checks):
    _stamped(board)
    holder = sqlite3.connect(board)
    try:
        holder.execute("PRAGMA journal_mode=WAL")
        holder.execute("PRAGMA wal_autocheckpoint=0")
        main_before = board.stat()
        holder.execute("CREATE TABLE IF NOT EXISTS walmut (x)")
        holder.execute("INSERT INTO walmut VALUES (1)")
        holder.commit()
        wal = Path(str(board) + "-wal")
        assert wal.exists() and wal.stat().st_size > 0
        assert board.stat().st_size == main_before.st_size  # change lives only in the WAL
        assert not _fresh(board)
        # and a later WAL-only write changes it again
        stamped_gen = kbc._integrity_generation(board.resolve())
        holder.execute("INSERT INTO walmut VALUES (2)")
        holder.commit()
        assert kbc._integrity_generation(board.resolve()) != stamped_gen
    finally:
        holder.close()


def test_connect_after_wal_mutation_rescans(board, checks):
    _stamped(board)
    holder = sqlite3.connect(board)
    try:
        holder.execute("PRAGMA wal_autocheckpoint=0")
        holder.execute("CREATE TABLE walmut2 (x)")
        holder.commit()
        assert _connect_scans(board, checks) >= 1
    finally:
        holder.close()


def test_db_replacement_invalidates(board, tmp_path):
    _stamped(board)
    twin = tmp_path / "twin.db"
    shutil.copyfile(board, twin)  # identical bytes, different inode
    os.replace(twin, board)
    assert not _fresh(board)


def test_db_replacement_with_copystat_invalidates(board, tmp_path):
    _stamped(board)
    twin = tmp_path / "twin.db"
    shutil.copy2(board, twin)
    os.replace(twin, board)
    assert not _fresh(board)


def test_wal_creation_invalidates(board):
    _stamped(board)
    wal = Path(str(board) + "-wal")
    assert not wal.exists()
    wal.write_bytes(b"")
    assert not _fresh(board)


def test_wal_removal_invalidates(board):
    # stamp while a WAL exists (a held-open WAL connection keeps it)
    holder = sqlite3.connect(board)
    try:
        holder.execute("PRAGMA wal_autocheckpoint=0")
        holder.execute("CREATE TABLE w (x)")
        holder.commit()
        wal = Path(str(board) + "-wal")
        assert wal.exists()
        resolved = board.resolve()
        gen = kbc._integrity_generation(resolved)
        assert gen is not None and gen["-wal"] is not None
        kbc._write_integrity_stamp(resolved, gen)
        assert _fresh(board)
        holder.close()  # last close checkpoints and removes the WAL
        if wal.exists():
            wal.unlink()
        assert not _fresh(board)
    finally:
        with pytest.raises(sqlite3.ProgrammingError):
            holder.execute("select 1")


def test_wal_truncation_invalidates(board):
    holder = sqlite3.connect(board)
    try:
        holder.execute("PRAGMA wal_autocheckpoint=0")
        holder.execute("CREATE TABLE w (x)")
        holder.executemany("INSERT INTO w VALUES (?)", [(i,) for i in range(200)])
        holder.commit()
        wal = Path(str(board) + "-wal")
        resolved = board.resolve()
        gen = kbc._integrity_generation(resolved)
        kbc._write_integrity_stamp(resolved, gen)
        assert _fresh(board)
        os.truncate(wal, wal.stat().st_size // 2)
        assert not _fresh(board)
    finally:
        holder.close()


def test_wal_replacement_invalidates(board, tmp_path):
    holder = sqlite3.connect(board)
    try:
        holder.execute("PRAGMA wal_autocheckpoint=0")
        holder.execute("CREATE TABLE w (x)")
        holder.commit()
        wal = Path(str(board) + "-wal")
        resolved = board.resolve()
        kbc._write_integrity_stamp(resolved, kbc._integrity_generation(resolved))
        assert _fresh(board)
        twin = tmp_path / "wal.copy"
        shutil.copyfile(wal, twin)
        os.replace(twin, wal)
        assert not _fresh(board)
    finally:
        holder.close()


def test_rollback_journal_disables_cache(board, checks):
    _stamped(board)
    journal = Path(str(board) + "-journal")
    journal.write_bytes(b"")
    assert kbc._integrity_generation(board.resolve()) is None
    assert not _fresh(board)
    # and a stamp cannot be written while a journal is present
    _stamp(board).unlink()
    kbc._write_integrity_stamp(board.resolve(), kbc._integrity_generation(board.resolve()))
    assert not _stamp(board).exists()


def test_mutation_during_probe_is_not_certified(board, monkeypatch):
    real = kbc._probe_for_corruption

    def racing(resolved):
        out = real(resolved)
        c = sqlite3.connect(resolved)
        c.execute("PRAGMA wal_autocheckpoint=0")
        c.execute("CREATE TABLE raced (x)")
        c.commit()
        c.close()
        return out

    monkeypatch.setattr(kbc, "_probe_for_corruption", racing)
    _forget(board)
    kbc._guard_existing_db_is_healthy(board)
    assert not _stamp(board).exists()
    assert not _fresh(board)


def test_wal_mutation_during_probe_with_held_writer_is_not_certified(board, monkeypatch):
    holder = sqlite3.connect(board)
    holder.execute("PRAGMA wal_autocheckpoint=0")
    real = kbc._probe_for_corruption

    def racing(resolved):
        out = real(resolved)
        holder.execute("CREATE TABLE raced_wal (x)")
        holder.commit()
        return out

    monkeypatch.setattr(kbc, "_probe_for_corruption", racing)
    try:
        _forget(board)
        kbc._guard_existing_db_is_healthy(board)
        assert not _stamp(board).exists()
        assert not _fresh(board)
    finally:
        holder.close()


def test_write_stamp_refuses_stale_before_generation(board):
    resolved = board.resolve()
    before = kbc._integrity_generation(resolved)
    c = sqlite3.connect(board)
    c.execute("CREATE TABLE changed (x)")
    c.commit()
    c.close()
    assert kbc._integrity_generation(resolved) != before
    kbc._write_integrity_stamp(resolved, before)
    assert not _stamp(board).exists()


def test_write_stamp_refuses_none_generation_and_zero_ttl(board, monkeypatch):
    resolved = board.resolve()
    kbc._write_integrity_stamp(resolved, None)
    assert not _stamp(board).exists()
    monkeypatch.setattr(kbc, "_INTEGRITY_STAMP_TTL_SECONDS", 0)
    kbc._write_integrity_stamp(resolved, kbc._integrity_generation(resolved))
    assert not _stamp(board).exists()


def _write_stamp(board, **over):
    resolved = board.resolve()
    doc = {"version": 2, "generation": kbc._integrity_generation(resolved),
           "verified_at": time.time(), "pid": 1}
    doc.update(over)
    _stamp(board).write_text(json.dumps(doc), encoding="utf-8")


def test_hand_built_good_stamp_is_fresh_control(board):
    _write_stamp(board)
    assert _fresh(board)


@pytest.mark.parametrize("raw", [
    "", "not json", "[]", "null", "{}", '{"version": 2}',
    '{"version": 2, "verified_at": "x", "generation": {}}',
    b"\xff\xfe\x00garbage",
])
def test_malformed_stamps_do_not_skip(board, raw):
    if isinstance(raw, bytes):
        _stamp(board).write_bytes(raw)
    else:
        _stamp(board).write_text(raw)
    assert not _fresh(board)


def test_legacy_v1_stamp_does_not_skip(board, checks):
    st = board.stat()
    _stamp(board).write_text(json.dumps({
        "version": 1, "dev": st.st_dev, "ino": st.st_ino, "size": st.st_size,
        "mtime_ns": st.st_mtime_ns, "verified_at": time.time(), "pid": 1}))
    assert not _fresh(board)
    assert _connect_scans(board, checks) >= 1


def test_unversioned_legacy_stamp_does_not_skip(board):
    st = board.stat()
    _stamp(board).write_text(json.dumps({
        "ino": st.st_ino, "size": st.st_size, "mtime_ns": st.st_mtime_ns, "verified_at": time.time()}))
    assert not _fresh(board)


def test_wrong_version_does_not_skip(board):
    for v in (0, 1, 3, "2", None):
        _write_stamp(board, version=v)
        assert not _fresh(board), v


def test_future_stamp_does_not_skip(board):
    _write_stamp(board, verified_at=time.time() + 3600)
    assert not _fresh(board)


def test_expired_stamp_does_not_skip(board):
    _write_stamp(board, verified_at=time.time() - 6 * 3600.0 - 5)
    assert not _fresh(board)


def test_generation_mismatch_does_not_skip(board):
    gen = kbc._integrity_generation(board.resolve())
    assert gen is not None
    gen[""][2] += 1
    _write_stamp(board, generation=gen)
    assert not _fresh(board)


def test_ttl_zero_never_skips(board, monkeypatch):
    _write_stamp(board)
    monkeypatch.setattr(kbc, "_INTEGRITY_STAMP_TTL_SECONDS", 0)
    assert not _fresh(board)


def test_stat_failure_does_not_skip(board, monkeypatch):
    _write_stamp(board)
    assert _fresh(board)
    real_stat = Path.stat

    def boom(self, *a, **k):
        if self.name.startswith("kanban.db") and not self.name.endswith(".integrity-ok"):
            raise PermissionError("denied")
        return real_stat(self, *a, **k)

    monkeypatch.setattr(Path, "stat", boom)
    assert kbc._integrity_generation(board.resolve()) is None
    assert not _fresh(board)
    kbc._write_integrity_stamp(board.resolve(), None)


def test_missing_db_generation_is_none(tmp_path):
    assert kbc._integrity_generation(tmp_path / "absent.db") is None


def test_corrupt_db_with_forged_current_stamp_still_detected_after_write(board):
    """A stamp forged for the pre-corruption generation cannot survive the corrupting write."""
    _write_stamp(board)
    assert _fresh(board)
    size = board.stat().st_size
    with open(board, "r+b") as f:
        f.seek(4096)
        f.write(b"\xff" * (size - 4096))
    assert not _fresh(board)
    _forget(board)
    with pytest.raises((kbc.KanbanDbCorruptError, sqlite3.DatabaseError)):
        kbc.connect(board).close()


def test_corrupt_db_is_never_stamped(board):
    size = board.stat().st_size
    with open(board, "r+b") as f:
        f.seek(4096)
        f.write(b"\xff" * (size - 4096))
    _forget(board)
    with pytest.raises((kbc.KanbanDbCorruptError, sqlite3.DatabaseError)):
        kbc.connect(board).close()
    assert not _stamp(board).exists()


def test_connect_write_side_effects_invalidate(board, checks):
    _stamped(board)
    conn = kbc.connect(board)  # skip path: process-cached from here on
    conn.close()
    _forget(board)
    c = sqlite3.connect(board)
    c.execute("CREATE TABLE side_effect (x)")
    c.execute("INSERT INTO side_effect VALUES (1)")
    c.commit()
    c.close()
    assert not _fresh(board)
    assert _connect_scans(board, checks) >= 1


def test_schema_write_by_connect_not_certified_by_older_stamp(board, checks):
    """A write performed under the stamped generation (e.g. a migration) changes it: the next process rescans."""
    _stamped(board)
    gen = kbc._integrity_generation(board.resolve())
    conn = kbc.connect(board)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS migr (x)")
        conn.commit()
    finally:
        conn.close()
    _forget(board)
    assert kbc._integrity_generation(board.resolve()) != gen
    assert not _fresh(board)


def test_explicit_repair_revokes_prior_shared_certificate(board):
    _stamped(board)
    assert kbc.repair_db(board).status == "ok"
    assert not _stamp(board).exists()
