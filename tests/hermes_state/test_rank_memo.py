"""Incremental ranked session ids (hermes_state_rank_memo, design t_01a1d563324d7e8e §3 D2).

The session list, the batched sidebar and the project tree share one per-store memo of ranked page ids.
Between requests only the sessions D1's cursor or the ``messages.id`` watermark name are re-ranked
(with the previous page and their chain roots); anything that could LOWER a ranked row falls back to the
exact query. Every test compares against the exact path (``hermes_state_recency.ENABLED = False``).
"""

import random

import pytest

import hermes_state_rank_memo as memo
import hermes_state_recency
from hermes_state import SessionDB

_BASE = 1_700_000_000.0


@pytest.fixture
def db(tmp_path):
    hermes_state_recency.clear()
    memo.OUTCOMES.clear()
    database = SessionDB(db_path=tmp_path / "state.db")
    try:
        yield database
    finally:
        database.close()
        hermes_state_recency.clear()


def _write(db, sql, params=()):
    db._execute_write(lambda conn: conn.execute(sql, params))


def _seed(db, count, prefix="s"):
    def seed(conn):
        conn.executemany(
            "INSERT INTO sessions (id, source, started_at, last_activity_at, message_count, title)"
            " VALUES (?, 'cli', ?, ?, 1, ?)",
            [(f"{prefix}{i:04d}", _BASE + i, _BASE + i + 0.5, f"title {i}") for i in range(count)])
        conn.executemany("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, 'user', ?, ?)",
                         [(f"{prefix}{i:04d}", f"hello {i}", _BASE + i + 0.25) for i in range(count)])
    db._execute_write(seed)


def _page(db, **kwargs):
    kwargs.setdefault("limit", 10)
    return db.list_sessions_rich(order_by_last_active=True, compact_rows=True, min_message_count=1, **kwargs)


def _exact(db, monkeypatch, **kwargs):
    monkeypatch.setattr(hermes_state_recency, "ENABLED", False)
    try:
        return _page(db, **kwargs)
    finally:
        monkeypatch.setattr(hermes_state_recency, "ENABLED", True)


def _ids(rows):
    return [r["id"] for r in rows]


def _outcome(fn):
    before = memo.OUTCOMES.copy()
    out = fn()
    delta = memo.OUTCOMES - before
    assert sum(delta.values()) >= 1, "the memo path did not run"
    return out, delta


def test_unchanged_store_serves_the_memo_without_ranking(db, monkeypatch):
    _seed(db, 40)
    first, delta = _outcome(lambda: _page(db))
    assert set(delta) == {"fallback/no_entry"}
    second, delta = _outcome(lambda: _page(db))
    assert set(delta) == {"served"}
    assert second == first == _exact(db, monkeypatch)


def test_mid_turn_activity_with_no_message_reorders_the_page(db, monkeypatch):
    _seed(db, 40)
    _page(db)
    # touch_session_activity's mid-turn heartbeat: last_activity_at moves, no message lands
    _write(db, "UPDATE sessions SET last_activity_at = ? WHERE id = 's0003'", (_BASE + 10_000,))
    rows, delta = _outcome(lambda: _page(db))
    assert set(delta) == {"reranked"}
    assert _ids(rows)[0] == "s0003"
    assert rows == _exact(db, monkeypatch)


def test_new_message_reorders_through_the_watermark(db, monkeypatch):
    _seed(db, 40)
    _page(db)
    _write(db, "INSERT INTO messages (session_id, role, content, timestamp) VALUES ('s0005', 'user', 'new', ?)",
           (_BASE + 20_000,))
    rows, delta = _outcome(lambda: _page(db))
    assert set(delta) == {"reranked"}
    assert _ids(rows)[0] == "s0005"
    assert rows == _exact(db, monkeypatch)


def test_a_new_continuation_lifts_its_root(db, monkeypatch):
    _seed(db, 40)
    _page(db)

    def continue_root(conn):
        conn.execute("UPDATE sessions SET end_reason = 'compression', ended_at = ? WHERE id = 's0002'",
                     (_BASE + 30,))
        conn.execute("INSERT INTO sessions (id, source, started_at, last_activity_at, message_count,"
                     " parent_session_id) VALUES ('tip', 'cli', ?, ?, 1, 's0002')",
                     (_BASE + 50_000, _BASE + 50_000))
        conn.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES ('tip', 'user', 'go', ?)",
                     (_BASE + 50_001,))
    db._execute_write(continue_root)
    for project in (True, False):
        rows, delta = _outcome(lambda: _page(db, project_compression_tips=project))
        assert "reranked" in delta or "served" in delta, delta
        assert rows == _exact(db, monkeypatch, project_compression_tips=project)
    assert _ids(_page(db, project_compression_tips=False))[0] == "s0002"


