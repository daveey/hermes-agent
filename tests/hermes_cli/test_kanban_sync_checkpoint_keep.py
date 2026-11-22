"""BoardWatch keeps its reader across a WAL checkpoint and reopens on every other main-file change. [daveey]"""
import os
import sqlite3

import pytest

from hermes_cli import kanban_sync_wal_evidence as evidence
from hermes_cli import loop_accounting
from hermes_cli.kanban_sync_board_watch import BoardWatch
from hermes_cli.kanban_sync_wal_evidence import WalState

VALUE = "SELECT v FROM t"
PAD = "x" * 10000  # more than a page: every checkpoint of it grows or rewrites the main file


@pytest.fixture
def board(tmp_path):
    path = tmp_path / "board.db"
    writer = sqlite3.connect(path, isolation_level=None)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")  # every checkpoint here is one the test asked for
    writer.execute("CREATE TABLE t (v TEXT)")
    writer.execute("CREATE TABLE u (v TEXT)")
    writer.execute("CREATE TABLE pad (v TEXT)")
    # A board as the loop meets it: its header is in the main file, as after any first checkpoint.
    assert writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
    writer.execute("INSERT INTO t VALUES ('a')")
    watch = BoardWatch()
    loop_accounting.reset()
    yield path, writer, watch
    watch.reset()
    writer.close()
    loop_accounting.reset()


def _reader(watch, path):
    return watch._conns[str(path)][1]


def _runs(name):
    return loop_accounting.counters().get(name, {}).get("runs", 0)


def _checkpoint(writer, mode):
    busy, _log, _done = writer.execute(f"PRAGMA wal_checkpoint({mode})").fetchone()
    assert busy == 0


def _rewrite(path, image):
    """A raw restore: other bytes in the same inode, behind SQLite's back."""
    before = os.stat(path)
    with open(path, "r+b") as handle:
        handle.write(image)
        handle.truncate()
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 2_000_000_000))
    assert os.stat(path).st_ino == before.st_ino


@pytest.mark.parametrize("mode", ["PASSIVE", "RESTART", "TRUNCATE"])
def test_checkpoint_keeps_reader_and_sees_later_commit(board, mode):
    path, writer, watch = board
    gen, row = watch.query(path, VALUE, unchanged_ok=True)
    assert row == ("a",)
    kept = _reader(watch, path)
    for value in ("b", "c"):  # twice: the kept reader's new baseline must prove the next checkpoint too
        before = os.stat(path)
        writer.execute("INSERT INTO pad VALUES (?)", (PAD,))
        writer.execute("UPDATE t SET v = ?", (value,))
        _checkpoint(writer, mode)
        after = os.stat(path)
        assert (after.st_mtime_ns, after.st_size) != (before.st_mtime_ns, before.st_size)
        assert watch.query(path, VALUE, unchanged_ok=True) == (gen, (value,))
        assert _reader(watch, path) is kept
        writer.execute("UPDATE t SET v = ?", (value + "+",))  # committed after the checkpoint, WAL only
        assert watch.query(path, VALUE, unchanged_ok=True) == (gen, (value + "+",))
        assert _reader(watch, path) is kept
    assert _runs("sync.reader_keep/checkpoint") == 2
    assert _runs("sync.reader_open.reason/first") == 1
    assert _runs("sync.reader_open.reason/changed") == 0


def _two_images(writer, path):
    """The main file holding 'a', then advanced to 'b' with the WAL emptied; returns the 'a' image."""
    _checkpoint(writer, "TRUNCATE")
    image = path.read_bytes()
    writer.execute("UPDATE t SET v = 'b'")
    _checkpoint(writer, "TRUNCATE")
    assert len(path.read_bytes()) == len(image)
    # Same size, so only mtime can show the next write, and a coarse file clock (one kernel tick,
    # 1-10 ms before Linux 6.13) stamps a write in this same tick with this same mtime. Age the
    # file so every later write moves it. (CI 2026-11-22: the checkpoint landed in the tick.)
    stat = os.stat(path)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns - 10_000_000_000))
    return image


