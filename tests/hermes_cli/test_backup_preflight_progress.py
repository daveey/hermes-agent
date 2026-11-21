"""The update pre-flight snapshot reports progress and prefers a copy-on-write clone.

Desktop shows these stderr lines in its update overlay: on a 21.7 GB state.db the page copy took
minutes behind a bar that never moved, and the quick_check after it ran minutes more without
feedback and ignored SIGTERM. The clone path makes the common case instant.
"""
import errno
import io
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.waiting import wait_until

SCRIPT = Path(__file__).resolve().parents[2] / "hermes_cli" / "backup_sqlite.py"
NO_CLONE = {**os.environ, "HERMES_PREFLIGHT_CLONE": "0"}


def _home(base: Path, rows: int, journal_mode: str = "WAL") -> Path:
    home = base / "home"
    home.mkdir()
    db = sqlite3.connect(home / "state.db")
    try:
        db.execute(f"PRAGMA journal_mode={journal_mode}")
        db.execute("CREATE TABLE messages (body BLOB)")
        db.executemany("INSERT INTO messages VALUES (randomblob(4000))", [()] * rows)
        db.commit()
    finally:
        db.close()
    return home


def _events(stderr: str) -> list[dict]:
    events = []
    for line in stderr.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and str(event.get("event", "")).startswith("backup_"):
            events.append(event)
    return events


def _run(home: Path, env=None) -> tuple[dict, list[dict], float]:
    started = time.monotonic()
    result = subprocess.run([sys.executable, "-I", "-S", str(SCRIPT), str(home)], capture_output=True,
                            text=True, timeout=120, check=False, env=env)
    elapsed = time.monotonic() - started
    assert result.returncode == 0, result.stderr
    # stdout stays the single result object older Desktops log verbatim.
    lines = result.stdout.splitlines()
    assert len(lines) == 1, result.stdout
    return json.loads(lines[0]), _events(result.stderr), elapsed


def _snapshot_rows(path: str) -> list:
    with sqlite3.connect(path) as snapshot:
        assert snapshot.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        return snapshot.execute("SELECT body FROM messages ORDER BY rowid").fetchall()


def _lock_is_free(db: Path) -> bool:
    probe = sqlite3.connect(db, timeout=0, isolation_level=None)
    try:
        probe.execute("BEGIN IMMEDIATE")
        probe.execute("ROLLBACK")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        probe.close()


# ── progress lines (page copy) ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("journal_mode", ["WAL", "DELETE"])
def test_copy_progress_lines_parse_end_at_100_and_precede_the_check(tmp_path, journal_mode):
    home = _home(tmp_path, 20000, journal_mode)  # ~80 MB
    result, events, elapsed = _run(home, NO_CLONE)

    _assert_copy_progress(result, events, elapsed)


def _assert_copy_progress(result, events, elapsed):
    assert result["method"] == "copy"
    progress = [e for e in events if e["event"] == "backup_progress"]
    assert progress, events
    for event in progress:
        assert set(event) == {"event", "done_bytes", "total_bytes", "method"}
        assert event["method"] == "copy"
        assert 0 <= event["done_bytes"] <= event["total_bytes"]
    final = progress[-1]
    assert final["done_bytes"] == final["total_bytes"] == result["bytes"] > 0
    # Throttled to two lines a second, plus the forced first and final lines.
    assert len(progress) <= 2 * elapsed + 3, (len(progress), elapsed)
    # The check is announced after the copy, so the overlay can say so.
    assert events[-1] == {"event": "backup_check"}
    # A throttled callback can already report 100%; the forced final line may
    # have equal values, so locate the actual final event by identity, not index().
    assert events[-2] is final


