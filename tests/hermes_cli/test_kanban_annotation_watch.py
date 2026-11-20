"""The watch signature sees every commit, not only those that change a file's size or mtime. [daveey]"""
import os
import sqlite3

from hermes_cli import kanban_annotation_watch as watch

SCOPE = ("watch-test", "default")


def _db(path, wal: bool):
    conn = sqlite3.connect(path, isolation_level=None)
    if wal:
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.execute("INSERT INTO t VALUES (1, 'aaaa')")
    return conn


def _same_stat_commit(conn, files):
    """Commit a same-size row update, then put every file's size/mtime back as it was before."""
    before = {f: os.stat(f) for f in files if os.path.exists(f)}
    conn.execute("UPDATE t SET v = CASE v WHEN 'aaaa' THEN 'bbbb' ELSE 'aaaa' END WHERE id = 1")
    for f, st in before.items():
        assert os.stat(f).st_size == st.st_size
        os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))


def test_rollback_journal_commit_with_same_stat_moves_the_signature(tmp_path):
    path = str(tmp_path / "rb.db")
    conn = _db(path, wal=False)
    before = watch.signature(SCOPE, [path], [])
    _same_stat_commit(conn, [path])
    assert watch.signature(SCOPE, [path], []) != before
    conn.close()


def test_wal_commit_with_same_stat_moves_the_signature(tmp_path):
    path = str(tmp_path / "wal.db")
    conn = _db(path, wal=True)
    conn.execute("UPDATE t SET v = 'cccc' WHERE id = 1")
    # After a RESTART checkpoint the next commit rewrites the WAL from its first frame: same size.
    conn.execute("PRAGMA wal_checkpoint(RESTART)")
    files = [path, path + "-wal"]
    stats = {f: os.stat(f) for f in files}
    before = watch.signature(SCOPE, [path], [])
    conn.execute("UPDATE t SET v = 'dddd' WHERE id = 1")
    for f, st in stats.items():
        assert os.stat(f).st_size == st.st_size
        os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert watch.signature(SCOPE, [path], []) != before
    conn.close()


def test_unchanged_files_keep_the_signature(tmp_path):
    path = str(tmp_path / "quiet.db")
    conn = _db(path, wal=True)
    cue = tmp_path / "cue.json"
    assert watch.signature(SCOPE, [path], [str(cue)]) == watch.signature(SCOPE, [path], [str(cue)])
    cue.write_text("{}")
    assert watch.signature(SCOPE, [path], []) != watch.signature(SCOPE, [path], [str(cue)])
    conn.close()


def test_missing_files_are_part_of_the_signature(tmp_path):
    path = str(tmp_path / "later.db")
    before = watch.signature(SCOPE, [path], [])
    _db(path, wal=False).close()
    assert watch.signature(SCOPE, [path], []) != before


_TRY_WRITE = """
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1], timeout=0, isolation_level=None)
try:
    conn.execute("BEGIN IMMEDIATE")
except sqlite3.OperationalError as exc:
    print("locked" if "locked" in str(exc) else "error: %s" % exc)
else:
    conn.execute("ROLLBACK")
    print("acquired")
"""


def test_signature_keeps_this_process_sqlite_locks(tmp_path):
    """hermes-hub 2026-11-18/20: the signature read state.db and its -shm with open()/close() in the Desktop
    backend, which cancelled every SQLite POSIX lock the process held on them; a second process then took the WAL
    writer lock while this one was mid-write and the two corrupted state.db's indexes. A write transaction open
    here must keep refusing a foreign writer across signature()."""
    import subprocess
    import sys

    path = str(tmp_path / "live.db")
    conn = _db(path, wal=True)

    def foreign_writer():
        return subprocess.run([sys.executable, "-c", _TRY_WRITE, path], capture_output=True, text=True, check=True,
                              timeout=60).stdout.strip()

    conn.execute("BEGIN IMMEDIATE")
    conn.execute("UPDATE t SET v = 'mid-write' WHERE id = 1")
    assert foreign_writer() == "locked"
    for _ in range(3):
        watch.signature(SCOPE, [path], [])
    assert foreign_writer() == "locked"
    conn.execute("COMMIT")
    assert foreign_writer() == "acquired"
    conn.close()


def test_head_reports_missing_files_and_raises_on_unreadable(tmp_path, monkeypatch):
    assert watch._head(str(tmp_path / "absent.db"), 0, 4) is None
    present = tmp_path / "present.db"
    present.write_bytes(b"0123456789")
    assert watch._head(str(present), 2, 6) == b"2345".hex()
    monkeypatch.setattr(watch, "pread_lock_safe", lambda *_a: None)
    import pytest

    with pytest.raises(OSError):
        watch._head(str(present), 0, 4)