def _moved(watch, path):
    """The main file no longer has the (mtime, size) the kept reader was stored with."""
    stat = os.stat(path)
    return watch._conns[str(path)][0][2:] != (stat.st_mtime_ns, stat.st_size)


def test_missing_pread_reads_and_reopens_conservatively(board, monkeypatch):
    """Platforms without pread still read boards; no checkpoint may keep their reader."""
    path, writer, watch = board
    image = _two_images(writer, path)
    monkeypatch.delattr(os, "pread", raising=False)
    gen, row = watch.query(path, VALUE, unchanged_ok=True)
    assert row == ("b",)
    old = _reader(watch, path)
    assert watch._conns[str(path)][6] is None
    writer.execute("INSERT INTO pad VALUES (?)", (PAD,))
    _checkpoint(writer, "TRUNCATE")
    assert _moved(watch, path)
    assert watch.query(path, VALUE, unchanged_ok=True) == (gen, ("b",))
    assert _reader(watch, path) is not old
    writer.execute("UPDATE t SET v = 'later'")
    assert watch.query(path, VALUE, unchanged_ok=True) == (gen, ("later",))
    _checkpoint(writer, "TRUNCATE")
    old = _reader(watch, path)
    _rewrite(path, image)
    assert _moved(watch, path)
    assert watch.query(path, VALUE, unchanged_ok=True) == (gen, ("a",))
    assert _reader(watch, path) is not old
    assert _runs("sync.reader_keep/checkpoint") == 0


def test_same_inode_raw_restore_reopens(board):
    path, writer, watch = board
    image = _two_images(writer, path)
    gen, row = watch.query(path, VALUE, unchanged_ok=True)
    assert row == ("b",)
    old = _reader(watch, path)
    state = evidence.read(path)
    assert state is not None  # the sidecars are there: nothing but missing proof forces the reopen
    _rewrite(path, image)
    assert evidence.read(path) == state
    assert _moved(watch, path)
    assert watch.query(path, VALUE, unchanged_ok=True) == (gen, ("a",))
    assert _reader(watch, path) is not old
    with pytest.raises(sqlite3.ProgrammingError):
        old.execute("SELECT 1")
    assert _runs("sync.reader_open.reason/changed") == 1
    assert _runs("sync.reader_keep/checkpoint") == 0


def test_restore_with_checkpoint_in_same_interval_matches_fresh_reader(board):
    """Proof of a checkpoint is not proof that nothing else wrote the file: the kept reader must
    answer as a new connection would, from the bytes now on disk."""
    path, writer, watch = board
    image = _two_images(writer, path)
    writer.execute("INSERT INTO u VALUES ('pending')")  # frames the next checkpoint will backfill
    assert watch.query(path, VALUE)[1] == ("b",)  # t's page is now in the kept reader's cache
    kept = _reader(watch, path)
    _rewrite(path, image)
    _checkpoint(writer, "PASSIVE")
    fresh = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        expected = fresh.execute(VALUE).fetchone()
    finally:
        fresh.close()
    assert expected == ("a",)
    assert _moved(watch, path)  # else the watch is never asked to choose, and no path is tested
    assert watch.query(path, VALUE)[1] == expected
    assert _reader(watch, path) is kept  # the proof held, so this exercised the kept path
    assert _runs("sync.reader_keep/checkpoint") == 1


SWAPPED = ("CREATE TABLE u (v TEXT)", "CREATE TABLE t (v TEXT)", "CREATE TABLE pad (v TEXT)",
           "INSERT INTO t VALUES ('img-t')", "INSERT INTO u VALUES ('img-u')")  # t and u trade root pages
WIDER = ("CREATE TABLE t (z TEXT, v TEXT)", "CREATE TABLE u (v TEXT)", "CREATE TABLE pad (v TEXT)",
         "INSERT INTO t VALUES ('img-z', 'img-t')")  # v is t's second column


def _image(tmp_path, statements):
    """The bytes of another database: its own schema, written by its own connection."""
    other = tmp_path / "image.db"
    conn = sqlite3.connect(other, isolation_level=None)
    for statement in statements:
        conn.execute(statement)
    conn.close()
    return other.read_bytes()