def test_copy_progress_order_accepts_equal_but_distinct_final_events():
    from hermes_cli.backup_sqlite import _ProgressReporter

    out = io.StringIO()
    reporter = _ProgressReporter(out, clock=lambda: 100.0)
    reporter.progress(1000, 1000, method="copy")
    reporter.progress(1000, 1000, method="copy", force=True)
    events = _events(out.getvalue())
    assert len(events) == 2
    assert events[0] == events[1] and events[0] is not events[1]
    events.append({"event": "backup_check"})

    _assert_copy_progress({"method": "copy", "bytes": 1000}, events, elapsed=0)

    # Equal earlier values must not conceal a final progress line after the check,
    # or an intervening event between the final progress line and the check.
    for misplaced in (events[:1] + events[2:] + events[1:2],
                      events[:2] + [{"event": "backup_lock_wait"}] + events[2:]):
        with pytest.raises(AssertionError):
            _assert_copy_progress({"method": "copy", "bytes": 1000}, misplaced, elapsed=0)


def test_reporter_throttles_to_two_lines_a_second_and_always_writes_the_final_line():
    from hermes_cli.backup_sqlite import _ProgressReporter

    now = [100.0]
    out = io.StringIO()
    reporter = _ProgressReporter(out, clock=lambda: now[0])
    for done in range(100):
        reporter.progress(done, 1000, method="copy")
        now[0] += 0.004  # 100 updates in 0.4 s
    assert len(out.getvalue().splitlines()) == 1
    now[0] += 0.2  # 0.6 s after the first line
    reporter.progress(500, 1000, method="copy")
    reporter.progress(600, 1000, method="copy")
    assert len(out.getvalue().splitlines()) == 2
    reporter.progress(1000, 1000, method="copy", force=True)
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [line["done_bytes"] for line in lines] == [0, 500, 1000]
    assert lines[-1] == {"event": "backup_progress", "done_bytes": 1000, "total_bytes": 1000, "method": "copy"}


def test_imported_callers_get_no_progress_output(tmp_path, capsys):
    from hermes_cli.backup_sqlite import preflight_state_db

    home = _home(tmp_path, 10)
    assert preflight_state_db(home)["method"] == "copy"
    captured = capsys.readouterr()
    assert "backup_progress" not in captured.out + captured.err


# ── SIGTERM during the check ────────────────────────────────────────────────────────────────────

@pytest.mark.platforms("posix")
def test_sigterm_stops_the_copy_check(tmp_path):
    """The real quick_check of a small database is too quick to signal mid-way, so the driver makes
    the checked statement slow (a long recursive CTE first) through the same off-thread runner."""
    home = _home(tmp_path, 10)
    driver = (
        "import runpy, signal, sys\n"
        "ns = runpy.run_path(sys.argv[1], run_name='not_main')\n"
        "signal.signal(signal.SIGTERM, ns['_on_sigterm'])\n"
        "live = ns['_quick_check'].__globals__  # run_path returns a copy; patch the module's own globals\n"
        "real = live['_run_interruptibly']\n"
        "def slow(conn, work):\n"
        "    slow_sql = 'WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n) SELECT count(*) FROM n'\n"
        "    return real(conn, lambda: (conn.execute(slow_sql).fetchall(), work())[1])\n"
        "live['_run_interruptibly'] = slow\n"
        "ns['preflight_state_db'](__import__('pathlib').Path(sys.argv[2]), reporter=ns['_ProgressReporter']())\n")
    proc = subprocess.Popen([sys.executable, "-I", "-S", "-u", "-c", driver, str(SCRIPT), str(home)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=NO_CLONE)
    try:
        seen = []
        wait_until(lambda: seen.append(proc.stderr.readline()) or '"backup_check"' in seen[-1] or
                   proc.poll() is not None, timeout=60, what="the check phase")
        assert proc.poll() is None, "".join(seen)
        signalled = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=30)
        latency = time.monotonic() - signalled
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert proc.returncode == 128 + signal.SIGTERM
    assert latency < 5, latency
    assert sorted(p.name for p in home.glob("state.db.pre-update-emergency-*")) == []


# ── copy-on-write clone ─────────────────────────────────────────────────────────────────────────

