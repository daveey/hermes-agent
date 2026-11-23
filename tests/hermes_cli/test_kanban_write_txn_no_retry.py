"""Contract tests for the opt-in ``write_txn(no_retry=..., boundary_done=...)`` primitive.

Real isolated SQLite files with two connections; no production caller is involved.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

PRIOR_TIMEOUT_MS = 4321

# Independent process: LOCK_EX|LOCK_NB on the same path. Exit 0 acquired, 2 busy.
_FLOCK_PROBE = (
    "import fcntl,sys\n"
    "f=open(sys.argv[1],'ab')\n"
    "try:\n"
    "    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
    "except BlockingIOError:\n"
    "    raise SystemExit(2)\n"
    "raise SystemExit(0)\n"
)


def _db(tmp_path: Path) -> Path:
    db = tmp_path / "kanban.db"
    kb._INITIALIZED_PATHS.discard(str(db.resolve()))
    kb.init_db(db_path=db)
    return db


def _open(db: Path) -> sqlite3.Connection:
    conn = kbc.connect(db_path=db)
    conn.execute(f"PRAGMA busy_timeout={PRIOR_TIMEOUT_MS}")
    return conn


def _timeout(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA busy_timeout").fetchone()[0]


def _probe_flock(path: Path) -> int:
    return subprocess.run(
        [sys.executable, "-c", _FLOCK_PROBE, str(path)],
        check=False,
    ).returncode


@pytest.fixture
def two(tmp_path):
    db = _db(tmp_path)
    a, b = _open(db), _open(db)
    yield a, b
    a.close()
    b.close()


@pytest.fixture
def no_sleep(monkeypatch):
    sleeps = []
    monkeypatch.setattr(kbc.time, "sleep", lambda s: sleeps.append(s))
    return sleeps


def test_begin_contention_single_attempt_zero_timeout(two, no_sleep, monkeypatch):
    a, b = two
    b.execute("BEGIN IMMEDIATE")
    seen, order = [], []
    attempts = []
    raw = a.execute

    def execute(sql, *args):
        if sql == "BEGIN IMMEDIATE":
            attempts.append(sql)
            assert raw("PRAGMA busy_timeout").fetchone()[0] == 0
        return raw(sql, *args)

    monkeypatch.setattr(a, "execute", execute)

    def done():
        seen.append(_timeout(a))
        order.append("done")

    with pytest.raises(sqlite3.OperationalError, match="locked|busy"):
        with kbc.write_txn(a, no_retry=True, boundary_done=done):
            order.append("body")
    assert order == ["done"]
    assert attempts == ["BEGIN IMMEDIATE"]
    assert seen == [PRIOR_TIMEOUT_MS]  # restored BEFORE callback
    assert no_sleep == []  # no Python BEGIN retry
    assert _timeout(a) == PRIOR_TIMEOUT_MS
    assert not a.in_transaction
    b.execute("ROLLBACK")


def test_begin_zero_timeout_observed_while_attempting(two, monkeypatch):
    a, _ = two
    seen = []
    orig = kbc._execute_boundary_with_retry

    def spy(conn, sql):  # must not be used in no_retry mode
        seen.append(sql)
        return orig(conn, sql)

    monkeypatch.setattr(kbc, "_execute_boundary_with_retry", spy)
    inside = []
    with kbc.write_txn(a, no_retry=True):
        inside.append(_timeout(a))
    assert inside == [0]
    assert seen == []
    assert _timeout(a) == PRIOR_TIMEOUT_MS


def test_real_commit_contention_rollback_journal(tmp_path, no_sleep):
    """A rollback-journal reader's SHARED lock blocks the writer's COMMIT."""
    db = tmp_path / "rj.db"
    s = sqlite3.connect(str(db), isolation_level=None)
    s.execute("PRAGMA journal_mode=DELETE")
    s.execute("CREATE TABLE t(x)")
    s.close()
    a = sqlite3.connect(str(db), isolation_level=None, timeout=5)
    b = sqlite3.connect(str(db), isolation_level=None, timeout=0)
    calls = []
    # a: writer inside the txn; b holds a SHARED lock, so a's COMMIT (needs EXCLUSIVE) is BUSY.
    b.execute("BEGIN")
    b.execute("SELECT * FROM t").fetchall()
    order = []

    class Wrap:
        def __init__(self, r):
            self._r = r

        def execute(self, sql, *a_):
            calls.append(sql)
            if sql in {"BEGIN IMMEDIATE", "COMMIT"}:
                assert _timeout(self._r) == 0
            return self._r.execute(sql, *a_)

        def __getattr__(self, n):
            return getattr(self._r, n)

    w = Wrap(a)
    with pytest.raises(sqlite3.OperationalError, match="locked|busy"):
        with kbc.write_txn(w, no_retry=True, boundary_done=lambda: order.append(a.in_transaction)):
            w.execute("INSERT INTO t VALUES (1)")
    assert calls.count("COMMIT") == 1
    assert no_sleep == []
    assert order == [False]
    assert a.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    b.execute("ROLLBACK")
    assert a.execute("SELECT count(*) FROM t").fetchone()[0] == 0
    a.close()
    b.close()


def test_success_callback_before_all_hooks_and_after_commit(two, monkeypatch):
    a, b = two
    order = []
    monkeypatch.setattr(kbc._run_end_lease, "flush", lambda c, committed: order.append(("lease", committed)))
    monkeypatch.setattr(kbc._local_status_wake, "flush", lambda c: order.append("wake"))
    from hermes_cli import kanban_annotation_mutation as am

    orig = am.flush
    monkeypatch.setattr(am, "flush", lambda c, s: (order.append("annotation"), orig(c, s))[1])

    def done():
        # committed and visible from the other connection already
        order.append(("done", a.in_transaction, b.execute("SELECT count(*) FROM tasks WHERE title='x'").fetchone()[0], _timeout(a)))

    with kbc.write_txn(a, no_retry=True, boundary_done=done):
        a.execute(
            "INSERT INTO tasks (id,title,status,created_at) VALUES ('t1','x','todo',0)"
        )
    assert order[0] == ("done", False, 1, PRIOR_TIMEOUT_MS)
    assert order[1:] == ["annotation", ("lease", True), "wake"]


def test_rollback_callback_before_lease_flush(two, monkeypatch):
    a, _ = two
    order = []
    monkeypatch.setattr(kbc._run_end_lease, "flush", lambda c, committed: order.append(("lease", committed)))
    with pytest.raises(ValueError):
        with kbc.write_txn(a, no_retry=True, boundary_done=lambda: order.append(("done", a.in_transaction, _timeout(a)))):
            raise ValueError("body")
    assert order == [("done", False, PRIOR_TIMEOUT_MS), ("lease", False)]


def test_setup_failure_restores_and_calls_back(two, monkeypatch):
    a, _ = two
    order = []
    from hermes_cli import kanban_annotation_mutation as am

    def boom(conn):
        raise RuntimeError("arm failed")

    monkeypatch.setattr(am, "arm", boom)
    with pytest.raises(RuntimeError, match="arm failed"):
        with kbc.write_txn(a, no_retry=True, boundary_done=lambda: order.append((a.in_transaction, _timeout(a)))):
            order.append("body")
    assert order == [(False, PRIOR_TIMEOUT_MS)]
    assert _timeout(a) == PRIOR_TIMEOUT_MS


def test_callback_failure_after_commit_skips_hooks_committed_bytes_remain(two, monkeypatch):
    a, b = two
    hooks = []
    monkeypatch.setattr(kbc._local_status_wake, "flush", lambda c: hooks.append("wake"))
    monkeypatch.setattr(kbc._run_end_lease, "flush", lambda c, committed: hooks.append(("lease", committed)))
    from hermes_cli import kanban_annotation_mutation as am

    monkeypatch.setattr(am, "flush", lambda c, s: hooks.append("annotation"))

    def done():
        raise OSError("release failed")

    with pytest.raises(OSError, match="release failed"):
        with kbc.write_txn(a, no_retry=True, boundary_done=done):
            a.execute("INSERT INTO tasks (id,title,status,created_at) VALUES ('t2','y','todo',0)")
    assert hooks == [("lease", False)]  # local discard, not a network release
    assert _timeout(a) == PRIOR_TIMEOUT_MS
    assert b.execute("SELECT count(*) FROM tasks WHERE id='t2'").fetchone()[0] == 1


def test_callback_failure_does_not_shadow_body_error(two, monkeypatch):
    a, _ = two
    hooks = []
    monkeypatch.setattr(kbc._run_end_lease, "flush", lambda c, committed: hooks.append(("lease", committed)))

    def done():
        raise OSError("release failed")

    with pytest.raises(ValueError, match="body"):
        with kbc.write_txn(a, no_retry=True, boundary_done=done):
            raise ValueError("body")
    assert _timeout(a) == PRIOR_TIMEOUT_MS
    assert not a.in_transaction
    assert hooks == [("lease", False)]  # local discard, not a network release


@pytest.mark.parametrize("failure", ["body", "body_interrupt", "commit", "commit_interrupt", "callback", "restore"])
def test_failed_boundary_discards_real_pending_lease_queue(two, monkeypatch, failure):
    """Drain the actual id(conn)-keyed queue, including when cleanup fails."""
    from hermes_cli import kanban_db_sync as sync

    a, b = two
    leases = kbc._run_end_lease
    monkeypatch.setattr(sync, "board_mode", lambda conn: "primary")
    monkeypatch.setattr(sync, "run_uid", lambda conn, run_id: "queued-run")
    released = []
    monkeypatch.setattr(leases, "_release", lambda *args: released.append(args))
    raw = a.execute
    original = KeyboardInterrupt("original") if failure.endswith("interrupt") else ValueError("original")

    def execute(sql, *args):
        if sql == "COMMIT" and failure.startswith("commit"):
            raise original
        if sql == f"PRAGMA busy_timeout={PRIOR_TIMEOUT_MS}" and failure == "restore":
            raise original
        return raw(sql, *args)

    monkeypatch.setattr(a, "execute", execute)

    def done():
        # Deliberately suppress even BaseException from cleanup when the body or
        # COMMIT already failed: preserving the original error takes precedence.
        if failure == "callback":
            raise original
        if failure in {"body", "body_interrupt", "commit", "commit_interrupt"}:
            raise SystemExit("secondary cleanup error")

    try:
        with pytest.raises(type(original)) as caught:
            with kbc.write_txn(a, no_retry=True, boundary_done=done):
                a.execute("INSERT INTO tasks (id,title,status,created_at) VALUES ('tqueue','q','todo',0)")
                leases.note(a, "tqueue", 1)
                assert leases._PENDING[id(a)] == [("tqueue", "queued-run")]
                if failure.startswith("body"):
                    raise original
        assert caught.value is original
        assert id(a) not in leases._PENDING
        assert released == []  # no committed/network release, even after COMMIT
        assert not a.in_transaction
        committed = failure in {"callback", "restore"}
        assert b.execute("SELECT count(*) FROM tasks WHERE id='tqueue'").fetchone()[0] == int(committed)
        # A later unrelated commit must not inherit a stranded release.
        monkeypatch.setattr(a, "execute", raw)
        with kbc.write_txn(a):
            pass
        assert released == []
    finally:
        leases._PENDING.pop(id(a), None)


@pytest.mark.parametrize("phase", ["body", "commit"])
@pytest.mark.parametrize("error_type", [ValueError, KeyboardInterrupt])
@pytest.mark.parametrize("no_retry", [False, True])
def test_rollback_sqlite_error_closes_before_callback(two, monkeypatch, phase, error_type, no_retry):
    """A failed ROLLBACK must not release custody of a still-open writer."""
    from hermes_cli import kanban_annotation_mutation as am

    a, b = two
    original = error_type("original")
    order = []
    hooks = []
    raw_execute, raw_close = a.execute, a.close

    def execute(sql, *args):
        if sql == "ROLLBACK":
            assert a.in_transaction
            order.append("rollback_failed")
            raise sqlite3.DatabaseError("injected rollback failure")
        if sql == "COMMIT" and phase == "commit":
            raise original
        return raw_execute(sql, *args)

    def close():
        order.append("close")
        raw_close()

    def done():
        order.append("done")
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            raw_execute("SELECT 1")
        # Actual independent writer proves the close released SQLite's locks.
        b.execute("BEGIN IMMEDIATE")
        b.execute("ROLLBACK")
        raise SystemExit("secondary callback error")

    monkeypatch.setattr(a, "execute", execute)
    monkeypatch.setattr(a, "close", close)
    monkeypatch.setattr(am, "flush", lambda *args: hooks.append("annotation"))
    monkeypatch.setattr(kbc._local_status_wake, "flush", lambda *args: hooks.append("wake"))
    monkeypatch.setattr(kbc._run_end_lease, "_release", lambda *args: hooks.append("release"))
    with pytest.raises(error_type) as caught:
        with kbc.write_txn(a, no_retry=no_retry, boundary_done=done):
            a.execute("INSERT INTO tasks (id,title,status,created_at) VALUES ('trbf','rb','todo',0)")
            if phase == "body":
                raise original
    assert caught.value is original
    assert order == ["rollback_failed", "close", "done"]
    assert hooks == []
    assert b.execute("SELECT count(*) FROM tasks WHERE id='trbf'").fetchone()[0] == 0


def test_callback_runs_once(two):
    a, _ = two
    n = []
    with kbc.write_txn(a, no_retry=True, boundary_done=lambda: n.append(1)):
        pass
    assert n == [1]


@pytest.mark.parametrize("allow_nested", [False, True])
def test_nested_no_retry_refused(two, allow_nested):
    a, _ = two
    called = []
    with kbc.write_txn(a):
        with pytest.raises(RuntimeError, match="no_retry"):
            with kbc.write_txn(a, allow_nested=allow_nested, no_retry=True, boundary_done=lambda: called.append(1)):
                pass
        assert called == []
        assert _timeout(a) == PRIOR_TIMEOUT_MS
        assert a.in_transaction
    assert not a.in_transaction


def test_nested_callback_only_refused_without_invoking_callback(two):
    a, _ = two
    called = []
    with kbc.write_txn(a):
        with pytest.raises(RuntimeError, match="no_retry|boundary_done"):
            with kbc.write_txn(a, allow_nested=True, boundary_done=lambda: called.append(1)):
                called.append("body")
        assert called == []
        assert a.in_transaction
        assert _timeout(a) == PRIOR_TIMEOUT_MS
    assert not a.in_transaction


def test_begin_without_no_retry_callback_still_retries(two, no_sleep):
    a, b = two
    b.execute("BEGIN IMMEDIATE")
    order = []
    with pytest.raises(sqlite3.OperationalError, match="locked|busy"):
        with kbc.write_txn(a, boundary_done=lambda: order.append(("done", a.in_transaction, _timeout(a)))):
            order.append("body")
    assert order == [("done", False, PRIOR_TIMEOUT_MS)]
    assert len(no_sleep) == kbc._BUSY_MAX_RETRIES
    assert _timeout(a) == PRIOR_TIMEOUT_MS
    b.execute("ROLLBACK")


def test_opt_in_baseexception_rolls_back_before_callback(two, monkeypatch):
    a, b = two
    order = []
    monkeypatch.setattr(kbc._run_end_lease, "flush", lambda c, committed: order.append(("lease", committed)))
    monkeypatch.setattr(kbc._local_status_wake, "flush", lambda c: order.append("wake"))
    with pytest.raises(KeyboardInterrupt):
        with kbc.write_txn(
            a,
            no_retry=True,
            boundary_done=lambda: order.append(("done", a.in_transaction, _timeout(a))),
        ):
            a.execute(
                "INSERT INTO tasks (id,title,status,created_at) VALUES ('tki','ki','todo',0)"
            )
            raise KeyboardInterrupt()
    assert order[0] == ("done", False, PRIOR_TIMEOUT_MS)
    assert "wake" not in order
    assert ("lease", True) not in order
    assert order[1:] == [("lease", False)]  # rollback discards the local queue
    assert not a.in_transaction
    assert b.execute("SELECT count(*) FROM tasks WHERE id='tki'").fetchone()[0] == 0
    assert _timeout(a) == PRIOR_TIMEOUT_MS


def test_legacy_baseexception_leaves_transaction_open(two):
    a, _ = two
    with pytest.raises(KeyboardInterrupt):
        with kbc.write_txn(a):
            a.execute(
                "INSERT INTO tasks (id,title,status,created_at) VALUES ('tleg','l','todo',0)"
            )
            raise KeyboardInterrupt()
    assert a.in_transaction
    a.execute("ROLLBACK")
    assert not a.in_transaction


def test_timeout_restore_failure_skips_hooks_committed_bytes_remain(two, monkeypatch):
    a, b = two
    hooks = []
    released = []
    monkeypatch.setattr(kbc._local_status_wake, "flush", lambda c: hooks.append("wake"))
    monkeypatch.setattr(kbc._run_end_lease, "flush", lambda c, committed: hooks.append(("lease", committed)))
    from hermes_cli import kanban_annotation_mutation as am

    monkeypatch.setattr(am, "flush", lambda c, s: hooks.append("annotation"))
    raw = a.execute

    def execute(sql, *args):
        if isinstance(sql, str) and sql == f"PRAGMA busy_timeout={PRIOR_TIMEOUT_MS}":
            raise sqlite3.OperationalError("restore failed")
        return raw(sql, *args)

    a.execute = execute  # type: ignore[method-assign]
    with pytest.raises(sqlite3.OperationalError, match="restore failed"):
        with kbc.write_txn(a, no_retry=True, boundary_done=lambda: released.append(True)):
            a.execute("INSERT INTO tasks (id,title,status,created_at) VALUES ('t4','r','todo',0)")
    assert released == [True]
    assert hooks == [("lease", False)]  # local discard, not a network release
    assert b.execute("SELECT count(*) FROM tasks WHERE id='t4'").fetchone()[0] == 1


def test_timeout_restore_failure_does_not_shadow_body_error(two, monkeypatch):
    a, _ = two
    hooks = []
    released = []
    monkeypatch.setattr(kbc._run_end_lease, "flush", lambda c, committed: hooks.append(("lease", committed)))
    raw = a.execute

    def execute(sql, *args):
        if isinstance(sql, str) and sql == f"PRAGMA busy_timeout={PRIOR_TIMEOUT_MS}":
            raise sqlite3.OperationalError("restore failed")
        return raw(sql, *args)

    a.execute = execute  # type: ignore[method-assign]
    with pytest.raises(ValueError, match="body"):
        with kbc.write_txn(a, no_retry=True, boundary_done=lambda: released.append(True)):
            raise ValueError("body")
    assert released == [True]
    assert hooks == [("lease", False)]  # local discard, not a network release
    assert not a.in_transaction


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX flock custody barrier")
def test_postcommit_hook_acquires_released_flock_from_independent_process(two, tmp_path, monkeypatch):
    """boundary_done drops the fixture flock before postcommit hooks; a sibling
    process can then LOCK_EX|LOCK_NB. No production custody/signals."""
    import fcntl

    lock_path = tmp_path / "custody.barrier"
    lock_path.touch()
    holder = open(lock_path, "ab")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    order = []

    def done():
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()
        order.append("done")

    def wake(_conn):
        order.append(("wake", _probe_flock(lock_path)))

    monkeypatch.setattr(kbc._local_status_wake, "flush", wake)
    a, _ = two
    with kbc.write_txn(a, no_retry=True, boundary_done=done):
        order.append(("body", _probe_flock(lock_path)))
    assert order == [("body", 2), "done", ("wake", 0)]


def test_legacy_default_retries_and_keeps_timeout(two, no_sleep):
    a, b = two
    b.execute("BEGIN IMMEDIATE")
    a.execute("PRAGMA busy_timeout=0")
    with pytest.raises(sqlite3.OperationalError, match="locked|busy"):
        with kbc.write_txn(a):
            pass
    assert len(no_sleep) == kbc._BUSY_MAX_RETRIES  # existing retry behaviour unchanged
    b.execute("ROLLBACK")
    a.execute(f"PRAGMA busy_timeout={PRIOR_TIMEOUT_MS}")
    seen = []
    with kbc.write_txn(a):
        seen.append(_timeout(a))
    assert seen == [PRIOR_TIMEOUT_MS]


def test_legacy_nested_savepoint_unchanged(two):
    a, b = two
    with kbc.write_txn(a):
        with kbc.write_txn(a, allow_nested=True):
            a.execute("INSERT INTO tasks (id,title,status,created_at) VALUES ('t3','z','todo',0)")
    assert b.execute("SELECT count(*) FROM tasks WHERE id='t3'").fetchone()[0] == 1
    with kbc.write_txn(a):
        with pytest.raises(RuntimeError, match="already inside a transaction"):
            with kbc.write_txn(a):
                pass


@pytest.mark.parametrize("no_retry", [False, True])
def test_rollback_error_with_no_live_transaction_keeps_the_callers_connection(two, monkeypatch, no_retry):
    """SQLite ended the transaction itself (disk full, I/O error): ROLLBACK then fails with
    "no transaction is active". No writer is live, so the caller's connection must stay usable."""
    a, b = two
    order = []
    raw_execute, raw_close = a.execute, a.close

    def execute(sql, *args):
        if sql == "ROLLBACK":
            raw_execute("ROLLBACK")  # what SQLite's own automatic rollback leaves behind
            assert not a.in_transaction
            order.append("rollback_failed")
            raise sqlite3.OperationalError("cannot rollback - no transaction is active")
        return raw_execute(sql, *args)

    def close():
        order.append("close")
        raw_close()

    monkeypatch.setattr(a, "execute", execute)
    monkeypatch.setattr(a, "close", close)
    with pytest.raises(ValueError, match="original"):
        with kbc.write_txn(a, no_retry=no_retry, boundary_done=lambda: order.append("done")):
            a.execute("INSERT INTO tasks (id,title,status,created_at) VALUES ('tnlt','rb','todo',0)")
            raise ValueError("original")
    assert order == ["rollback_failed", "done"]
    assert raw_execute("SELECT count(*) FROM tasks WHERE id='tnlt'").fetchone()[0] == 0
    assert int(raw_execute("PRAGMA busy_timeout").fetchone()[0]) == PRIOR_TIMEOUT_MS
    b.execute("BEGIN IMMEDIATE")
    b.execute("ROLLBACK")