def test_archiving_a_page_row_falls_back(db, monkeypatch):
    _seed(db, 40)
    assert "s0039" in _ids(_page(db))
    _write(db, "UPDATE sessions SET archived = 1 WHERE id = 's0039'")
    rows, delta = _outcome(lambda: _page(db))
    assert set(delta) == {"fallback/removed"}
    assert "s0039" not in _ids(rows)
    assert rows == _exact(db, monkeypatch)


def test_a_title_change_on_a_page_row_is_projected(db, monkeypatch):
    _seed(db, 40)
    _page(db)
    _write(db, "UPDATE sessions SET title = 'renamed' WHERE id = 's0035'")
    rows, delta = _outcome(lambda: _page(db))
    assert set(delta) == {"reranked"}
    assert {r["id"]: r["title"] for r in rows}["s0035"] == "renamed"
    assert rows == _exact(db, monkeypatch)


def test_a_session_delete_falls_back(db, monkeypatch):
    _seed(db, 40)
    _page(db)
    db.delete_session("s0038")
    rows, delta = _outcome(lambda: _page(db))
    # delete_session removes the transcript first, so the lowered counter (C9) trips before the page check
    assert set(delta) == {"fallback/lowered"}
    assert "s0038" not in _ids(rows)
    assert rows == _exact(db, monkeypatch)


def test_a_session_row_removed_without_its_messages_falls_back(db, monkeypatch):
    _seed(db, 40)
    _write(db, "INSERT INTO sessions (id, source, started_at, last_activity_at, message_count)"
               " VALUES ('bare', 'cli', ?, ?, 1)", (_BASE + 900, _BASE + 900))
    assert "bare" in _ids(_page(db))
    _write(db, "DELETE FROM sessions WHERE id = 'bare'")
    rows, delta = _outcome(lambda: _page(db))
    assert set(delta) == {"fallback/removed"}
    assert rows == _exact(db, monkeypatch)


def test_a_message_delete_falls_back(db, monkeypatch):
    _seed(db, 40)
    _write(db, "INSERT INTO messages (session_id, role, content, timestamp) VALUES ('s0001', 'user', 'x', ?)",
           (_BASE + 90_000,))
    assert _ids(_page(db))[0] == "s0001"
    _write(db, "DELETE FROM messages WHERE session_id = 's0001' AND content = 'x'")
    rows, delta = _outcome(lambda: _page(db))
    assert set(delta) == {"fallback/lowered"}
    assert "s0001" not in _ids(rows)
    assert rows == _exact(db, monkeypatch)


@pytest.mark.parametrize("untrusted", [False, True])
def test_timestamp_raise_rebuilds_candidate_cache_after_reset(db, monkeypatch, untrusted):
    _seed(db, 400)
    assert "s0000" not in _ids(_page(db))
    if untrusted:
        _write(db, f"DROP TRIGGER {memo.PREFIX}messages_move")
    _write(db, "UPDATE messages SET timestamp = ? WHERE session_id = 's0000'", (_BASE + 90_000,))
    rows, delta = _outcome(lambda: _page(db))
    assert set(delta) == {"fallback/untrusted" if untrusted else "fallback/lowered"}
    assert _ids(rows)[0] == "s0000"
    assert rows == _exact(db, monkeypatch)


def test_a_message_rewind_falls_back(db, monkeypatch):
    _seed(db, 40)
    _page(db)
    # rewind_in_transaction's model-side soft delete (hermes_state_mutation_transcript)
    _write(db, "UPDATE messages SET active = 0 WHERE session_id = 's0039'")
    rows, delta = _outcome(lambda: _page(db))
    assert set(delta) == {"fallback/lowered"}
    assert rows == _exact(db, monkeypatch)


def test_a_session_lowered_in_rank_falls_back(db, monkeypatch):
    _seed(db, 40)
    _page(db)
    _write(db, "UPDATE sessions SET last_activity_at = ?, started_at = ? WHERE id = 's0039'", (_BASE, _BASE))
    rows, delta = _outcome(lambda: _page(db))
    assert set(delta) == {"fallback/rank_lowered"}
    assert rows == _exact(db, monkeypatch)


def test_params_change_starts_a_new_entry(db, monkeypatch):
    _seed(db, 40)
    _page(db)
    rows, delta = _outcome(lambda: _page(db, exclude_sources=["cron"]))
    assert set(delta) == {"fallback/no_entry"}
    assert rows == _exact(db, monkeypatch, exclude_sources=["cron"])


def test_untrusted_cursor_falls_back(db, monkeypatch):
    _seed(db, 40)
    _page(db)

    def drop(conn):
        import hermes_state_session_change as sc
        sc.drop_triggers(conn)
    db._execute_write(drop)
    _write(db, "UPDATE sessions SET last_activity_at = ? WHERE id = 's0003'", (_BASE + 10_000,))
    rows, delta = _outcome(lambda: _page(db))
    assert set(delta) == {"fallback/untrusted"}
    assert rows == _exact(db, monkeypatch)