@pytest.fixture
def clone_base(tmp_path):
    """A directory whose filesystem can clone (APFS; btrfs/xfs via HERMES_TEST_REFLINK_DIR)."""
    from hermes_cli.backup_sqlite import _clone_file

    base = Path(os.environ.get("HERMES_TEST_REFLINK_DIR") or tmp_path)
    base = base / f"clone-{os.getpid()}-{time.monotonic_ns()}"
    base.mkdir(parents=True)
    probe = base / "probe"
    probe.write_bytes(b"x" * 4096)
    try:
        _clone_file(probe, base / "probe.clone")
    except OSError as exc:
        pytest.skip(f"no copy-on-write clone on this filesystem: {exc}")
    finally:
        for name in ("probe", "probe.clone"):
            (base / name).unlink(missing_ok=True)
    yield base
    subprocess.run(["rm", "-rf", str(base)], check=False)


def _wal_writer(db: Path):
    writer = sqlite3.connect(db, isolation_level=None, check_same_thread=False)
    writer.execute("PRAGMA wal_autocheckpoint=0")
    return writer


@pytest.mark.platforms("posix")
def test_clone_captures_wal_commits_made_while_it_waited_for_the_lock(clone_base):
    home = _home(clone_base, 200)
    db = home / "state.db"
    writer = _wal_writer(db)
    try:
        writer.execute("INSERT INTO messages VALUES ('committed in WAL')")
        assert Path(f"{db}-wal").stat().st_size > 0
        # Uncommitted when the pre-flight starts: it waits for the write lock, then sees the commit.
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO messages VALUES ('committed while the clone waited')")
        proc = subprocess.Popen([sys.executable, "-I", "-S", str(SCRIPT), str(home)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        # The child says when it found the lock held and started waiting; only then commit.
        first = proc.stderr.readline()
        assert json.loads(first) == {"event": "backup_lock_wait"}, first
        writer.execute("COMMIT")
        stdout, rest = proc.communicate(timeout=60)
        stderr = first + rest
        assert proc.returncode == 0, stderr
        result = json.loads(stdout)
        # Written after the clone: must not be in the snapshot.
        writer.execute("INSERT INTO messages VALUES ('after the snapshot')")
    finally:
        writer.close()

    assert result["method"] == "clone", stderr
    assert 0 < result["lock_wait_ms"] < 2000 and result["lock_ms"] < 2000
    snapshot = Path(result["path"])
    # One self-contained file: the clone's WAL was folded in (opening the snapshot below recreates one).
    assert not Path(f"{snapshot}-wal").exists() and not list(home.glob("*.partial*"))
    rows = _snapshot_rows(result["path"])
    assert rows[-2:] == [("committed in WAL",), ("committed while the clone waited",)]
    assert len(rows) == 202
    events = _events(stderr)
    # One instant 100% line, and no whole-database check on a byte-identical clone.
    assert events == [{"event": "backup_lock_wait"},
                      {"event": "backup_progress", "done_bytes": result["bytes"],
                       "total_bytes": result["bytes"], "method": "clone"}]
    assert oct(snapshot.stat().st_mode & 0o777) == "0o600"
    assert _lock_is_free(db)


@pytest.mark.platforms("posix")
def test_a_writer_holding_the_lock_sends_the_snapshot_to_the_copy(clone_base):
    home = _home(clone_base, 50)
    db = home / "state.db"
    writer = _wal_writer(db)
    try:
        writer.execute("INSERT INTO messages VALUES ('committed')")
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO messages VALUES ('never committed')")
        result, events, _elapsed = _run(home)
        writer.execute("ROLLBACK")
    finally:
        writer.close()
    assert result["method"] == "copy" and "locked" in result["clone_error"]
    assert _snapshot_rows(result["path"])[-1] == ("committed",)
    assert {e.get("method") for e in events if e["event"] == "backup_progress"} == {"copy"}
    assert _lock_is_free(db)


@pytest.mark.platforms("posix")
def test_clone_holds_the_write_lock_only_while_cloning(tmp_path, monkeypatch):
    """Lock lifecycle on every path, with a stand-in clone (another process's cp) so it runs on any
    filesystem: held during the clone, released after success, clone failure and a bad clone, and
    never held during the fallback copy."""
    from hermes_cli import backup_sqlite

    home = _home(tmp_path, 50)
    db = home / "state.db"
    seen: dict = {}

    def cp_clone(src, dst, _fd=None):
        if Path(src) == db:  # not the support probe, which runs before the lock
            seen.setdefault("held_during_clone", not _lock_is_free(db))
        subprocess.run(["cp", str(src), str(dst)], check=True)

    real_copy = backup_sqlite._safe_copy_db

    def watching_copy(*args, **kwargs):
        seen["free_during_copy"] = _lock_is_free(db)
        return real_copy(*args, **kwargs)

    monkeypatch.setattr(backup_sqlite, "_safe_copy_db", watching_copy)
    monkeypatch.setattr(backup_sqlite, "_clone_file", cp_clone)
    result = backup_sqlite.preflight_state_db(home, clone=True)
    assert result["method"] == "clone" and "clone_error" not in result
    assert seen == {"held_during_clone": True}
    assert _lock_is_free(db)

    def unsupported(src, dst, _fd=None):
        raise OSError(errno.EXDEV, "cross-device", str(src))

    def must_not_lock(*_args):
        raise AssertionError("took the write lock on a filesystem that cannot clone")

    monkeypatch.setattr(backup_sqlite, "_clone_file", unsupported)
    monkeypatch.setattr(backup_sqlite, "_clone_snapshot", must_not_lock)
    out = io.StringIO()
    result = backup_sqlite.preflight_state_db(home, clone=True, reporter=backup_sqlite._ProgressReporter(out))
    assert result["method"] == "copy" and "cross-device" in result["clone_error"]
    monkeypatch.undo()
    monkeypatch.setattr(backup_sqlite, "_safe_copy_db", watching_copy)
    assert seen["free_during_copy"] is True and _lock_is_free(db)
    assert len(_snapshot_rows(result["path"])) == 50
    assert _events(out.getvalue())[-1] == {"event": "backup_check"}

    def garbage_clone(src, dst, _fd=None):
        Path(dst).write_bytes(b"not a database" * 100)

    seen.clear()
    monkeypatch.setattr(backup_sqlite, "_clone_file", garbage_clone)
    result = backup_sqlite.preflight_state_db(home, clone=True)
    assert result["method"] == "copy" and "header" in result["clone_error"]
    assert seen["free_during_copy"] is True and _lock_is_free(db)
    assert not list(home.glob("*.partial*"))


def test_the_clone_path_can_be_turned_off(tmp_path, monkeypatch):
    from hermes_cli import backup_sqlite

    def must_not_clone(*_args):
        raise AssertionError("clone attempted")

    home = _home(tmp_path, 5)
    monkeypatch.setattr(backup_sqlite, "_clone_file", must_not_clone)
    monkeypatch.setenv("HERMES_PREFLIGHT_CLONE", "0")
    assert backup_sqlite.preflight_state_db(home, clone=True)["method"] == "copy"


@pytest.mark.platforms("posix")
@pytest.mark.skipif(not hasattr(sqlite3, "SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE"), reason="needs Python 3.12+")
def test_the_clone_never_writes_the_source_database(clone_base):
    """The lock connection may be the last one on state.db; closing it must not fold the WAL in."""
    home = _home(clone_base, 20)
    db = home / "state.db"
    writer = _wal_writer(db)
    writer.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
    writer.execute("INSERT INTO messages VALUES ('only in the WAL')")
    writer.close()
    assert Path(f"{db}-wal").stat().st_size > 0
    before = db.read_bytes()

    result, _events_seen, _elapsed = _run(home)

    assert result["method"] == "clone"
    assert db.read_bytes() == before
    assert _snapshot_rows(result["path"])[-1] == ("only in the WAL",)


def test_without_python_3_12_the_clone_is_skipped_for_the_copy(tmp_path, monkeypatch):
    """Without SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE the lock connection's close could checkpoint the WAL
    into state.db on the main thread, where SIGTERM cannot stop it: copy instead, and say why."""
    from hermes_cli import backup_sqlite

    def must_not_clone(*_args):
        raise AssertionError("clone attempted")

    home = _home(tmp_path, 5)
    monkeypatch.delattr(backup_sqlite.sqlite3, "SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE", raising=False)
    monkeypatch.setattr(backup_sqlite, "_clone_file", must_not_clone)
    result = backup_sqlite.preflight_state_db(home, clone=True)
    assert result["method"] == "copy"
    assert "Python 3.12" in result["clone_error"]


def _cp_clone(src, dst, _fd=None):
    """A stand-in clone (another process's byte copy) so the lock-window tests run on any filesystem."""
    subprocess.run(["cp", str(src), str(dst)], check=True)


@pytest.mark.platforms("posix")
@pytest.mark.skipif(not hasattr(sqlite3, "SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE"), reason="needs Python 3.12+")
def test_a_checkpoint_between_the_db_and_wal_clones_loses_no_committed_row(tmp_path, monkeypatch):
    """Under the snapshot's write lock no one can commit, but a passive checkpoint may still move WAL
    frames into state.db between the two clones. The WAL clone keeps those frames, so the snapshot
    still holds every committed row."""
    from hermes_cli import backup_sqlite

    home = _home(tmp_path, 10)
    db = home / "state.db"
    writer = _wal_writer(db)
    writer.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
    for n in range(50):
        writer.execute("INSERT INTO messages VALUES (?)", (f"row {n}",))
    writer.close()
    with sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True) as reader:
        committed = reader.execute("SELECT body FROM messages ORDER BY rowid").fetchall()
    assert len(committed) == 60
    seen: dict = {}

    def checkpoint_between(src, dst, fd=None):
        _cp_clone(src, dst, fd)
        if Path(src) != db:
            return
        other = sqlite3.connect(db, timeout=0, isolation_level=None)
        other.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute("INSERT INTO messages VALUES ('during the clone')")
            seen["checkpoint"] = other.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        finally:
            other.close()

    monkeypatch.setattr(backup_sqlite, "_clone_file", checkpoint_between)
    result = backup_sqlite.preflight_state_db(home, clone=True)

    busy, frames, moved = seen["checkpoint"]
    assert busy == 0 and frames > 0 and moved == frames, seen
    assert result["method"] == "clone", result
    assert _snapshot_rows(result["path"]) == committed
    with sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True) as reader:
        assert reader.execute("SELECT body FROM messages ORDER BY rowid").fetchall() == committed
    assert _lock_is_free(db)


