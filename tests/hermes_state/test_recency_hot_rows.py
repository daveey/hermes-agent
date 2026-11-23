"""The recency path steps Python through few SQLite rows (hermes_state_recency, hermes_state_rank_memo).

Python's sqlite3 drops and re-takes the GIL around every row it steps; on the hub's ~190-thread Desktop
backend each re-take could wait the 5 ms switch interval, all while the store lock queued every other
session list and project tree behind it. A 60 s profile put ~57 thread-s/min here: the full message-max
rebuild (one row per session, 27k), the candidate list (~7.5k rows for the 2000-row tree page), the
re-probe of every legitimately high session (~5.5k) and the re-rank of the whole previous page. These
tests pin the pages to the exact ranking and the rows each read steps through.
"""

import random
import sqlite3
from contextlib import contextmanager

import pytest

import hermes_state_rank_memo as memo
import hermes_state_recency as recency
from hermes_state import SessionDB

_BASE = 1_700_000_000.0


@pytest.fixture
def db(tmp_path):
    recency.clear()
    memo.OUTCOMES.clear()
    database = SessionDB(db_path=tmp_path / "state.db")
    try:
        yield database
    finally:
        database.close()
        recency.clear()


def _write(db, sql, params=()):
    db._execute_write(lambda conn: conn.execute(sql, params))


def _seed(db, count, *, source="cli", prefix="s", messages=1):
    def seed(conn):
        conn.executemany(
            "INSERT INTO sessions (id, source, started_at, last_activity_at, message_count) VALUES (?, ?, ?, ?, 1)",
            [(f"{prefix}{i:05d}", source, _BASE + i, _BASE + i + 0.5) for i in range(count)])
        conn.executemany("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, 'user', 'x', ?)",
                         [(f"{prefix}{i:05d}", _BASE + i + 0.25 + k) for i in range(count) for k in range(messages)])
    db._execute_write(seed)


class _Cursor:
    def __init__(self, cursor, sql, log):
        self._cursor, self._sql, self._log = cursor, sql, log

    def fetchall(self):
        rows = self._cursor.fetchall()
        self._log.append((self._sql, len(rows)))
        return rows

    def fetchone(self):
        row = self._cursor.fetchone()
        self._log.append((self._sql, int(row is not None)))
        return row

    def __iter__(self):
        for row in self._cursor:
            self._log.append((self._sql, 1))
            yield row


class _Conn:
    """A read connection that logs ``(sql, rows Python stepped through)`` per statement."""

    def __init__(self, conn, log):
        self._conn, self._log = conn, log

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def execute(self, sql, *args):
        return _Cursor(self._conn.execute(sql, *args), sql, self._log)


@contextmanager
def _stepped(db):
    log: list = []
    borrow = db._read_ctx

    @contextmanager
    def counted(*args, **kwargs):
        with borrow(*args, **kwargs) as conn:
            yield conn if conn is None else _Conn(conn, log)

    db._read_ctx = counted  # type: ignore[method-assign]
    try:
        yield log
    finally:
        del db._read_ctx


def _rows(log, needle=""):
    return sum(n for sql, n in log if needle in sql)


def _page(db, **kwargs):
    return db.list_sessions_rich(order_by_last_active=True, compact_rows=True, min_message_count=1, **kwargs)


def _exact(db, monkeypatch, **kwargs):
    monkeypatch.setattr(recency, "ENABLED", False)
    try:
        return _page(db, **kwargs)
    finally:
        monkeypatch.setattr(recency, "ENABLED", True)


