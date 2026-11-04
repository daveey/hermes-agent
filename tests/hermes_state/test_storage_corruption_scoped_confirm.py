"""A structural SQLITE_CORRUPT is confirmed by a scoped, bounded, read-only quick_check before replay.

Regression for selfheal gateway-d7a592a91c (2026-11-03, hermes-hub): one unconfirmed "database
disk image is malformed" in a compression split latched a file that later passed quick_check on
every table. Before the writer (or the pooled reader) replays such a statement, it now runs
``PRAGMA quick_check`` on the tables the failing attempt touched, read-only, in a fresh process,
bounded by the remaining write patience:

* clean check -> one replay, the write lands once, nothing latches, the error is counted;
* damage (a real damaged B-tree page), a check timeout, a check error, or no budget left ->
  fail closed: latch with no replay;
* FTS-scoped corruption, COMMIT faults and durable-commit-then-error never reach the check;
* a recurrence after a clean check latches without a second check.

Covered per entry path: worker receipt write (``mutate_worker_execution`` transcript.append),
transcript append (``append_message``), compression split (``publish_compression_child``).
Faults are injected into throwaway databases under ``tmp_path`` only.
"""

import logging
import sqlite3
import sys
import time
from contextlib import contextmanager

import pytest

import hermes_state_corruption_confirm as cc
import hermes_state_runtime as rt
from hermes_state import SessionDB, StateDbCorruptError
from hermes_state_health import (
    reset_storage_state, storage_corrupt_reason, storage_state, unconfirmed_corruption_count,
)

MALFORMED = "database disk image is malformed"
PII = "PII-SENTINEL-alice@example.com"


@pytest.fixture(autouse=True)
def _fresh_latch():
    reset_storage_state()
    yield
    reset_storage_state()


def _malformed():
    exc = sqlite3.DatabaseError(MALFORMED)
    exc.sqlite_errorcode = 11
    exc.sqlite_errorname = "SQLITE_CORRUPT"
    return exc


def _fts_error():
    exc = sqlite3.DatabaseError(MALFORMED)
    exc.sqlite_errorcode = 267
    exc.sqlite_errorname = "SQLITE_CORRUPT_VTAB"
    return exc


class _Faulty:
    """Proxy over the real writer connection raising an injected error on matching SQL."""

    def __init__(self, real, *, match, times=1, exc=_malformed, on_commit=False, after_real_commit=False):
        self._real, self._match, self._left, self._exc = real, match, times, exc
        self._on_commit, self._after_real_commit = on_commit, after_real_commit
        self.faults = 0
        self.matched_calls = 0

    def _fire(self):
        if self._left is None or self._left > 0:
            if self._left is not None:
                self._left -= 1
            self.faults += 1
            raise self._exc()

    def execute(self, sql, *args, **kwargs):
        if self._match in sql:
            self.matched_calls += 1
            if not self._on_commit:
                if not sql.lstrip().upper().startswith("BEGIN"):
                    # Real corruption surfaces when the statement STEPS, after it was prepared (the
                    # authorizer has seen its tables by then): prepare it via EXPLAIN, then fault.
                    self._real.execute("EXPLAIN " + sql, *args, **kwargs)
                self._fire()
        return self._real.execute(sql, *args, **kwargs)

    def commit(self):
        if self._on_commit:
            if self._after_real_commit:
                self._real.commit()
            self._fire()
        return self._real.commit()

    def __getattr__(self, name):
        return getattr(self._real, name)


@contextmanager
def _inject(db, **kwargs):
    real = db._conn
    proxy = _Faulty(real, **kwargs)
    db._conn = proxy
    try:
        yield proxy
    finally:
        if real.in_transaction:
            real.rollback()
        db._conn = real


class _Calls(list):
    """Recorded confirmations; ``spawned`` holds the fresh-process commands built for them."""

    def __init__(self):
        super().__init__()
        self.spawned = []


@pytest.fixture
def confirms(monkeypatch):
    """Spy on the REAL confirmation (and on the fresh-process command it builds)."""
    calls = _Calls()
    real_confirm, real_argv = cc.confirm_corruption, cc._child_argv

    def spy(db_path, tables, timeout_s):
        result = real_confirm(db_path, tables, timeout_s)
        calls.append({"tables": tables, "timeout_s": timeout_s, "result": result})
        return result

    def argv_spy(*a):
        argv = real_argv(*a)
        calls.spawned.append(argv)
        return argv

    monkeypatch.setattr(cc, "confirm_corruption", spy)
    monkeypatch.setattr(cc, "_child_argv", argv_spy)
    return calls