@pytest.mark.parametrize("statements", [SWAPPED, WIDER], ids=["swapped-roots", "other-columns"])
@pytest.mark.parametrize("mode", ["PASSIVE", "TRUNCATE"])
@pytest.mark.parametrize("restore_first", [False, True], ids=["checkpoint-then-restore", "restore-then-checkpoint"])
def test_restore_of_another_schema_under_the_same_cookie_reopens(board, tmp_path, statements, mode, restore_first):
    """A checkpoint and a raw restore in one interval, the image's schema cookie equal to the
    live one: SQLite sees no schema change, so the reader's parsed schema must not be kept."""
    path, writer, watch = board
    image = _image(tmp_path, statements)
    _two_images(writer, path)
    assert path.read_bytes()[40:44] == image[40:44]  # equal schema cookies: SQLite's check passes
    writer.execute("INSERT INTO u VALUES ('pending')")  # frames the checkpoint will backfill
    gen, row = watch.query(path, VALUE)
    assert row == ("b",)
    old = _reader(watch, path)
    if restore_first:
        _rewrite(path, image)
        _checkpoint(writer, mode)
    else:
        _checkpoint(writer, mode)
        _rewrite(path, image)
    fresh = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        expected = fresh.execute(VALUE).fetchall()
    finally:
        fresh.close()
    assert _moved(watch, path)
    assert watch.read(path, VALUE, lambda conn: conn.execute(VALUE).fetchall()) == (gen, expected)
    assert _reader(watch, path) is not old
    assert _runs("sync.reader_open.reason/changed") == 1
    assert _runs("sync.reader_keep/checkpoint") == 0


@pytest.mark.parametrize("mode", ["PASSIVE", "TRUNCATE"])
@pytest.mark.parametrize("restore_first", [False, True], ids=["checkpoint-then-restore", "restore-then-checkpoint"])
def test_restore_of_another_file_format_under_the_same_schema_reopens(board, mode, restore_first):
    """The image has the live cookie and the live ``sqlite_schema`` rows; only the header's schema
    format number differs. The reader's parse holds that number too, so it must not be kept."""
    path, writer, watch = board
    image = bytearray(_two_images(writer, path))
    assert image[44:48] == path.read_bytes()[44:48] == (4).to_bytes(4, "big")
    image[44:48] = (1).to_bytes(4, "big")
    writer.execute("INSERT INTO u VALUES ('pending')")  # frames the checkpoint will backfill
    gen, row = watch.query(path, VALUE)
    assert row == ("b",)
    old = _reader(watch, path)
    stored = watch._conns[str(path)][6]
    if restore_first:
        _rewrite(path, bytes(image))
        _checkpoint(writer, mode)
    else:
        _checkpoint(writer, mode)
        _rewrite(path, bytes(image))
    assert evidence.checkpointed(watch._conns[str(path)][3], evidence.read(path))
    assert _moved(watch, path)
    assert watch.query(path, VALUE) == (gen, _fresh(path, VALUE)[0])
    assert _reader(watch, path) is not old
    now = watch._conns[str(path)][6]
    assert now[1:] == stored[1:] and now[0] != stored[0]  # same cookie and rows: the format decided
    assert _runs("sync.reader_open.reason/changed") == 1
    assert _runs("sync.reader_keep/checkpoint") == 0


DESC = ("CREATE TABLE t (v TEXT)", "CREATE TABLE u (v TEXT)", "CREATE INDEX t_v ON t (v DESC)")
BY_INDEX = "SELECT v FROM t INDEXED BY t_v WHERE v >= 'img-y' ORDER BY v DESC"


