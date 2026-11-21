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
    assert events.index(final) == len(events) - 2


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
        time.sleep(0.3)  # health: allow HX013 -- the lock holder must still be holding when the child asks
        writer.execute("COMMIT")
        stdout, stderr = proc.communicate(timeout=60)
        assert proc.returncode == 0, stderr
        result = json.loads(stdout)
        # Written after the clone: must not be in the snapshot.
        writer.execute("INSERT INTO messages VALUES ('after the snapshot')")
    finally:
        writer.close()

    assert result["method"] == "clone", stderr
    assert result["lock_ms"] < 2000
    snapshot = Path(result["path"])
    # One self-contained file: the clone's WAL was folded in (opening the snapshot below recreates one).
    assert not Path(f"{snapshot}-wal").exists() and not list(home.glob("*.partial*"))
    rows = _snapshot_rows(result["path"])
    assert rows[-2:] == [("committed in WAL",), ("committed while the clone waited",)]
    assert len(rows) == 202
    events = _events(stderr)
    # One instant 100% line, and no whole-database check on a byte-identical clone.
    assert events == [{"event": "backup_progress", "done_bytes": result["bytes"],
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

    monkeypatch.setattr(backup_sqlite, "_clone_file", unsupported)
    out = io.StringIO()
    result = backup_sqlite.preflight_state_db(home, clone=True, reporter=backup_sqlite._ProgressReporter(out))
    assert result["method"] == "copy" and "cross-device" in result["clone_error"]
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
