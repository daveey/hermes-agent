"""Darwin write-lock holder diagnostic.

Linux reads ``/proc/locks``. Darwin has no such file, and opening the db or ``-shm`` in the
waiter drops every fcntl lock that process holds (``close`` releases them all). The probe
runs in a child. An unsupported platform must say the holder is unknown, not that it released.
"""

import json
import logging
import os
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import hermes_state_lockowners as lockowners
from hermes_state_lockowners import (
    darwin_write_lock_probes,
    format_write_lock_holder_detail,
    lines_from_darwin_hits,
    lock_owner_probe_kind,
    log_write_lock_holders,
    state_db_write_lock_holders,
)
from tests.waiting import wait_until

_RELEASED = (
    "no write-class lock held at the deadline (the holder released just before it)"
)


def test_unprobed_detail_says_holder_unknown_not_released():
    detail = format_write_lock_holder_detail(False, ["should not be used"])
    assert "unknown" in detail
    assert "released" not in detail
    assert "no write-class lock held" not in detail
    assert detail == (
        "write-lock holder unknown (this platform has no lock-owner diagnostic)"
    )


def test_empty_completed_probe_still_says_holder_released():
    assert format_write_lock_holder_detail(True, []) == _RELEASED
    named = format_write_lock_holder_detail(
        True, ["PID 1 (x) holds WAL write lock on state.db-shm"]
    )
    assert "released" not in named
    assert " | " not in named


def test_probe_kind_is_linux_prefix_darwin_or_unsupported():
    assert lock_owner_probe_kind("linux") == "linux"
    assert lock_owner_probe_kind("linux2") == "linux"
    assert lock_owner_probe_kind("darwin") == "darwin"
    assert lock_owner_probe_kind("win32") == ""
    assert lock_owner_probe_kind("freebsd") == ""


def test_darwin_probe_offsets_are_wal_write_class_and_pending_reserved():
    assert set(darwin_write_lock_probes()) == {
        ("-shm", 120),
        ("-shm", 121),
        ("-shm", 122),
        ("", 0x40000000),
        ("", 0x40000001),
    }
    assert len(darwin_write_lock_probes()) == 5


def test_darwin_hits_name_each_write_class_byte_and_ignore_read_slots():
    lines = lines_from_darwin_hits(
        "state.db",
        [
            {"sidecar": "-shm", "start": 120, "pid": 42},
            {"sidecar": "-shm", "start": 121, "pid": 42},
            {"sidecar": "-shm", "start": 122, "pid": 42},
            {"sidecar": "-shm", "start": 123, "pid": 42},
            {"sidecar": "", "start": 0x40000000, "pid": 7},
            {"sidecar": "", "start": 0x40000001, "pid": -1},
            {"sidecar": "", "start": 0x40000002, "pid": 7},
        ],
    )
    assert [line.split(" holds ", 1)[1] for line in lines] == [
        "WAL write lock on state.db-shm",
        "WAL checkpoint lock on state.db-shm",
        "WAL recover lock on state.db-shm",
        "PENDING lock on state.db",
        "RESERVED lock on state.db",
    ]
    assert lines[0].startswith("PID 42 ")
    assert "OFD lock, owner pid not exported by the kernel" in lines[-1]


