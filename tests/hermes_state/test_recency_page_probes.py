"""Recency pages probe messages only for rows that can reach the page (hermes_state_recency).

The sidebar polls ``list_sessions_rich(order_by_last_active=True)`` about once a second on a
store whose session rows change that often, so every poll is a miss. The exact ranking used to
probe ``MAX(messages.timestamp)`` for every admitted session on each of them (~100 ms CPU at 22k
sessions). These tests pin both halves: polls after a write probe only the delta and the
candidates, and the page stays identical to the unrestricted query through inserts, deletes,
garbage timestamps (#91536) and compression chains.
"""

import random
from contextlib import contextmanager

import pytest

import hermes_state_recency
from hermes_state import SessionDB

_BASE = 1_700_000_000.0


@pytest.fixture
def db(tmp_path):
    hermes_state_recency.clear()
    database = SessionDB(db_path=tmp_path / "state.db")
    try:
        yield database
    finally:
        database.close()
        hermes_state_recency.clear()


def _write(db, sql, params=()):
    db._execute_write(lambda conn: conn.execute(sql, params))


def _seed(db, count):
    def seed(conn):
        conn.executemany(
            "INSERT INTO sessions (id, source, started_at, last_activity_at, message_count) VALUES (?, 'cli', ?, ?, 1)",
            [(f"s{i:04d}", _BASE + i, _BASE + i + 0.5) for i in range(count)])
        conn.executemany("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, 'user', 'x', ?)",
                         [(f"s{i:04d}", _BASE + i + 0.25) for i in range(count)])
    db._execute_write(seed)


@contextmanager
def _traced(db):
    statements = []
    borrow = db._read_ctx

    @contextmanager
    def traced_read(*args, **kwargs):
        with borrow(*args, **kwargs) as conn:
            if conn is None:
                yield conn
                return
            conn.set_trace_callback(statements.append)
            try:
                yield conn
            finally:
                conn.set_trace_callback(None)

    db._read_ctx = traced_read  # type: ignore[method-assign]
    try:
        yield statements
    finally:
        del db._read_ctx


def _unrestricted_rankings(statements):
    """Ranking statements that probe the messages of EVERY admitted session (the trace callback
    sees the SQL with its bound values expanded)."""
    return [sql for sql in statements if "chain_max AS" in sql and "s.id IN (SELECT value FROM json_each(" not in sql]


def _page(db, **kwargs):
    return db.list_sessions_rich(order_by_last_active=True, compact_rows=True, min_message_count=1, **kwargs)


def test_polls_after_one_insert_do_not_probe_every_session(db, monkeypatch):
    _seed(db, 400)
    probed = []
    real_probe = hermes_state_recency._StoreRecency.probe
    monkeypatch.setattr(hermes_state_recency._StoreRecency, "probe",
                        lambda self, conn, ids: (probed.append(list(ids)), real_probe(self, conn, ids))[1])
    _page(db, limit=20)  # first read builds the store's recency once
    probed.clear()
    db.append_message(session_id="s0007", role="user", content="new")
    with _traced(db) as statements:
        first = _page(db, limit=20)
        second = _page(db, limit=20)
    assert _unrestricted_rankings(statements) == []
    assert sum(len(ids) for ids in probed) <= 2  # the one session the insert touched
    assert [s["id"] for s in first][:1] == ["s0007"] and first == second
    monkeypatch.setattr(hermes_state_recency, "ENABLED", False)
    assert _page(db, limit=20) == first


def _random_store(db, rng, count):
    ids = [f"r{i:03d}" for i in range(count)]

    def pick_ts():
        roll = rng.random()
        if roll < 0.05:
            return 5.4905047707024164e+246  # salvaged garbage double (#91536)
        if roll < 0.08:
            return "not-a-timestamp"
        if roll < 0.25:
            return None
        return _BASE + rng.randrange(0, 5000) + rng.choice((0, 0.5))

    def seed(conn):
        for i, sid in enumerate(ids):
            parent = None
            if i and rng.random() < 0.25:
                parent = ids[rng.randrange(0, i)]
                conn.execute("UPDATE sessions SET end_reason = 'compression', ended_at = ? WHERE id = ?",
                             (_BASE + 1, parent))
            started = pick_ts() or _BASE + rng.randrange(0, 5000)
            conn.execute("INSERT INTO sessions (id, source, started_at, last_activity_at, message_count,"
                         " parent_session_id, model_config) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (sid, rng.choice(("cli", "cli", "cron", "telegram")), started, pick_ts(),
                          rng.randrange(0, 4), parent,
                          '{"_branched_from": "x"}' if parent and rng.random() < 0.2 else None))
            for _ in range(rng.randrange(0, 4)):
                conn.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, 'user', 'x', ?)",
                             (sid, pick_ts() or _BASE + rng.randrange(0, 5000)))
    db._execute_write(seed)
    return ids, pick_ts