def _hung_child(monkeypatch, cap_s=1.0):
    monkeypatch.setattr(cc, "CONFIRM_MAX_S", cap_s)
    monkeypatch.setattr(cc, "_child_argv", lambda *a: [
        sys.executable, "-I", "-S", "-c", "import threading; threading.Event().wait(120)"])


def _failing_child(monkeypatch):
    monkeypatch.setattr(cc, "_child_argv", lambda *a: [sys.executable, "-I", "-S", "-c", "raise SystemExit(3)"])


def _contents(path):
    raw = sqlite3.connect(str(path))
    try:
        return [r[0] for r in raw.execute("SELECT content FROM messages ORDER BY id")]
    finally:
        raw.close()


def _reason(path):
    return storage_corrupt_reason(path) or ""


# ── entry paths ──


class _TranscriptAppend:
    match = "INSERT INTO messages"

    def __init__(self, tmp_path):
        self.db = SessionDB(db_path=tmp_path / "state.db")
        self.db.create_session("s1", source="cli")

    def write(self, content):
        self.db.append_message(session_id="s1", role="user", content=content)

    def landed(self, content):
        return _contents(self.db.db_path).count(content)


class _WorkerReceipt:
    match = "INSERT INTO worker_receipts"

    def __init__(self, tmp_path):
        self.db = SessionDB(db_path=tmp_path / "state.db")
        self.db.create_session("parent", source="kanban", profile_name="coder")
        self.epoch = rt.begin_runtime_epoch(self.db, instance_id="boot")
        rt.register_worker_execution(self.db, epoch=self.epoch, execution_id="worker", session_id="parent",
                                     generation=0, kind="compute", adoption_secret="private-fixture")
        self.seq = 0

    def write(self, content):
        self.seq += 1
        return rt.mutate_worker_execution(
            self.db, epoch=self.epoch, execution_id="worker", session_id="parent", generation=0,
            sequence=self.seq, operation="transcript.append",
            payload={"messages": [{"role": "assistant", "content": content}]})

    def landed(self, content):
        with self.db._read_ctx() as conn:
            receipts = conn.execute("SELECT count(*) FROM worker_receipts WHERE execution_id='worker'").fetchone()[0]
        n = _contents(self.db.db_path).count(content)
        assert receipts == (1 if n else 0)  # one receipt per landed transcript append
        return n


class _CompressionSplit:
    match = "INSERT INTO messages"

    def __init__(self, tmp_path):
        self.db = SessionDB(db_path=tmp_path / "state.db")
        self.db.create_session("parent-1", source="cli")
        self.db.append_message(session_id="parent-1", role="user", content="before split")

    def write(self, content):
        self.db.publish_compression_child(
            parent_session_id="parent-1", child_session_id="child-1", source="test",
            messages=[{"role": "user", "content": content}], require_compression_lease=False)

    def landed(self, content):
        n = _contents(self.db.db_path).count(content)
        raw = sqlite3.connect(str(self.db.db_path))
        try:
            child = raw.execute("SELECT count(*) FROM sessions WHERE id='child-1'").fetchone()[0]
            ended = raw.execute("SELECT end_reason FROM sessions WHERE id='parent-1'").fetchone()[0]
        finally:
            raw.close()
        assert (child, ended is not None) == ((1, True) if n else (0, False))  # atomic split
        return n


ENTRY_PATHS = [_TranscriptAppend, _WorkerReceipt, _CompressionSplit]
IDS = ["transcript_append", "worker_receipt", "compression_split"]


@pytest.fixture(params=ENTRY_PATHS, ids=IDS)
def entry(request, tmp_path):
    path = request.param(tmp_path)
    yield path
    path.db.close()