def test_rebuild_carries_every_maximum_exactly_in_one_row(tmp_path):
    rng = random.Random(7)
    conn = sqlite3.connect(tmp_path / "m.db")
    conn.executescript("CREATE TABLE sessions (id TEXT PRIMARY KEY);"
                       "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, timestamp);"
                       "CREATE INDEX idx_messages_session ON messages (session_id, timestamp);")
    stamps = [_BASE + rng.random() * 1e8 for _ in range(3000)]
    stamps += [rng.uniform(1, 4.2e9) for _ in range(2000)] + [rng.randrange(1, 4_200_000_000) for _ in range(500)]
    stamps += [1.0, 1.5, 4_199_999_999.999999, 4_200_000_000.0, 1_700_000_000, 1_700_000_000.0, 0.0, 0.25]
    garbage = [5.49e246, "not-a-timestamp", -3.0, None]
    rows = [(f"s{i}", ts) for i, ts in enumerate(stamps)] + [(f"g{i}", ts) for i, ts in enumerate(garbage)]
    conn.executemany("INSERT INTO sessions (id) VALUES (?)", [(sid,) for sid, _ in rows] + [("empty",)])
    conn.executemany("INSERT INTO messages (session_id, timestamp) VALUES (?, ?)", rows)
    conn.execute("INSERT INTO messages (session_id, timestamp) VALUES ('s0', ?)", (stamps[0] - 5,))
    native = {sid: v for sid, v in conn.execute(f"SELECT s.id, {recency._msg_max_sql('s.id')} FROM sessions s")
              if v is not None}
    log: list = []
    rebuilt = recency._rebuilt_maxima(_Conn(conn, log))
    assert log == [(recency._REBUILD_SQL, 1)]
    assert set(rebuilt) == set(native) and "empty" not in rebuilt and "g1" not in rebuilt
    for sid, value in native.items():
        if isinstance(value, float) and value < 1:
            assert rebuilt[sid] == 1.0  # an upper bound, all the cache claims
        else:
            assert rebuilt[sid] == value and type(rebuilt[sid]) is type(value), (sid, value, rebuilt[sid])


def test_cold_and_lowered_reads_step_through_the_page_not_the_store(db, monkeypatch):
    _seed(db, 3000)
    with _stepped(db) as log:
        cold = _page(db, limit=50)
    assert cold == _exact(db, monkeypatch, limit=50)
    # No statement steps more than the page (the ranked ids, then the projection); the rebuild alone
    # stepped 3000 rows and the candidate list ~100 before. (A cold read also opens and probes the store.)
    assert max(n for _, n in log) == 50
    _write(db, "DELETE FROM messages WHERE session_id = 's02999'")  # bumps the lowered counter
    with _stepped(db) as log:
        lowered = _page(db, limit=50)
    assert lowered == _exact(db, monkeypatch, limit=50)
    assert max(n for _, n in log) == 50 and _rows(log) <= 50 + 50 + 20, _rows(log)
    assert memo.OUTCOMES["fallback/lowered"] == 1


def test_reprobe_of_legitimately_high_sessions_steps_through_none(db, monkeypatch):
    """Sessions outside the page's filter can hold most fresh messages: the re-probe finds them still high
    and must not bring them back to Python one row each."""
    _seed(db, 100)
    _seed(db, 600, source="cron", prefix="c")
    _write(db, "UPDATE messages SET timestamp = timestamp + 100000 WHERE session_id LIKE 'c%'")
    _page(db, limit=10, exclude_sources=["cron"])
    for offset in (5, 6):  # offset > 0: the candidate path on every read
        with _stepped(db) as log:
            got = _page(db, limit=10, offset=offset, exclude_sources=["cron"])
        assert got == _exact(db, monkeypatch, limit=10, offset=offset, exclude_sources=["cron"])
        reprobes = [(sql, n) for sql, n in log if "m IS NULL OR m < ?" in sql]
        assert reprobes and _rows(log, "m IS NULL OR m < ?") == 0
        assert _rows(log) <= 10 + 20, _rows(log)


def test_rerank_steps_only_through_the_rows_a_write_touched(db, monkeypatch):
    _seed(db, 1500)
    _page(db, limit=1000)
    _write(db, "INSERT INTO messages (session_id, role, content, timestamp) VALUES ('s00010', 'user', 'y', ?)",
           (_BASE + 50_000,))
    _write(db, "UPDATE sessions SET last_activity_at = ? WHERE id = 's01400'", (_BASE + 60_000,))
    with _stepped(db) as log:
        rows = _page(db, limit=1000)
    assert memo.OUTCOMES["reranked"] == 1
    assert rows == _exact(db, monkeypatch, limit=1000)
    assert [r["id"] for r in rows[:2]] == ["s01400", "s00010"]
    assert _rows(log, "chain_max AS") <= 4  # the two touched rows; was the whole 1000-row page
