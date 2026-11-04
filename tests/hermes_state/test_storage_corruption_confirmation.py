"""One unconfirmed "database disk image is malformed" must not latch state.db for the process.

On 2026-11-03 a single SQLITE_CORRUPT inside a compression split set the process-wide storage
latch on a file that later passed ``quick_check`` on every table; every write (worker receipts,
transcripts) was then refused for an hour until a restart. The writer and the pooled reader now
confirm structural corruption once, on the same connection, before latching:

* a rolled-back write that raised structural corruption is replayed once; a replay that succeeds
  lands exactly once and leaves the latch clear;
* damage that persists (real B-tree damage, a fault on every attempt) still latches, after at most
  one replay;
* FTS-scoped corruption keeps its own fail-open path and is never routed through the replay;
* a commit whose outcome is unknown is NEVER replayed (a durable-but-unacknowledged receipt
  would apply twice): it latches immediately, as before;
* the latch reason names the SQLite code, the operation and the stage.

All faults are injected into a throwaway database under ``tmp_path``.
"""

import logging
import sqlite3
from contextlib import contextmanager

import pytest

import hermes_state_runtime as rt
from hermes_state import SessionDB, StateDbCorruptError
from hermes_state_health import reset_storage_state, storage_corrupt_reason, storage_state

MALFORMED = "database disk image is malformed"


@pytest.fixture(autouse=True)
def _fresh_latch():
    reset_storage_state()
    yield
    reset_storage_state()


def _malformed(code=sqlite3.SQLITE_CORRUPT if hasattr(sqlite3, "SQLITE_CORRUPT") else 11, name="SQLITE_CORRUPT"):
    exc = sqlite3.DatabaseError(MALFORMED)
    exc.sqlite_errorcode = code
    exc.sqlite_errorname = name
    return exc


class _Faulty:
    """Proxy over the real writer connection that raises an injected error.

    ``match``: substring of the SQL whose ``execute`` faults; ``times``: how many faults
    (``None`` = every call); ``on_commit``: fault ``commit()`` instead, optionally after the
    real commit landed (``after_real_commit``), which is the ambiguous-settlement case."""

    def __init__(self, real, *, match=None, times=1, exc=_malformed, on_commit=False, after_real_commit=False):
        self._real = real
        self._match = match
        self._left = times
        self._exc = exc
        self._on_commit = on_commit
        self._after_real_commit = after_real_commit
        self.faults = 0
        self.matched_calls = 0

    def _fire(self):
        if self._left is None or self._left > 0:
            if self._left is not None:
                self._left -= 1
            self.faults += 1
            raise self._exc()

    def execute(self, sql, *args, **kwargs):
        if self._match is not None and self._match in sql:
            self.matched_calls += 1  # with on_commit, match only counts the callback's runs
            if not self._on_commit:
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
        db._conn = real


def _message_contents(path):
    raw = sqlite3.connect(str(path))
    try:
        return [r[0] for r in raw.execute("SELECT content FROM messages ORDER BY id")]
    finally:
        raw.close()