def test_log_says_unknown_when_platform_has_no_probe(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(lockowners, "lock_owner_probe_kind", lambda _platform: "")
    db = tmp_path / "state.db"
    with caplog.at_level(logging.WARNING, logger="hermes_state_lockowners"):
        log_write_lock_holders(db, 60)
    assert "unknown" in caplog.text
    assert "released" not in caplog.text
    assert "no write-class lock held" not in caplog.text
    assert "state.db" in caplog.text


def test_probe_timeout_logs_unknown_not_released(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(lockowners, "lock_owner_probe_kind", lambda _platform: "darwin")

    def _timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="probe", timeout=5)

    monkeypatch.setattr(lockowners.subprocess, "run", _timeout)
    with caplog.at_level(logging.WARNING, logger="hermes_state_lockowners"):
        log_write_lock_holders(tmp_path / "state.db", 60)
    assert "unknown" in caplog.text
    assert "exceeded 5s" in caplog.text
    assert "released" not in caplog.text


def test_probe_garbage_logs_unknown_not_released(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(lockowners, "lock_owner_probe_kind", lambda _platform: "darwin")

    def _garbage(args, **_kwargs):
        return subprocess.CompletedProcess(args, 0, stdout="not-json", stderr="")

    monkeypatch.setattr(lockowners.subprocess, "run", _garbage)
    with caplog.at_level(logging.WARNING, logger="hermes_state_lockowners"):
        log_write_lock_holders(tmp_path / "state.db", 60)
    assert "unknown" in caplog.text
    assert "no result" in caplog.text
    assert "released" not in caplog.text


def test_darwin_spawn_passes_the_probe_plan_to_a_bounded_child(tmp_path, monkeypatch):
    monkeypatch.setattr(lockowners, "lock_owner_probe_kind", lambda _platform: "darwin")
    captured = {}

    def _fake_run(args, **kwargs):
        captured["args"] = args
        captured["timeout"] = kwargs.get("timeout")
        payload = '{"probed": true, "base": "state.db", "hits": []}'
        return subprocess.CompletedProcess(args, 0, stdout=payload, stderr="")

    monkeypatch.setattr(lockowners.subprocess, "run", _fake_run)
    assert state_db_write_lock_holders(tmp_path / "state.db") == []
    assert captured["args"][0] == sys.executable
    assert captured["args"][1] == "-c"
    assert captured["args"][2] == lockowners._DARWIN_PROBE_SCRIPT
    asked = {
        (item["sidecar"], item["start"]) for item in json.loads(captured["args"][4])
    }
    assert asked == set(darwin_write_lock_probes())
    assert captured["timeout"] == lockowners._DARWIN_PROBE_TIMEOUT_S
    assert 0 < captured["timeout"] <= 5


def _init_db(path: Path, journal_mode: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA journal_mode={journal_mode}")
    conn.execute("CREATE TABLE t(x)")
    conn.commit()
    conn.close()


def _hold_immediate(db: Path) -> subprocess.Popen:
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            textwrap.dedent(
                f"""
                import sqlite3, sys
                c = sqlite3.connect({str(db)!r}, isolation_level=None)
                c.execute("BEGIN IMMEDIATE")
                c.execute("INSERT INTO t VALUES (1)")
                print("held", flush=True)
                sys.stdin.read()
                """
            ),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    line = proc.stdout.readline()
    if line.strip() != "held":
        err = proc.stderr.read() if proc.stderr is not None else ""
        proc.kill()
        proc.wait(timeout=5)
        raise AssertionError(f"holder did not take the lock: {line!r} {err!r}")
    return proc


def _stop_holder(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if proc.stdin is not None and not proc.stdin.closed:
        proc.stdin.close()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _file_bytes(db: Path) -> dict[str, bytes]:
    return {
        path.name: path.read_bytes()
        for path in (db, Path(str(db) + "-wal"), Path(str(db) + "-shm"))
        if path.exists()
    }


def _compete(db: Path) -> str:
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent(
                f"""
                import sqlite3, sys
                c = sqlite3.connect({str(db)!r}, timeout=0, isolation_level=None)
                try:
                    c.execute("BEGIN IMMEDIATE")
                except sqlite3.OperationalError as exc:
                    print("blocked", exc)
                    raise SystemExit(2)
                print("acquired")
                """
            ),
        ],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    return proc.stdout.strip()


@pytest.mark.platforms("macos")
def test_live_subprocess_writer_is_named_and_files_are_not_written(tmp_path, caplog):
    db = tmp_path / "state.db"
    _init_db(db, "wal")
    holder = _hold_immediate(db)
    try:
        before = _file_bytes(db)
        lines = state_db_write_lock_holders(db)
        assert len(lines) == 1, lines
        assert f"PID {holder.pid} " in lines[0]
        assert "WAL write lock on state.db-shm" in lines[0]
        assert _file_bytes(db) == before
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="hermes_state_lockowners"):
            log_write_lock_holders(db, 60)
        assert f"PID {holder.pid} " in caplog.text
        assert "WAL write" in caplog.text
        assert "released" not in caplog.text
        assert "unknown" not in caplog.text
    finally:
        _stop_holder(holder)
    wait_until(
        lambda: state_db_write_lock_holders(db) == [],
        timeout=2,
        what="write lock released after holder exit",
    )
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="hermes_state_lockowners"):
        log_write_lock_holders(db, 60)
    assert _RELEASED in caplog.text
    assert "unknown" not in caplog.text


@pytest.mark.platforms("macos")
def test_probe_does_not_drop_parent_sqlite_locks(tmp_path):
    # This process holds the lock. Do not open the db, wal, or shm here: Darwin close()
    # would drop it and the competitor assertion would fail for the wrong reason.
    db = tmp_path / "state.db"
    _init_db(db, "wal")
    parent = sqlite3.connect(db, isolation_level=None)
    try:
        parent.execute("BEGIN IMMEDIATE")
        parent.execute("INSERT INTO t VALUES (1)")
        assert _compete(db).startswith("blocked"), "lock was not held before the probe"
        lines = state_db_write_lock_holders(db)
        assert any(
            f"PID {os.getpid()} " in line and "WAL write lock on state.db-shm" in line
            for line in lines
        ), lines
        assert _compete(db).startswith("blocked"), lines
        parent.execute("INSERT INTO t VALUES (2)")
        parent.commit()
        assert parent.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 2
    finally:
        parent.close()
    assert _compete(db).startswith("acquired")


@pytest.mark.platforms("macos")
def test_rollback_immediate_holder_is_named_reserved(tmp_path):
    db = tmp_path / "state.db"
    _init_db(db, "delete")
    holder = _hold_immediate(db)
    try:
        lines = state_db_write_lock_holders(db)
        assert len(lines) == 1, lines
        assert f"PID {holder.pid} " in lines[0]
        assert f"RESERVED lock on {db.name}" in lines[0]
        assert "WAL write" not in lines[0]
    finally:
        _stop_holder(holder)


@pytest.mark.platforms("macos")
def test_child_script_reports_only_the_offsets_it_is_given(tmp_path):
    db = tmp_path / "state.db"
    _init_db(db, "wal")
    holder = _hold_immediate(db)
    try:
        missed = _run_probe_script(db, [{"sidecar": "-shm", "start": 121}])
        assert missed["probed"] is True
        assert missed["hits"] == []
        found = _run_probe_script(db, [{"sidecar": "-shm", "start": 120}])
        assert found["hits"] == [{"sidecar": "-shm", "start": 120, "pid": holder.pid}]
    finally:
        _stop_holder(holder)


def _run_probe_script(db: Path, probes: list[dict]) -> dict:
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            lockowners._DARWIN_PROBE_SCRIPT,
            str(db),
            json.dumps(probes),
        ],
        capture_output=True,
        text=True,
        timeout=lockowners._DARWIN_PROBE_TIMEOUT_S,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)