@pytest.mark.skipif(not hasattr(sqlite3, "SQLITE_DBCONFIG_LEGACY_FILE_FORMAT"), reason="needs Connection.setconfig")
def test_restored_legacy_format_image_is_read_as_a_fresh_reader_reads_it(tmp_path):
    """What the format number changes: a format-1 file stores a DESC index in ascending order.
    A kept reader that parsed format 4 walked it the wrong way and lost rows without an error."""
    path, other = tmp_path / "board.db", tmp_path / "image.db"
    writer = sqlite3.connect(path, isolation_level=None)
    legacy = sqlite3.connect(other, isolation_level=None)
    legacy.setconfig(sqlite3.SQLITE_DBCONFIG_LEGACY_FILE_FORMAT, True)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    for conn, rows in ((writer, ("live-a", "live-b")), (legacy, ("img-x", "img-y", "img-z", "img-w"))):
        for statement in DESC:
            conn.execute(statement)
        conn.executemany("INSERT INTO t VALUES (?)", [(row,) for row in rows])
    _checkpoint(writer, "TRUNCATE")
    cookie = writer.execute("PRAGMA schema_version").fetchone()[0]
    legacy.execute(f"PRAGMA schema_version={cookie}")
    legacy.close()
    image = other.read_bytes()
    assert image[44:48] == (1).to_bytes(4, "big") and image[40:44] == path.read_bytes()[40:44]
    stat = os.stat(path)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns - 10_000_000_000))
    writer.execute("INSERT INTO u VALUES ('pending')")
    watch = BoardWatch()
    loop_accounting.reset()
    try:
        before = [("live-b",), ("live-a",)]
        assert watch.read(path, BY_INDEX, lambda conn: conn.execute(BY_INDEX).fetchall())[1] == before
        old = _reader(watch, path)
        _checkpoint(writer, "PASSIVE")
        _rewrite(path, image)
        expected = _fresh(path, BY_INDEX)
        assert expected == [("img-z",), ("img-y",)]
        assert _moved(watch, path)
        assert watch.read(path, BY_INDEX, lambda conn: conn.execute(BY_INDEX).fetchall())[1] == expected
        assert _reader(watch, path) is not old
        assert _runs("sync.reader_keep/checkpoint") == 0
    finally:
        watch.reset()
        writer.close()
        loop_accounting.reset()


def test_reader_whose_file_header_cannot_be_read_is_not_kept(board, monkeypatch):
    """No format number at open and none at the keep are not two equal answers: both are doubt."""
    from hermes_cli import kanban_sync_board_watch as module

    path, writer, watch = board
    monkeypatch.setattr(module, "pread_lock_safe", lambda *_args: None)
    writer.execute("INSERT INTO pad VALUES (?)", (PAD,))
    gen, row = watch.query(path, VALUE)
    assert row == ("a",)
    old = _reader(watch, path)
    assert watch._conns[str(path)][6] is None
    base = evidence.read(path)
    _checkpoint(writer, "PASSIVE")
    assert evidence.checkpointed(base, evidence.read(path))
    assert _moved(watch, path)
    assert watch.query(path, VALUE) == (gen, ("a",))
    assert _reader(watch, path) is not old
    assert _runs("sync.reader_keep/checkpoint") == 0
    assert _runs("sync.reader_open.reason/changed") == 1


def test_board_whose_header_was_only_in_the_wal_reopens_once_then_keeps(tmp_path):
    """A board never checkpointed has no format number in its main file yet. The first checkpoint
    writes one, which differs from what the reader was stored with: one reopen, then keeps."""
    path = tmp_path / "new.db"
    writer = sqlite3.connect(path, isolation_level=None)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("CREATE TABLE t (v TEXT)")
    writer.execute("INSERT INTO t VALUES ('a')")
    watch = BoardWatch()
    loop_accounting.reset()
    try:
        assert watch.query(path, VALUE, unchanged_ok=True)[1] == ("a",)
        first = _reader(watch, path)
        assert watch._conns[str(path)][6][0] != (4).to_bytes(4, "big")
        _checkpoint(writer, "PASSIVE")
        assert watch.query(path, VALUE, unchanged_ok=True)[1] == ("a",)
        second = _reader(watch, path)
        assert second is not first and _runs("sync.reader_keep/checkpoint") == 0
        assert watch._conns[str(path)][6][0] == (4).to_bytes(4, "big")
        writer.execute("INSERT INTO t VALUES (?)", (PAD,))
        assert watch.query(path, "SELECT count(*) FROM t", unchanged_ok=True)[1] == (2,)
        _checkpoint(writer, "PASSIVE")
        assert _moved(watch, path)
        assert watch.query(path, "SELECT count(*) FROM t", unchanged_ok=True)[1] == (2,)
        assert _reader(watch, path) is second and _runs("sync.reader_keep/checkpoint") == 1
    finally:
        watch.reset()
        writer.close()
        loop_accounting.reset()


