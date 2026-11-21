"""A fresh ``hermes kanban`` process must not re-scan the whole board DB, nor queue behind the others that do.

2026-11-21, hub: every ``hermes kanban show/create/archive`` took 9-20 s. cProfile of the installed launcher:
9.2 s asleep in ``_cross_process_init_lock`` (another CLI held ``kanban.db.init.lock``) + 2.6 s in
``PRAGMA integrity_check`` of the 700 MB board; imports and argparse were under 1.5 s. ``connect()`` ran the full
integrity probe once PER PROCESS (the ``_INITIALIZED_PATHS`` cache is in-process) while holding the cross-process lock,
so N concurrent CLIs cost N serial scans and the waiters' 10 s bound expired (the hub scaler's 10 s subprocess
ceiling, then 30 s, was only the symptom). A clean full check now leaves a verified stamp next to the DB: processes
that find a fresh stamp for the same file skip the O(DB) scan.

Real subprocesses (not a mocked ``connect``) enter through ``kanban_db_connect.connect`` like the CLI does.
"""
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

CHILD = r"""
import os, sys, threading
from pathlib import Path
from hermes_cli import kanban_db_connect as kbc

log = os.environ["CHECK_LOG"]
real = kbc._run_integrity_check

def counted(conn):
    with open(log, "a") as f:
        f.write(f"{os.getpid()}\n")
    threading.Event().wait(float(os.environ.get("CHECK_SLEEP_S", "0")))  # a slow scan, as on a 700 MB board
    return real(conn)

kbc._run_integrity_check = counted
if "CHECK_TTL_S" in os.environ:
    kbc._INTEGRITY_STAMP_TTL_SECONDS = float(os.environ["CHECK_TTL_S"])
kbc.connect(Path(sys.argv[1])).close()
"""


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    for key in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_RUN_ID"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    from hermes_cli import kanban_db_connect as kbc
    path = home / "kanban.db"
    kbc.init_db(path)
    kbc._INITIALIZED_PATHS.discard(str(path.resolve()))
    return path


def _env(tmp_path, board_path, **extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("HERMES_KANBAN_", "PYTEST"))}
    env.update(HERMES_HOME=str(board_path.parent), HERMES_KANBAN_HOME=str(board_path.parent),
               CHECK_LOG=str(tmp_path / "checks.log"), PYTHONPATH=str(REPO), **extra)
    return env


def _spawn(tmp_path, board_path, n, **extra):
    env = _env(tmp_path, board_path, **extra)
    procs = [subprocess.Popen([sys.executable, "-c", CHILD, str(board_path)], env=env, cwd=REPO,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(n)]
    try:
        for p in procs:
            _out, err = p.communicate(timeout=120)
            assert p.returncode == 0, err[-2000:]
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
            p.communicate(timeout=10)
    log = tmp_path / "checks.log"
    return log.read_text().split() if log.exists() else []


def _stamp(board_path):
    return board_path.with_name(board_path.name + ".integrity-ok")


def test_concurrent_cli_processes_share_one_integrity_check(tmp_path, board):
    """Old: every process scanned (6 checks, serialized behind the init lock). Fixed: the first scans, the rest skip."""
    checks = _spawn(tmp_path, board, 6, CHECK_SLEEP_S="0.4")
    assert len(checks) == 1, checks
    assert _stamp(board).exists()


def test_next_process_after_a_clean_check_skips_the_scan(tmp_path, board):
    assert len(_spawn(tmp_path, board, 1)) == 1
    started = time.monotonic()
    assert len(_spawn(tmp_path, board, 1, CHECK_SLEEP_S="5")) == 1  # still the first run's single line: no second scan
    assert time.monotonic() - started < 5


def test_an_expired_stamp_forces_a_fresh_full_check(tmp_path, board):
    _spawn(tmp_path, board, 1)
    stamp = json.loads(_stamp(board).read_text())
    stamp["verified_at"] -= 7 * 3600
    _stamp(board).write_text(json.dumps(stamp))
    assert len(_spawn(tmp_path, board, 6, CHECK_SLEEP_S="0.4")) == 2


def test_a_stamp_for_a_replaced_file_is_ignored(tmp_path, board):
    _spawn(tmp_path, board, 1)
    data = board.read_bytes()
    replacement = board.with_name("kanban.db.restore")
    replacement.write_bytes(data)  # allocated while the old file still holds its inode number
    for suffix in ("-wal", "-shm"):
        Path(str(board) + suffix).unlink(missing_ok=True)
    os.replace(replacement, board)  # same bytes, new inode: the stamp vouched for another file
    assert len(_spawn(tmp_path, board, 1)) == 2


def test_ttl_zero_disables_the_stamp(tmp_path, board):
    assert len(_spawn(tmp_path, board, 2, CHECK_TTL_S="0")) == 2
    assert not _stamp(board).exists()


def test_corruption_is_still_caught_when_the_check_runs_and_never_stamped(tmp_path, board):
    from hermes_cli import kanban_db_connect as kbc
    conn = sqlite3.connect(board)
    conn.execute("CREATE TABLE IF NOT EXISTS pad(x TEXT)")
    conn.executemany("INSERT INTO pad VALUES (?)", [("x" * 500,)] * 3000)
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    raw = bytearray(board.read_bytes())
    for off in range(len(raw) // 2, len(raw) // 2 + 4096, 8):  # smash interior pages: the header stays valid
        raw[off] = 0xFF
    board.write_bytes(bytes(raw))
    for suffix in ("-wal", "-shm"):
        Path(str(board) + suffix).unlink(missing_ok=True)
    kbc._INITIALIZED_PATHS.discard(str(board.resolve()))
    with pytest.raises((kbc.KanbanDbCorruptError, sqlite3.DatabaseError)):
        kbc.connect(board).close()
    assert not _stamp(board).exists()


def test_an_unwritable_stamp_never_fails_the_connect(board, monkeypatch):
    from hermes_cli import kanban_db_connect as kbc

    def refuse(*_a, **_k):
        raise OSError("read-only filesystem")
    monkeypatch.setattr(kbc.os, "replace", refuse)
    kbc._INITIALIZED_PATHS.discard(str(board.resolve()))
    kbc.connect(board).close()
