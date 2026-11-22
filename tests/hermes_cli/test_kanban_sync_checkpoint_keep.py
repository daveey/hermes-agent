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