@pytest.mark.platforms("posix")
@pytest.mark.skipif(not hasattr(sqlite3, "SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE"), reason="needs Python 3.12+")
def test_sigterm_during_the_clone_verify_exits_cleanly(tmp_path):
    home = _home(tmp_path, 10)
    db = home / "state.db"
    writer = _wal_writer(db)
    writer.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
    writer.execute("INSERT INTO messages VALUES ('in the WAL')")
    writer.close()
    driver = (
        "import runpy, signal, subprocess, sys\n"
        "ns = runpy.run_path(sys.argv[1], run_name='not_main')\n"
        "signal.signal(signal.SIGTERM, ns['_on_sigterm'])\n"
        "live = ns['_verify_clone'].__globals__  # run_path returns a copy; patch the module's own globals\n"
        "live['_clone_file'] = lambda src, dst, fd=None: subprocess.run(['cp', str(src), str(dst)], check=True)\n"
        "real = live['_run_interruptibly']\n"
        "def slow(conn, work):\n"
        "    sys.stderr.write('verifying\\n'); sys.stderr.flush()\n"
        "    slow_sql = 'WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n) SELECT count(*) FROM n'\n"
        "    return real(conn, lambda: (conn.execute(slow_sql).fetchall(), work())[1])\n"
        "live['_run_interruptibly'] = slow\n"
        "ns['preflight_state_db'](__import__('pathlib').Path(sys.argv[2]), clone=True,\n"
        "                         reporter=ns['_ProgressReporter']())\n")
    proc = subprocess.Popen([sys.executable, "-I", "-S", "-u", "-c", driver, str(SCRIPT), str(home)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        seen = []
        wait_until(lambda: seen.append(proc.stderr.readline()) or seen[-1] == "verifying\n" or
                   proc.poll() is not None, timeout=60, what="the clone verify")
        assert proc.poll() is None, "".join(seen)
        assert _lock_is_free(db), "the write lock must be released before the verify"
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert proc.returncode == 128 + signal.SIGTERM
    assert sorted(p.name for p in home.glob("state.db.pre-update-emergency-*")) == []
    assert _lock_is_free(db)