def test_missing_lowered_trigger_is_untrusted(db, monkeypatch):
    _seed(db, 40)
    _page(db)
    _write(db, f"DROP TRIGGER {memo.PREFIX}messages_delete")
    _write(db, "DELETE FROM messages WHERE session_id = 's0039'")
    rows, delta = _outcome(lambda: _page(db))
    assert set(delta) == {"fallback/untrusted"}
    assert rows == _exact(db, monkeypatch)


# ── preview cache: exact, or not used ─────────────────────────────────────────

def test_preview_follows_a_hidden_flag_a_redaction_and_a_first_message(db, monkeypatch):
    _seed(db, 20)
    _write(db, "INSERT INTO sessions (id, source, started_at, last_activity_at, message_count)"
               " VALUES ('empty', 'cli', ?, ?, 1)", (_BASE + 500, _BASE + 500))
    _page(db)
    _page(db)  # the second read serves every preview from the cache
    first = {r["id"]: r["preview"] for r in _page(db)}
    assert first["s0019"] == "hello 19" and first["empty"] == ""
    _write(db, "UPDATE messages SET content = 'redacted' WHERE session_id = 's0019'")
    _write(db, "INSERT INTO messages (session_id, role, content, timestamp) VALUES ('s0018', 'user', 'later', ?)",
           (_BASE + 18.5,))
    _write(db, "UPDATE messages SET display_kind = 'hidden' WHERE session_id = 's0018' AND content = 'hello 18'")
    _write(db, "INSERT INTO messages (session_id, role, content, timestamp) VALUES ('empty', 'user', 'first!', ?)",
           (_BASE + 501,))
    rows = _page(db)
    got = {r["id"]: r["preview"] for r in rows}
    assert (got["s0019"], got["s0018"], got["empty"]) == ("redacted", "later", "first!")
    assert rows == _exact(db, monkeypatch)


def test_old_snapshot_does_not_project_a_newer_first_message(db, monkeypatch):
    _seed(db, 5)
    _write(db, "DELETE FROM messages WHERE session_id = 's0004'")
    before = _page(db)
    assert before[0]["preview"] == ""
    original = memo.project

    def interleave(conn, rank, *args):
        monkeypatch.setattr(memo, "project", original)
        with SessionDB(db_path=db.db_path) as newer:
            _write(newer, "INSERT INTO messages (session_id, role, content, timestamp)"
                          " VALUES ('s0004', 'user', 'first!', ?)", (_BASE + 5,))
            assert _page(newer)[0]["preview"] == "first!"
        return original(conn, rank, *args)

    monkeypatch.setattr(memo, "project", interleave)
    assert _page(db) == before


def test_a_stale_snapshot_never_installs_a_preview(db):
    _seed(db, 5)
    _page(db)
    store = next(iter(hermes_state_recency._STORES.values()))
    gen = store.rank_memo.generation
    store.rank_memo.advance_for_test()  # another reader moved the clock after this one read
    store.rank_memo.install_previews(gen, {"s0001": (0, "stale")})
    assert "s0001" not in store.rank_memo.previews or store.rank_memo.previews["s0001"][1] != "stale"


def test_preview_cache_is_off_without_the_display_revision_trigger(db, monkeypatch):
    _seed(db, 10)
    _page(db)
    _page(db)
    _write(db, "DROP TRIGGER messages_display_revision_update")
    _write(db, "UPDATE messages SET display_kind = 'hidden' WHERE session_id = 's0009'")
    rows = _page(db)
    assert {r["id"]: r["preview"] for r in rows}["s0009"] == ""
    assert rows == _exact(db, monkeypatch)


# ── the store LRU holds every profile ─────────────────────────────────────────

def test_store_capacity_covers_every_profile():
    hermes_state_recency.reserve_stores(11 + 4)
    assert hermes_state_recency.max_stores() >= 15
    hermes_state_recency.reserve_stores(2)
    assert hermes_state_recency.max_stores() >= 15  # never shrinks below a reservation this process made


# ── randomized equivalence ────────────────────────────────────────────────────

