"""A structural-error quarantine must not freeze the gateway's Python threads.

A competing writer can acquire the connection after _execute_write releases its
Python lock to classify corruption. CPython setconfig keeps the GIL while waiting
for SQLite's connection mutex; a Python SQL callback needs that GIL to finish.
Run the actual write/error/quarantine path in a disposable process so the old
lock inversion cannot wedge pytest itself.
"""

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from tools.environments.local import build_subprocess_env


_PROBE = r'''
import sqlite3
import sys
import threading
from pathlib import Path
from hermes_state import SessionDB, StateDbCorruptError

db = SessionDB(db_path=Path(sys.argv[1]) / "state.db")
entered = threading.Event()
writer_errors = []
workers = []
original_halt = db._halt_db_corrupt

def sql_callback():
    entered.set()
    # Releases the GIL but retains SQLite's connection mutex. This callback
    # must reacquire the GIL to return to SQLite and release that mutex.
    threading.Event().wait(5.0)
    return 1

db._conn.create_function("contended_callback", 0, sql_callback)

def competing_writer():
    try:
        db._execute_write(lambda conn: conn.execute("SELECT contended_callback()").fetchone())
    except BaseException as exc:
        writer_errors.append(exc)

def halt_after_competing_writer(exc, *, context=None):
    worker = threading.Thread(target=competing_writer)
    workers.append(worker)
    worker.start()
    assert entered.wait(10), "competing SQL callback did not acquire SQLite's mutex"
    print("entered-quarantine-with-competing-sql", flush=True)
    original_halt(exc, context=context)

db._halt_db_corrupt = halt_after_competing_writer

def malformed(_conn):
    raise sqlite3.DatabaseError("database disk image is malformed")

try:
    db._execute_write(malformed)
except StateDbCorruptError:
    pass
else:
    raise AssertionError("structural-error write did not quarantine")
for worker in workers:
    worker.join(10)
    assert not worker.is_alive(), "competing writer did not finish"
assert not writer_errors, writer_errors
assert db._db_corrupt
assert db._conn.getconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE)
db.close()
print("quarantine-completed", flush=True)
'''


def test_corrupt_write_quarantine_waits_without_holding_gil(tmp_path):
    if not hasattr(sqlite3.Connection, "setconfig"):
        pytest.skip("CPython setconfig lock inversion requires Python 3.12+")
    env = build_subprocess_env()
    env["HERMES_HOME"] = str(tmp_path / "home")
    try:
        result = subprocess.run(
            [sys.executable, "-c", _PROBE, str(tmp_path)],
            env=env, cwd=Path(__file__).resolve().parents[2],
            capture_output=True, text=True, encoding="utf-8", timeout=25, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(f"quarantine deadlocked against competing SQLite callback: {exc.stdout!r}")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "quarantine-completed" in result.stdout


def test_close_winning_quarantine_race_disables_internal_checkpoint(tmp_path, monkeypatch):
    import hermes_state as state

    if not hasattr(sqlite3.Connection, "setconfig"):
        pytest.skip("close-time checkpoint switch requires Python 3.12+")
    db = state.SessionDB(db_path=tmp_path / "state.db")
    armed_at_close = []
    real_close = db._close_connection_quietly
    real_mark = state.mark_storage_corrupt

    def record_close(conn):
        armed_at_close.append(conn.getconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE))
        real_close(conn)

    def close_after_publication(path, reason):
        real_mark(path, reason)
        # The close thread can win _lock while halt is publishing the latch.
        db.close()

    def malformed(_conn):
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(db, "_close_connection_quietly", record_close)
    monkeypatch.setattr(state, "mark_storage_corrupt", close_after_publication)
    try:
        with pytest.raises(state.StateDbCorruptError):
            db._execute_write(malformed)
        assert armed_at_close == [True], "close checkpointed before the quarantine switch was armed"
    finally:
        db.close()