def test_schema_change_through_sqlite_reopens_at_the_next_checkpoint(board):
    path, writer, watch = board
    gen, row = watch.query(path, VALUE, unchanged_ok=True)
    assert row == ("a",)
    old = _reader(watch, path)
    writer.execute("ALTER TABLE t ADD COLUMN w TEXT DEFAULT 'new'")
    writer.execute("INSERT INTO pad VALUES (?)", (PAD,))
    _checkpoint(writer, "PASSIVE")
    assert _moved(watch, path)
    assert watch.query(path, "SELECT v, w FROM t", unchanged_ok=True) == (gen, ("a", "new"))
    assert _reader(watch, path) is not old
    assert _runs("sync.reader_keep/checkpoint") == 0


def _fresh(path, sql):
    """What a connection opened now answers, or the SQLite error class it raises."""
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        return conn.execute(sql).fetchall()
    except sqlite3.Error as exc:
        return type(exc)
    finally:
        conn.close()


MIGRATIONS = {
    "add-column": ("ALTER TABLE t ADD COLUMN w TEXT DEFAULT 'new'", "SELECT * FROM t"),
    "rename-column": ("ALTER TABLE t RENAME COLUMN v TO q", "SELECT q FROM t"),
    "rename-table": ("ALTER TABLE u RENAME TO moved", "SELECT count(*) FROM moved"),
}


@pytest.mark.parametrize("name", list(MIGRATIONS))
@pytest.mark.parametrize("mode", ["PASSIVE", "TRUNCATE"])
def test_restore_of_the_open_time_schema_after_a_migration_the_reader_parsed(board, tmp_path, name, mode):
    """SQLite re-parses the kept reader's schema when a migration arrives through the WAL, so the
    rows stored at open no longer describe its parse. A backup taken before the migration
    (``VACUUM INTO`` bumps the cookie, as the migration does) restored in the checkpoint's own
    interval has the open-time rows under the current cookie: only the cookie stored with the
    rows shows that the reader's parse is a later one."""
    migration, sql = MIGRATIONS[name]
    path, writer, watch = board
    _two_images(writer, path)
    backup = tmp_path / "backup.db"
    writer.execute("VACUUM INTO ?", (str(backup),))
    image = backup.read_bytes()

    def ask():
        got = watch.read(path, sql, lambda conn: conn.execute(sql).fetchall(), unchanged_ok=True)
        return sqlite3.OperationalError if got is None else got[1]

    assert watch.query(path, VALUE, unchanged_ok=True)[1] == ("b",)
    old = _reader(watch, path)
    writer.execute(migration)  # WAL only: the main file does not move, so the watch is not asked
    migrated = ask()
    assert migrated == _fresh(path, sql) and _reader(watch, path) is old  # its parse is now the migrated one
    _checkpoint(writer, mode)
    assert path.read_bytes()[40:44] == image[40:44]  # the image has the current cookie
    assert len(image) >= os.stat(path).st_size  # so the shrink clause does not decide this
    _rewrite(path, image)
    assert evidence.checkpointed(watch._conns[str(path)][3], evidence.read(path))
    expected = _fresh(path, sql)
    assert expected != migrated  # the restored file answers as it did before the migration
    assert _moved(watch, path)
    assert ask() == expected
    with pytest.raises(sqlite3.ProgrammingError):
        old.execute("SELECT 1")  # closed: the reader was not kept
    assert _runs("sync.reader_keep/checkpoint") == 0