def _mutate(db, rng, ids):
    sid = rng.choice(ids)
    roll = rng.random()
    ts = _BASE + rng.randrange(0, 6000) + rng.choice((0, 0.5))
    if roll < 0.3:
        _write(db, "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, 'user', ?, ?)",
               (sid, f"m{rng.random()}", ts))
    elif roll < 0.45:
        _write(db, "UPDATE sessions SET last_activity_at = ? WHERE id = ?", (ts, sid))
    elif roll < 0.5:
        _write(db, "UPDATE sessions SET title = ? WHERE id = ?", (f"t{rng.random()}", sid))
    elif roll < 0.55:
        _write(db, "UPDATE sessions SET archived = ? WHERE id = ?", (rng.randrange(0, 2), sid))
    elif roll < 0.6:
        _write(db, "DELETE FROM messages WHERE id = (SELECT MAX(id) FROM messages WHERE session_id = ?)", (sid,))
    elif roll < 0.65:
        _write(db, "UPDATE sessions SET end_reason = ?, ended_at = ? WHERE id = ?",
               (rng.choice(("compression", None, "branched")), _BASE + 1, sid))
    elif roll < 0.72:
        new = f"c{rng.randrange(10**9)}"
        ids.append(new)
        _write(db, "INSERT INTO sessions (id, source, started_at, last_activity_at, message_count, parent_session_id)"
                   " VALUES (?, ?, ?, ?, 1, ?)", (new, rng.choice(("cli", "cron")), ts, ts, sid))
    elif roll < 0.76:
        _write(db, "UPDATE sessions SET source = ? WHERE id = ?", (rng.choice(("cli", "cron", "telegram")), sid))
    elif roll < 0.8:
        _write(db, "UPDATE sessions SET message_count = ? WHERE id = ?", (rng.randrange(0, 3), sid))
    elif roll < 0.84:
        _write(db, "UPDATE messages SET active = 0 WHERE id = (SELECT MAX(id) FROM messages WHERE session_id = ?)",
               (sid,))
    elif roll < 0.88:
        _write(db, "UPDATE sessions SET started_at = ? WHERE id = ?", (ts, sid))
    elif roll < 0.9:
        def drop(conn):
            if conn.execute("SELECT 1 FROM sessions WHERE parent_session_id = ?", (sid,)).fetchone():
                return
            conn.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
            conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))
        db._execute_write(drop)
    else:
        _write(db, "UPDATE messages SET display_kind = ? WHERE id = (SELECT MIN(id) FROM messages WHERE"
                   " session_id = ?)", (rng.choice(("hidden", None)), sid))


@pytest.mark.parametrize("seed", range(6))
def test_memo_pages_match_the_exact_ranking_through_writes(db, monkeypatch, seed):
    rng = random.Random(seed)
    ids = [f"r{i:03d}" for i in range(90)]

    def seed_store(conn):
        for i, sid in enumerate(ids):
            parent = ids[rng.randrange(0, i)] if i and rng.random() < 0.2 else None
            if parent:
                conn.execute("UPDATE sessions SET end_reason = 'compression', ended_at = ? WHERE id = ?",
                             (_BASE + 1, parent))
            ts = _BASE + rng.randrange(0, 5000)
            conn.execute("INSERT INTO sessions (id, source, started_at, last_activity_at, message_count,"
                         " parent_session_id) VALUES (?, ?, ?, ?, ?, ?)",
                         (sid, rng.choice(("cli", "cli", "cron")), ts,
                          rng.choice((None, ts + rng.randrange(0, 900))), rng.randrange(0, 3), parent))
            for _ in range(rng.randrange(0, 3)):
                conn.execute("INSERT INTO messages (session_id, role, content, timestamp)"
                             " VALUES (?, 'user', ?, ?)", (sid, f"x{rng.random()}", ts + rng.randrange(0, 900)))
    db._execute_write(seed_store)
    shapes = [dict(limit=5), dict(limit=25), dict(limit=200), dict(limit=10, exclude_sources=["cron"]),
              dict(limit=12, include_pinned=True), dict(limit=8, project_compression_tips=False)]
    for _ in range(25):
        for shape in shapes:
            fast = _page(db, **shape)
            assert fast == _exact(db, monkeypatch, **shape), (seed, shape)
        for _ in range(rng.randrange(1, 4)):
            _mutate(db, rng, ids)
    assert memo.OUTCOMES["reranked"] + memo.OUTCOMES["served"] > 0


# ── the SUBSTR-first preview window equals the replace-first one ──────────────

def test_preview_window_equals_replacing_the_whole_content_first():
    import sqlite3

    from hermes_state_common import _PREVIEW_CONTENT_SQL, _sql_preview_window
    rng = random.Random(7)
    alphabet = ["a", "\n", "\r", " ", "é", "😀", "\r\n", "z", "\t"]
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE m (content TEXT)")
    for _ in range(300):
        conn.execute("INSERT INTO m VALUES (?)", ("".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 400))),))
    for start, length in ((1, 63), (1, 1), (1, 500), (-200, None), (-1, None), (5, 3)):
        old = f"SUBSTR({_PREVIEW_CONTENT_SQL}, {start}" + (f", {length})" if length else ")")
        new = _sql_preview_window(start, length)
        assert conn.execute(f"SELECT COUNT(*) FROM m WHERE {old} IS NOT {new}").fetchone()[0] == 0, (start, length)
