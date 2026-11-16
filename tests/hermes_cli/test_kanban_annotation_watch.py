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