def test_kept_reader_gives_up_its_cached_answer(board):
    """``outbox_pending`` asks with ``unchanged_ok``, and a passive checkpoint does not move
    ``data_version``: a kept reader that kept its answers would repeat the pre-restore one."""
    path, writer, watch = board
    image = _two_images(writer, path)
    writer.execute("INSERT INTO u VALUES ('pending')")  # frames the next checkpoint will backfill
    assert watch.query(path, VALUE, unchanged_ok=True)[1] == ("b",)
    kept = _reader(watch, path)
    version = kept.execute("PRAGMA data_version").fetchone()
    _checkpoint(writer, "PASSIVE")
    _rewrite(path, image)
    assert kept.execute("PRAGMA data_version").fetchone() == version  # only dropping the answer refreshes it
    assert _moved(watch, path)
    assert _fresh(path, VALUE) == [("a",)]
    assert watch.query(path, VALUE, unchanged_ok=True)[1] == ("a",)
    assert _reader(watch, path) is kept
    assert _runs("sync.reader_keep/checkpoint") == 1


def test_replaced_inode_reopens_even_with_checkpoint_proof(board):
    """A writer still on the unlinked inode checkpoints it: the -shm proves a checkpoint, of
    another file. The reader must follow the path to the new inode."""
    path, writer, watch = board
    _checkpoint(writer, "TRUNCATE")
    copy = path.with_name("copy.db")
    copy.write_bytes(path.read_bytes())
    edit = sqlite3.connect(copy)
    edit.execute("UPDATE t SET v = 'replaced'")
    edit.execute("INSERT INTO pad VALUES (?)", (PAD,))  # larger: the shrink clause does not decide this
    edit.commit()
    edit.close()
    writer.execute("INSERT INTO u VALUES ('pending')")  # frames outstanding at the baseline
    gen, row = watch.query(path, VALUE, unchanged_ok=True)
    assert row == ("a",)
    old = _reader(watch, path)
    base, stored = evidence.read(path), os.stat(path)
    os.replace(copy, path)
    _checkpoint(writer, "TRUNCATE")  # on the old inode; it also empties the log the new file is read through
    after = os.stat(path)
    assert after.st_ino != stored.st_ino and after.st_size >= stored.st_size
    assert evidence.checkpointed(base, evidence.read(path))
    new_gen, row = watch.query(path, VALUE, unchanged_ok=True)
    assert row == ("replaced",) and new_gen != gen
    assert _reader(watch, path) is not old
    assert _runs("sync.reader_open.reason/replaced") == 1
    assert _runs("sync.reader_keep/checkpoint") == 0


def test_raw_restore_after_a_keep_is_judged_against_the_keeps_own_baseline(board):
    """The keep stores the wal-index state it saw. Carrying the older baseline forward would let
    a checkpoint that was already used prove every later move of the file."""
    path, writer, watch = board
    image = _two_images(writer, path)
    writer.execute("INSERT INTO pad VALUES (?)", (PAD,))  # frames the checkpoint will backfill
    gen, row = watch.query(path, VALUE)
    assert row == ("b",)
    old = _reader(watch, path)
    _checkpoint(writer, "PASSIVE")
    assert watch.query(path, VALUE) == (gen, ("b",))
    assert _reader(watch, path) is old and _runs("sync.reader_keep/checkpoint") == 1
    assert watch._conns[str(path)][3] == evidence.read(path)
    _rewrite(path, image + bytes(os.stat(path).st_size - len(image)))  # no smaller than the kept file
    assert _moved(watch, path)
    expected = _fresh(path, VALUE)
    assert watch.read(path, VALUE, lambda conn: conn.execute(VALUE).fetchall()) == (gen, expected)
    assert _reader(watch, path) is not old
    assert _runs("sync.reader_keep/checkpoint") == 1
    assert _runs("sync.reader_open.reason/changed") == 1


@pytest.mark.parametrize("failing", ["shrink", "schema"])
def test_reader_that_cannot_be_checked_is_not_kept(board, monkeypatch, failing):
    """A SQLite error while the kept reader drops its cache or reads the schema is doubt."""
    from hermes_cli import kanban_sync_board_watch as module

    path, writer, watch = board
    writer.execute("INSERT INTO pad VALUES (?)", (PAD,))
    gen, row = watch.query(path, VALUE)
    assert row == ("a",)
    old = _reader(watch, path)
    base = evidence.read(path)
    _checkpoint(writer, "PASSIVE")
    assert evidence.checkpointed(base, evidence.read(path))
    real, asked = module.schema_rows, []

    def refuse(conn):
        asked.append(conn)
        if conn is old:
            raise sqlite3.OperationalError("disk I/O error")
        return real(conn)

    monkeypatch.setattr(module, "schema_rows", refuse)
    if failing == "shrink":
        old.close()  # every statement on it now raises sqlite3.ProgrammingError, the shrink first
    assert _moved(watch, path)
    assert watch.query(path, VALUE) == (gen, ("a",))
    assert _reader(watch, path) is not old
    assert (old in asked) is (failing == "schema")
    assert _runs("sync.reader_keep/checkpoint") == 0
    assert _runs("sync.reader_open.reason/changed") == 1