class TestCleanScopedConfirmAllowsOneReplay:
    def test_one_shot_corrupt_with_clean_confirm_lands_once(self, entry, confirms, caplog):
        caplog.set_level(logging.WARNING)
        with _inject(entry.db, match=entry.match, times=1) as proxy:
            entry.write(f"lands once {PII}")
        assert proxy.faults == 1 and proxy.matched_calls == 2
        assert entry.landed(f"lands once {PII}") == 1
        assert storage_state(entry.db.db_path) == "ok" and entry.db._db_corrupt is False
        # A real check ran, in a fresh process, scoped (not the whole file) to what the write touched.
        assert len(confirms) == 1 and len(confirms.spawned) == 1
        call = confirms[0]
        assert call["result"].outcome == "clean"
        assert call["tables"] is not None and "messages" in call["tables"]
        assert call["result"].scope != "full" and "messages" in call["result"].scope
        assert 0 < call["timeout_s"] <= cc.CONFIRM_MAX_S
        assert unconfirmed_corruption_count(entry.db.db_path) == 1
        assert "NOT confirmed (confirm=clean" in caplog.text
        assert PII not in caplog.text  # names only: no SQL params in diagnostics
        assert all(PII not in " ".join(argv) for argv in confirms.spawned)

    def test_recurrence_after_clean_confirm_latches_without_second_check(self, entry, confirms):
        with _inject(entry.db, match=entry.match, times=None) as proxy:
            with pytest.raises(StateDbCorruptError):
                entry.write("never lands")
        assert proxy.matched_calls == 2
        assert [c["result"].outcome for c in confirms] == ["clean"]
        assert entry.landed("never lands") == 0
        assert storage_state(entry.db.db_path) == "corrupt"
        assert "recurred on replay" in _reason(entry.db.db_path)


class TestConfirmFailsClosed:
    def test_confirm_timeout_latches_without_replay(self, entry, confirms, monkeypatch):
        _hung_child(monkeypatch, cap_s=1.0)
        started = time.monotonic()
        with _inject(entry.db, match=entry.match, times=1) as proxy:
            with pytest.raises(StateDbCorruptError) as excinfo:
                entry.write("timeout write")
        assert time.monotonic() - started < 20  # bounded: the hung child is killed at the cap
        assert proxy.matched_calls == 1  # no replay
        assert [c["result"].outcome for c in confirms] == ["timeout"]
        assert confirms[0]["timeout_s"] <= 1.0
        assert entry.landed("timeout write") == 0
        assert storage_state(entry.db.db_path) == "corrupt"
        assert "confirm=timeout" in _reason(entry.db.db_path)
        assert "confirm=timeout" in str(excinfo.value)
        assert unconfirmed_corruption_count(entry.db.db_path) == 0
        with pytest.raises(StateDbCorruptError):  # later writes refused exactly as before
            entry.write("after latch")

    def test_confirm_error_latches_without_replay(self, entry, confirms, monkeypatch):
        _failing_child(monkeypatch)
        with _inject(entry.db, match=entry.match, times=1) as proxy:
            with pytest.raises(StateDbCorruptError):
                entry.write("error write")
        assert proxy.matched_calls == 1
        assert [c["result"].outcome for c in confirms] == ["error"]
        assert entry.landed("error write") == 0
        assert storage_state(entry.db.db_path) == "corrupt"
        assert "confirm=error" in _reason(entry.db.db_path)


def test_no_patience_left_fails_closed_without_spawning(tmp_path, confirms):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("s1", source="cli")
        with _inject(db, match="INSERT INTO messages", times=1) as proxy:
            with pytest.raises(StateDbCorruptError):
                db._execute_write(lambda conn: conn.execute(
                    "INSERT INTO messages (session_id, role, content, timestamp) VALUES ('s1','user','x',0)"),
                    patience_s=0.0)
        assert proxy.matched_calls == 1
        assert [c["result"].outcome for c in confirms] == ["no_budget"]
        assert confirms.spawned == []
        assert storage_state(db.db_path) == "corrupt"
    finally:
        db.close()


def test_spawn_failure_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "_child_argv", lambda *a: [str(tmp_path / "no-such-python")])
    result = cc.confirm_corruption(tmp_path / "state.db", None, 5.0)
    assert result.outcome == "error" and not result.clean


def test_unparsable_child_output_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "_child_argv", lambda *a: [sys.executable, "-I", "-S", "-c", "print('ok')"])
    assert cc.confirm_corruption(tmp_path / "state.db", None, 5.0).outcome == "error"


# ── real damaged B-tree page ──