def _mutate(db, rng, ids, pick_ts):
    sid = rng.choice(ids)
    roll = rng.random()
    if roll < 0.4:
        _write(db, "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, 'user', 'y', ?)",
               (sid, pick_ts() or _BASE + rng.randrange(0, 6000)))
        if rng.random() < 0.5:
            _write(db, "UPDATE sessions SET message_count = message_count + 1 WHERE id = ?", (sid,))
    elif roll < 0.6:  # a rewind/delete that may lower the true max without touching message_count
        _write(db, "DELETE FROM messages WHERE id = (SELECT MAX(id) FROM messages WHERE session_id = ?)", (sid,))
    elif roll < 0.8:
        _write(db, "UPDATE sessions SET last_activity_at = ? WHERE id = ?", (pick_ts(), sid))
    elif roll < 0.9:
        _write(db, "UPDATE sessions SET message_count = ? WHERE id = ?", (rng.randrange(0, 4), sid))
    else:  # the newest messages go, AUTOINCREMENT still never reuses their ids
        _write(db, "DELETE FROM messages WHERE id > (SELECT MAX(id) - 3 FROM messages)")


@pytest.mark.parametrize("seed", range(4))
def test_pages_match_the_unrestricted_ranking_through_writes(db, monkeypatch, seed):
    rng = random.Random(seed)
    ids, pick_ts = _random_store(db, rng, 120)
    shapes = [dict(limit=5), dict(limit=20, offset=7), dict(limit=60), dict(limit=200),
              dict(limit=10, exclude_sources=["cron"]), dict(limit=15, include_children=True),
              dict(limit=8, min_message_count=0)]
    for _ in range(15):
        for shape in shapes:
            kwargs = {"min_message_count": 1, **shape}
            monkeypatch.setattr(hermes_state_recency, "ENABLED", True)
            fast = db.list_sessions_rich(order_by_last_active=True, compact_rows=True, include_pinned=True,
                                         **kwargs)
            monkeypatch.setattr(hermes_state_recency, "ENABLED", False)
            exact = db.list_sessions_rich(order_by_last_active=True, compact_rows=True, include_pinned=True,
                                          **kwargs)
            assert fast == exact, (seed, shape)
        for _ in range(rng.randrange(1, 4)):
            _mutate(db, rng, ids, pick_ts)


def test_deleted_messages_leave_no_stale_rank(db, monkeypatch):
    """A delete lowers the true max under a cached one; the page must still be the exact one, and the
    stale cached maxima get probed again instead of widening every later read."""
    _seed(db, 200)
    # Old heartbeats, fresh messages: the cache, not last_activity_at, ranks these rows first.
    _write(db, "UPDATE messages SET timestamp = timestamp + 100000 WHERE session_id < 's0100'")
    hermes_state_recency.clear()
    assert [s["id"] for s in _page(db, limit=3)] == ["s0099", "s0098", "s0097"]
    _write(db, "DELETE FROM messages WHERE session_id < 's0100'")
    monkeypatch.setattr(hermes_state_recency, "ENABLED", False)
    exact = _page(db, limit=3)
    monkeypatch.setattr(hermes_state_recency, "ENABLED", True)
    assert _page(db, limit=3) == exact
    assert [s["id"] for s in exact] == ["s0199", "s0198", "s0197"]
    assert not [sid for sid in next(iter(hermes_state_recency._STORES.values())).msg_max if sid < "s0100"]


def test_replaced_store_starts_over(tmp_path):
    hermes_state_recency.clear()
    path = tmp_path / "state.db"
    first = SessionDB(db_path=path)
    _seed(first, 30)
    assert _page(first, limit=5)[0]["id"] == "s0029"
    first.close()
    path.unlink()
    for suffix in ("-wal", "-shm"):
        (tmp_path / f"state.db{suffix}").unlink(missing_ok=True)
    second = SessionDB(db_path=path)
    try:
        _seed(second, 10)
        assert [s["id"] for s in _page(second, limit=3)] == ["s0009", "s0008", "s0007"]
    finally:
        second.close()
        hermes_state_recency.clear()