def test_file_moving_while_the_reader_opens_is_never_kept(board, monkeypatch):
    """The schema rows are read after the stat the reader is stored with: a file that moved in between
    may not be the one the schema was parsed from, so the next checkpoint reopens."""
    from hermes_cli import kanban_sync_board_watch as module

    path, writer, watch = board
    real = module.schema_rows

    def moved_under_the_read(conn):
        rows = real(conn)
        stat = os.stat(path)
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
        return rows

    monkeypatch.setattr(module, "schema_rows", moved_under_the_read)
    writer.execute("INSERT INTO pad VALUES (?)", (PAD,))  # frames outstanding: the checkpoint below is provable
    assert watch.query(path, VALUE, unchanged_ok=True)[1] == ("a",)
    monkeypatch.setattr(module, "schema_rows", real)
    assert watch._conns[str(path)][6] is None
    old = _reader(watch, path)
    base = evidence.read(path)
    _checkpoint(writer, "PASSIVE")
    assert evidence.checkpointed(base, evidence.read(path))
    assert watch.query(path, VALUE, unchanged_ok=True)[1] == ("a",)
    assert _reader(watch, path) is not old
    assert watch._conns[str(path)][6] == module.parsed_from(_reader(watch, path), str(path))
    assert _runs("sync.reader_keep/checkpoint") == 0


def test_wal_restart_with_backfilled_baseline_is_not_proof(board):
    path, writer, watch = board
    _checkpoint(writer, "PASSIVE")
    gen, row = watch.query(path, VALUE, unchanged_ok=True)
    assert row == ("a",)
    old = _reader(watch, path)
    base = evidence.read(path)
    assert base is not None and base.backfilled == base.max_frame > 0
    _rewrite(path, path.read_bytes())  # a raw rewrite; the next commit restarts the log, no checkpoint
    writer.execute("UPDATE t SET v = 'b'")
    now = evidence.read(path)
    assert now is not None and now.salts != base.salts
    assert watch.query(path, VALUE, unchanged_ok=True) == (gen, ("b",))
    assert _reader(watch, path) is not old
    assert _runs("sync.reader_open.reason/changed") == 1


def test_shrunken_file_reopens_even_with_checkpoint_proof(board):
    path, writer, watch = board
    writer.execute("INSERT INTO pad VALUES (?)", (PAD * 20,))
    _checkpoint(writer, "PASSIVE")
    writer.execute("UPDATE t SET v = 'b'")  # restarts the log: the baseline below has frames outstanding
    gen, row = watch.query(path, VALUE, unchanged_ok=True)
    assert row == ("b",)
    old = _reader(watch, path)
    base, size = evidence.read(path), os.stat(path).st_size
    writer.execute("DELETE FROM pad")
    writer.execute("VACUUM")
    _checkpoint(writer, "PASSIVE")
    assert os.stat(path).st_size < size
    assert evidence.checkpointed(base, evidence.read(path))
    assert watch.query(path, VALUE, unchanged_ok=True) == (gen, ("b",))
    assert _reader(watch, path) is not old
    assert _runs("sync.reader_open.reason/changed") == 1