def _seed_and_damage_sessions(path):
    seed = SessionDB(db_path=path)
    for i in range(40):
        seed.create_session(f"s{i:03d}", source="cli")
    seed.close()
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        root = conn.execute("SELECT rootpage FROM sqlite_master WHERE name = 'sessions'").fetchone()[0]
    finally:
        conn.close()
    with open(path, "r+b") as fh:
        fh.seek(page_size * (root - 1))
        fh.write(b"\xde\xad\xbe\xef" * (page_size // 4))


def test_real_damaged_btree_is_confirmed_and_latches_without_replay(tmp_path, confirms):
    path = tmp_path / "state.db"
    _seed_and_damage_sessions(path)
    db = SessionDB(db_path=path)
    try:
        with pytest.raises(StateDbCorruptError) as excinfo:
            db.create_session("new-session", source="cli")
        with pytest.raises(StateDbCorruptError):  # writes refused exactly as today
            db.append_message(session_id="s001", role="user", content="refused")
    finally:
        db.close()
    assert [c["result"].outcome for c in confirms] == ["damaged"]
    assert "sessions" in confirms[0]["tables"]
    assert storage_state(path) == "corrupt"
    assert "confirm=damaged" in _reason(path)
    assert "confirm=damaged" in str(excinfo.value)
    assert unconfirmed_corruption_count(path) == 0


def test_check_is_scoped_to_the_named_tables(tmp_path):
    """The damaged ``sessions`` B-tree is found by a check that names it (or the whole file) and
    not by one scoped to another table alone: the scope is what was touched, not a full scan."""
    path = tmp_path / "state.db"
    _seed_and_damage_sessions(path)
    assert cc.confirm_corruption(path, frozenset({"sessions"}), 10.0).outcome == "damaged"
    assert cc.confirm_corruption(path, None, 10.0).outcome == "damaged"
    narrow = cc.confirm_corruption(path, frozenset({"state_meta"}), 10.0)
    assert narrow.outcome == "clean", narrow
    assert narrow.scope == "sqlite_schema,state_meta"


# ── paths that must never reach the check ──


class TestPathsThatNeverConfirm:
    def test_fts_scoped_fault_bypasses_the_check(self, tmp_path, confirms):
        db = SessionDB(db_path=tmp_path / "state.db")
        try:
            if not db._fts_enabled:
                pytest.skip("FTS5 unavailable in this build")
            db.create_session("s1", source="cli")
            with _inject(db, match="INSERT INTO messages", times=1, exc=_fts_error):
                db.append_message(session_id="s1", role="user", content="fts fault")
            assert confirms == []
            assert storage_state(db.db_path) == "ok"
            assert _contents(db.db_path).count("fts fault") == 1
        finally:
            db.close()

    def test_commit_fault_is_never_checked_or_replayed(self, tmp_path, confirms):
        path = _WorkerReceipt(tmp_path)
        try:
            with _inject(path.db, match=path.match, times=1, on_commit=True) as proxy:
                with pytest.raises(StateDbCorruptError):
                    path.write("commit fault")
            assert proxy.matched_calls == 1 and confirms == []
            assert storage_state(path.db.db_path) == "corrupt"
        finally:
            path.db.close()

    def test_durable_commit_then_error_applies_once_and_is_never_checked(self, tmp_path, confirms):
        path = _WorkerReceipt(tmp_path)
        try:
            with _inject(path.db, match=path.match, times=1, on_commit=True, after_real_commit=True) as proxy:
                with pytest.raises(StateDbCorruptError):
                    path.write("durable once")
            assert proxy.matched_calls == 1 and confirms == []
            assert path.landed("durable once") == 1
        finally:
            path.db.close()


def test_begin_stage_fault_has_unknown_scope_and_checks_the_whole_file(tmp_path, confirms):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("s1", source="cli")
        with _inject(db, match="BEGIN IMMEDIATE", times=1):
            db.append_message(session_id="s1", role="user", content="after begin fault")
        assert [(c["tables"], c["result"].scope, c["result"].outcome) for c in confirms] == [(None, "full", "clean")]
        assert _contents(db.db_path).count("after begin fault") == 1
    finally:
        db.close()


# ── pooled reader ──


class TestPooledRead:
    def _flaky(self, db, monkeypatch, fail_times):
        real_ctx = db._read_ctx
        calls = {"n": 0}

        @contextmanager
        def ctx(*a, **k):
            with real_ctx(*a, **k) as conn:
                calls["n"] += 1
                if fail_times is None or calls["n"] <= fail_times:
                    raise _malformed()  # before the statement: unknown scope, whole-file check
                yield conn

        monkeypatch.setattr(db, "_read_ctx", ctx)
        return calls

    def test_one_shot_read_with_clean_confirm_replays(self, tmp_path, confirms, monkeypatch):
        db = SessionDB(db_path=tmp_path / "state.db")
        try:
            db.create_session("s1", source="cli")
            calls = self._flaky(db, monkeypatch, 1)
            row = db._read_one("SELECT id FROM sessions WHERE id = ?", ("s1",))
            assert row is not None and row[0] == "s1"
            assert calls["n"] == 2
            assert [(c["tables"], c["result"].outcome) for c in confirms] == [(None, "clean")]
            assert storage_state(db.db_path) == "ok"
            assert unconfirmed_corruption_count(db.db_path) == 1
        finally:
            db.close()

    def test_read_scope_is_the_tables_the_statement_read(self, tmp_path, confirms):
        db = SessionDB(db_path=tmp_path / "state.db")
        try:
            db.create_session("s1", source="cli")
            attempts = {"n": 0}

            def read(conn):
                attempts["n"] += 1
                row = conn.execute("SELECT count(*) FROM messages WHERE session_id = ?", (PII,)).fetchone()
                if attempts["n"] == 1:
                    raise _malformed()
                return row[0]

            assert db._read_retrying_ioerr(read) == 0
            assert attempts["n"] == 2
            [call] = confirms
            assert call["tables"] is not None and "messages" in call["tables"]
            assert call["result"].outcome == "clean"
            assert PII not in repr(call)
        finally:
            db.close()

    def test_read_confirm_timeout_latches_without_replay(self, tmp_path, confirms, monkeypatch):
        db = SessionDB(db_path=tmp_path / "state.db")
        try:
            db.create_session("s1", source="cli")
            calls = self._flaky(db, monkeypatch, 1)
            _hung_child(monkeypatch, cap_s=1.0)
            with pytest.raises(sqlite3.DatabaseError):
                db._read_one("SELECT id FROM sessions WHERE id = ?", ("s1",))
            assert calls["n"] == 1
            assert [c["result"].outcome for c in confirms] == ["timeout"]
            assert storage_state(db.db_path) == "corrupt"
            assert "confirm=timeout" in _reason(db.db_path)
        finally:
            db.close()


# ── the scope recorder ──


class TestTouchedTables:
    def test_records_tables_from_triggers_and_fts_shadow_statements_but_no_values(self, tmp_path):
        conn = sqlite3.connect(tmp_path / "x.db")
        try:
            conn.execute("CREATE TABLE t(a INTEGER PRIMARY KEY, b)")
            conn.execute("CREATE TABLE audit(n)")
            conn.execute("CREATE TRIGGER t_ai AFTER INSERT ON t BEGIN INSERT INTO audit VALUES (1); END")
            conn.commit()
            rec = cc.TouchedTables()
            with rec.recording(conn):
                conn.execute("INSERT INTO t(b) VALUES (?)", (PII,))
            assert rec.tables() == frozenset({"t", "audit"})
            assert PII not in repr(rec._names)
            # Detached afterwards: later statements are not recorded.
            conn.execute("SELECT * FROM audit").fetchall()
            assert rec.tables() == frozenset({"t", "audit"})
        finally:
            conn.close()

    def test_unknown_scope_widens_to_full(self, tmp_path):
        assert cc.TouchedTables().tables() is None  # never attached
        rec = cc.TouchedTables()
        conn = sqlite3.connect(tmp_path / "x.db")
        try:
            with rec.recording(conn):
                conn.execute("BEGIN")
                conn.execute("ROLLBACK")
            assert rec.tables() is None  # nothing table-scoped seen
        finally:
            conn.close()
        attached = cc.TouchedTables()
        attached(18, "t", None, "other_db", None)  # an ATTACHed database
        assert attached.tables() is None
        odd = cc.TouchedTables()
        odd(27, "idx", None, "main", None)  # SQLITE_REINDEX: not mapped
        odd(18, "t", None, "main", None)
        assert odd.tables() is None