def _corrupt_sessions_btree(db_path):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        root = conn.execute("SELECT rootpage FROM sqlite_master WHERE name = 'sessions'").fetchone()[0]
    finally:
        conn.close()
    with open(db_path, "r+b") as fh:
        fh.seek(page_size * (root - 1))
        fh.write(b"\xde\xad\xbe\xef" * (page_size // 4))


@pytest.fixture
def db(tmp_path):
    handle = SessionDB(db_path=tmp_path / "state.db")
    handle.create_session("s1", source="cli")
    yield handle
    handle.close()


# ── (a) one-shot structural error: the write lands once, the latch stays clear ──


class TestOneShotCorruptionIsConfirmedNotLatched:
    def test_one_shot_corrupt_in_the_callback_lands_the_write_once(self, db, caplog):
        caplog.set_level(logging.WARNING)
        with _inject(db, match="INSERT INTO messages", times=1) as proxy:
            db.append_message(session_id="s1", role="user", content="survives one misread")
        assert proxy.faults == 1
        assert proxy.matched_calls == 2  # the faulted attempt plus exactly one replay
        assert _message_contents(db.db_path).count("survives one misread") == 1
        assert storage_state(db.db_path) == "ok"
        assert db._db_corrupt is False
        assert "did not recur" in caplog.text
        # The handle keeps working afterwards; nothing is quarantined.
        db.append_message(session_id="s1", role="user", content="next write")
        assert _message_contents(db.db_path)[-1] == "next write"

    def test_one_shot_corrupt_on_begin_immediate_is_replayed(self, db):
        with _inject(db, match="BEGIN IMMEDIATE", times=1) as proxy:
            db.append_message(session_id="s1", role="user", content="after begin fault")
        assert proxy.faults == 1
        assert _message_contents(db.db_path).count("after begin fault") == 1
        assert storage_state(db.db_path) == "ok"

    def test_one_shot_corrupt_on_a_pooled_read_does_not_latch(self, db, monkeypatch):
        real_ctx = db._read_ctx
        calls = {"n": 0}

        @contextmanager
        def flaky_ctx(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _malformed()
            with real_ctx(*a, **k) as conn:
                yield conn

        monkeypatch.setattr(db, "_read_ctx", flaky_ctx)
        row = db._read_one("SELECT id FROM sessions WHERE id = ?", ("s1",))
        assert row[0] == "s1"
        assert calls["n"] == 2
        assert storage_state(db.db_path) == "ok"


# ── (b) persistent damage still latches, after at most one replay ──


class TestPersistentCorruptionStillLatches:
    def test_fault_on_every_attempt_latches_after_one_replay(self, db):
        with _inject(db, match="INSERT INTO messages", times=None) as proxy:
            with pytest.raises(StateDbCorruptError):
                db.append_message(session_id="s1", role="user", content="never lands")
        assert proxy.matched_calls == 2  # bounded: one replay, never a loop
        assert storage_state(db.db_path) == "corrupt"
        assert db._db_corrupt is True
        assert "never lands" not in _message_contents(db.db_path)
        with pytest.raises(StateDbCorruptError):  # still fail-closed for every later write
            db.append_message(session_id="s1", role="user", content="after latch")

    def test_real_btree_damage_on_write_latches(self, tmp_path):
        path = tmp_path / "state.db"
        seed = SessionDB(db_path=path)
        for i in range(40):
            seed.create_session(f"s{i:03d}", source="cli")
        seed.close()
        _corrupt_sessions_btree(path)
        db = SessionDB(db_path=path)
        try:
            with pytest.raises(StateDbCorruptError):
                db.create_session("new-session", source="cli")
        finally:
            db.close()
        assert storage_state(path) == "corrupt"

    def test_persistent_read_fault_latches(self, db, monkeypatch):
        calls = {"n": 0}

        @contextmanager
        def broken_ctx(*a, **k):
            calls["n"] += 1
            raise _malformed()
            yield  # pragma: no cover

        monkeypatch.setattr(db, "_read_ctx", broken_ctx)
        with pytest.raises(sqlite3.DatabaseError):
            db._read_one("SELECT id FROM sessions")
        assert calls["n"] == 2
        assert storage_state(db.db_path) == "corrupt"

    def test_unconfirmed_rollback_is_not_replayed(self, db):
        """A rollback that did not end the transaction leaves the connection state unknown:
        no replay on it, quarantine at once."""
        real = db._conn

        class _NoRollback(_Faulty):
            def rollback(self):
                raise sqlite3.OperationalError("rollback refused (injected)")

        proxy = _NoRollback(real, match="INSERT INTO messages", times=1)
        db._conn = proxy
        try:
            with pytest.raises(StateDbCorruptError):
                db.append_message(session_id="s1", role="user", content="no replay")
        finally:
            if real.in_transaction:
                real.rollback()
            db._conn = real
        assert proxy.matched_calls == 1
        assert storage_state(db.db_path) == "corrupt"


# ── (c) FTS-scoped corruption keeps its own path ──


class TestFtsScopedCorruptionUnchanged:
    def _fts_error(self):
        exc = sqlite3.DatabaseError(MALFORMED)
        exc.sqlite_errorcode = 267  # SQLITE_CORRUPT_VTAB
        exc.sqlite_errorname = "SQLITE_CORRUPT_VTAB"
        return exc

    def test_fts_scoped_fault_takes_fail_open_not_the_structural_replay(self, db, monkeypatch, caplog):
        if not db._fts_enabled:
            pytest.skip("FTS5 unavailable in this build")
        caplog.set_level(logging.WARNING)
        entered = []
        real_fail_open = db._enter_fts_fail_open

        def spy(exc, **kw):
            entered.append(getattr(exc, "sqlite_errorcode", None))
            return real_fail_open(exc, **kw)

        monkeypatch.setattr(db, "_enter_fts_fail_open", spy)
        with _inject(db, match="INSERT INTO messages", times=1, exc=self._fts_error):
            db.append_message(session_id="s1", role="user", content="fts fault")
        assert entered == [267]
        assert "replaying it once" not in caplog.text
        assert storage_state(db.db_path) == "ok"
        assert db._db_corrupt is False
        assert _message_contents(db.db_path).count("fts fault") == 1

    def test_refused_fts_detach_escapes_without_replay_or_latch(self, db, monkeypatch):
        monkeypatch.setattr(db, "_enter_fts_fail_open", lambda exc, **_: False)
        with _inject(db, match="INSERT INTO messages", times=1, exc=self._fts_error) as proxy:
            with pytest.raises(sqlite3.DatabaseError) as excinfo:
                db.append_message(session_id="s1", role="user", content="refused")
        assert not isinstance(excinfo.value, StateDbCorruptError)
        assert proxy.matched_calls == 1
        assert storage_state(db.db_path) == "ok"
        assert db._db_corrupt is False


# ── (d) acknowledged receipts apply exactly once ──


@pytest.fixture
def ledger(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("parent", source="kanban", profile_name="coder")
    epoch = rt.begin_runtime_epoch(db, instance_id="boot")
    rt.register_worker_execution(db, epoch=epoch, execution_id="worker", session_id="parent", generation=0,
                                 kind="compute", adoption_secret="private-fixture")
    yield db, epoch
    db.close()


def _append(db, epoch, sequence, content):
    return rt.mutate_worker_execution(
        db, epoch=epoch, execution_id="worker", session_id="parent", generation=0, sequence=sequence,
        operation="transcript.append", payload={"messages": [{"role": "assistant", "content": content}]})


def _receipt_state(db):
    with db._read_ctx() as conn:
        receipts = conn.execute("SELECT sequence FROM worker_receipts WHERE execution_id='worker' "
                                "ORDER BY sequence").fetchall()
        last = conn.execute("SELECT last_sequence FROM worker_executions WHERE execution_id='worker'").fetchone()[0]
    return [r[0] for r in receipts], last


class TestWorkerReceiptExactlyOnce:
    def test_receipt_replayed_after_one_shot_corruption_lands_once(self, ledger):
        db, epoch = ledger
        with _inject(db, match="INSERT INTO worker_receipts", times=1) as proxy:
            first = _append(db, epoch, 1, "receipt body")
        assert proxy.faults == 1
        assert _message_contents(db.db_path).count("receipt body") == 1
        assert _receipt_state(db) == ([1], 1)
        assert storage_state(db.db_path) == "ok"
        # The worker's own retry of the acknowledged sequence returns the stored result, no new row.
        assert _append(db, epoch, 1, "receipt body") == first
        assert _message_contents(db.db_path).count("receipt body") == 1
        assert _receipt_state(db) == ([1], 1)

    def test_commit_fault_is_never_replayed(self, ledger):
        """Corruption raised by COMMIT leaves settlement unknown: fail closed, no replay."""
        db, epoch = ledger
        with _inject(db, on_commit=True, times=1, match="INSERT INTO worker_receipts") as proxy:
            with pytest.raises(StateDbCorruptError):
                _append(db, epoch, 1, "commit fault")
        assert proxy.matched_calls == 1
        assert storage_state(db.db_path) == "corrupt"

    def test_durable_commit_then_error_applies_exactly_once(self, ledger):
        """The real commit landed (the receipt is durable) and an error is reported anyway:
        a replay would append the transcript twice. It must latch without replaying."""
        db, epoch = ledger
        with _inject(db, on_commit=True, after_real_commit=True, times=1,
                     match="INSERT INTO worker_receipts") as proxy:
            with pytest.raises(StateDbCorruptError):
                _append(db, epoch, 1, "durable once")
        assert proxy.matched_calls == 1
        assert _message_contents(db.db_path).count("durable once") == 1
        assert _receipt_state(db) == ([1], 1)
        assert storage_state(db.db_path) == "corrupt"


# ── compression split entry path (the 2026-11-03 trigger) ──


class TestCompressionSplitEntryPath:
    def _publish(self, db):
        db.publish_compression_child(
            parent_session_id="parent-1", child_session_id="child-1", source="test",
            messages=[{"role": "user", "content": "handoff summary"}], require_compression_lease=False)

    def _seed(self, tmp_path):
        db = SessionDB(db_path=tmp_path / "state.db")
        db.create_session("parent-1", source="cli")
        db.append_message(session_id="parent-1", role="user", content="before split")
        return db

    def test_one_shot_corrupt_during_split_publishes_once(self, tmp_path):
        db = self._seed(tmp_path)
        try:
            with _inject(db, match="INSERT INTO messages", times=1) as proxy:
                self._publish(db)
            assert proxy.faults == 1
            assert _message_contents(db.db_path).count("handoff summary") == 1
            assert db.get_session("child-1") is not None
            assert db.get_session("parent-1")["end_reason"] is not None
            assert storage_state(db.db_path) == "ok"
        finally:
            db.close()

    def test_persistent_corrupt_during_split_latches_and_publishes_nothing(self, tmp_path):
        db = self._seed(tmp_path)
        try:
            with _inject(db, match="INSERT INTO messages", times=None) as proxy:
                with pytest.raises(StateDbCorruptError):
                    self._publish(db)
            assert proxy.matched_calls == 2
            assert "handoff summary" not in _message_contents(db.db_path)
            assert storage_state(db.db_path) == "corrupt"
        finally:
            db.close()
        raw = sqlite3.connect(str(tmp_path / "state.db"))
        try:  # the split is atomic: no child row, parent still open
            assert raw.execute("SELECT count(*) FROM sessions WHERE id='child-1'").fetchone()[0] == 0
            assert raw.execute("SELECT end_reason FROM sessions WHERE id='parent-1'").fetchone()[0] is None
        finally:
            raw.close()


# ── (e) the latch says what tripped it ──


def test_latch_reason_names_code_operation_and_stage(db, caplog):
    caplog.set_level(logging.ERROR)
    with _inject(db, match="INSERT INTO messages", times=None):
        with pytest.raises(StateDbCorruptError) as excinfo:
            db.append_message(session_id="s1", role="user", content="x")
    reason = storage_corrupt_reason(db.db_path)
    for needle in ("SQLITE_CORRUPT", "op=", "stage=callback", "recurred on replay"):
        assert needle in reason, (needle, reason)
        assert needle in str(excinfo.value)
        assert needle in caplog.text