def test_smaller_restore_reopens_even_with_checkpoint_proof(board):
    """The shrink clause alone: same inode, a proven checkpoint, the same schema cookie and rows
    (the ``VACUUM`` in the test above also moves the cookie, which reopens by itself)."""
    path, writer, watch = board
    image = _two_images(writer, path)
    writer.execute("INSERT INTO pad VALUES (?)", (PAD * 20,))
    _checkpoint(writer, "TRUNCATE")
    writer.execute("INSERT INTO u VALUES ('pending')")  # frames outstanding at the baseline
    gen, row = watch.query(path, VALUE)
    assert row == ("b",)
    old = _reader(watch, path)
    base, size = evidence.read(path), os.stat(path).st_size
    assert watch._conns[str(path)][6][1] == (int.from_bytes(image[40:44], "big"),)  # the image has the reader's cookie
    assert watch._conns[str(path)][6][0] == image[44:48]  # and its file format
    _checkpoint(writer, "PASSIVE")
    _rewrite(path, image)
    assert os.stat(path).st_size < size
    assert evidence.checkpointed(base, evidence.read(path))
    expected = _fresh(path, VALUE)
    assert watch.read(path, VALUE, lambda conn: conn.execute(VALUE).fetchall()) == (gen, expected)
    assert _reader(watch, path) is not old
    assert _runs("sync.reader_keep/checkpoint") == 0
    assert _runs("sync.reader_open.reason/changed") == 1


def test_replaced_inode_reopens_with_new_generation(board):
    path, writer, watch = board
    _checkpoint(writer, "TRUNCATE")
    gen, row = watch.query(path, VALUE, unchanged_ok=True)
    assert row == ("a",)
    old = _reader(watch, path)
    copy = path.with_name("copy.db")
    copy.write_bytes(path.read_bytes())
    edit = sqlite3.connect(copy)
    edit.execute("UPDATE t SET v = 'replaced'")
    edit.commit()
    edit.close()
    inode = os.stat(path).st_ino
    os.replace(copy, path)
    assert os.stat(path).st_ino != inode
    new_gen, row = watch.query(path, VALUE, unchanged_ok=True)
    assert row == ("replaced",) and new_gen != gen
    assert _reader(watch, path) is not old
    assert _runs("sync.reader_open.reason/replaced") == 1
    assert _runs("sync.reader_keep/checkpoint") == 0


def test_rollback_journal_board_has_no_proof(tmp_path):
    path = tmp_path / "plain.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (v TEXT)")
    conn.commit()
    conn.close()
    assert evidence.read(path) is None


def test_header_in_motion_is_not_read(board):
    path, _writer, _watch = board
    good = evidence.read(path)
    assert good is not None and good.max_frame > 0 and good.backfilled == 0
    shm = path.with_name(path.name + "-shm").read_bytes()
    torn = tmp_path_copy(path, shm[:48] + bytes(48) + shm[96:])
    assert evidence.read(torn) is None
    unset = tmp_path_copy(path, bytes(12) + b"\0" + shm[13:48] + bytes(12) + b"\0" + shm[61:])
    assert evidence.read(unset) is None
    assert evidence.read(tmp_path_copy(path, shm[:100])) is None


def tmp_path_copy(path, shm):
    """A stand-in board path whose -shm holds *shm* (never the live board's own sidecar)."""
    fake = path.with_name(f"fake-{len(shm)}-{shm[60]}.db")
    fake.with_name(fake.name + "-shm").write_bytes(shm)
    return fake


STATE = WalState(shm=(1, 2), salts=b"saltsalt", max_frame=8, backfilled=3)


@pytest.mark.parametrize("before, after, proven", [
    (STATE, STATE._replace(backfilled=8), True),
    (STATE, STATE._replace(max_frame=12, backfilled=4), True),
    (STATE, STATE, False),
    (STATE, STATE._replace(max_frame=12), False),  # commits alone
    (STATE._replace(backfilled=8), STATE._replace(backfilled=3), False),
    (STATE, STATE._replace(salts=b"newsalts", max_frame=1, backfilled=0), True),
    (STATE._replace(backfilled=8), STATE._replace(salts=b"newsalts", max_frame=1, backfilled=0), False),
    (STATE._replace(max_frame=0, backfilled=0), STATE._replace(salts=b"newsalts", max_frame=0, backfilled=0), False),
    (STATE, STATE._replace(shm=(1, 3), backfilled=8), False),
    (None, STATE, False),
    (STATE, None, False),
])
def test_checkpoint_proof(before, after, proven):
    assert evidence.checkpointed(before, after) is proven
