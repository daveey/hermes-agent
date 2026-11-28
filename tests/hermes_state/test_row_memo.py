"""Kept projected list rows across sidebar reads (hermes_state_row_memo, card t_01a21ab4eb964866). [daveey]

Every read is compared with a fresh full read of the same snapshot: the exact path
(``hermes_state_recency.ENABLED = False``: no memo, full ranking, wide pin back-fill).
"""

import os
import random
import sqlite3

import pytest

import hermes_state_page_pack as pack
import hermes_state_rank_memo as rank_memo
import hermes_state_recency as recency
from hermes_state import SessionDB

_BASE = 1_700_000_000.0
_SLICES = {"recents": dict(exclude_sources=["cron"], limit=8), "cron": dict(source="cron", limit=4),
           "messaging": dict(exclude_sources=["cli", "cron"], limit=4)}


@pytest.fixture
def db(tmp_path):
    recency.clear()
    database = SessionDB(db_path=tmp_path / "state.db")
    try:
        yield database
    finally:
        database.close()
        recency.clear()


def _write(db, sql, params=()):
    db._execute_write(lambda conn: conn.execute(sql, params))


def _seed(db, count=60, pins=(3, 9, 17, 21, 30, 41)):
    sources = ["cli", "cron", "telegram", "cli", "slack"]

    def seed(conn):
        conn.executemany(
            "INSERT INTO sessions (id, source, started_at, last_activity_at, message_count, title, pinned,"
            " input_tokens, estimated_cost_usd) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?)",
            [(f"s{i:03d}", sources[i % 5], _BASE + i, _BASE + i + 0.5, f"title {i}", int(i in pins), i, i / 7)
             for i in range(count)])
        conn.executemany("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, 'user', ?, ?)",
                         [(f"s{i:03d}", f"hello {i}", _BASE + i + 0.25) for i in range(count)])
    db._execute_write(seed)


def _sidebar(db):
    return {key: db.list_sessions_rich(offset=0, min_message_count=1, include_archived=False, archived_only=False,
                                       order_by_last_active=True, compact_rows=True, include_pinned=True, **kw)
            for key, kw in _SLICES.items()}


def _fresh(db, monkeypatch):
    monkeypatch.setattr(recency, "ENABLED", False)
    try:
        return _sidebar(db)
    finally:
        monkeypatch.setattr(recency, "ENABLED", True)


def _kept_stats():
    import hermes_state_row_memo as row_memo
    return row_memo.STATS


def _assert_fresh(db, monkeypatch):
    got = _sidebar(db)
    assert got == _fresh(db, monkeypatch)
    return got


def _row(slices, sid):
    return next(r for rows in slices.values() for r in rows if r["id"] == sid)


def _wide_rows(monkeypatch):
    """Rows the wide projection (the packed page/pin reads that carry ``_preview_raw``) decoded."""
    seen = []
    for name in ("read_page", "read_chunks"):
        original = getattr(pack, name)

        def counted(conn, sql, params, _original=original):
            rows = _original(conn, sql, params)
            seen.extend(r["id"] for r in rows if "_preview_raw" in r)
            return rows
        monkeypatch.setattr(pack, name, counted)
    return seen


def test_unchanged_rows_are_not_reprojected_after_an_unrelated_write(db, monkeypatch):
    """RED on origin/main: every slice re-projected its whole page and every off-page pin."""
    _seed(db)
    _sidebar(db)
    seen = _wide_rows(monkeypatch)
    _write(db, "UPDATE sessions SET last_activity_at = ? WHERE id = 's058'", (_BASE + 500,))
    got = _sidebar(db)
    assert sorted(set(seen)) == ["s058"], seen
    del seen[:]
    _write(db, "UPDATE sessions SET title = 'pinned rename' WHERE id = 's009'")
    got = _sidebar(db)
    assert sorted(set(seen)) == ["s009"], seen
    assert _row(got, "s009")["title"] == "pinned rename"
    assert got == _fresh(db, monkeypatch)


def test_commit_between_reads_is_visible_on_next_read(db, monkeypatch):
    """Notification loss: a session commit between two reads (no other signal) shows on the next read."""
    _seed(db)
    first = _sidebar(db)
    assert _row(first, "s058")["title"] == "title 58"
    _write(db, "UPDATE sessions SET title = 'renamed', end_reason = 'user' WHERE id = 's058'")
    got = _assert_fresh(db, monkeypatch)
    assert _row(got, "s058")["title"] == "renamed"
    db.append_message("s041", role="user", content="later")  # a pin: its last_active moves
    got = _assert_fresh(db, monkeypatch)
    assert _row(got, "s041")["last_active"] > _BASE + 41


def test_counter_only_and_transcript_rewrites_are_visible(db, monkeypatch):
    """The cursor ignores counters (tokens, cost, transcript_revision): kept rows must still see them."""
    _seed(db)
    _sidebar(db)
    _write(db, "UPDATE sessions SET input_tokens = 999, estimated_cost_usd = 0.1 + 0.2 WHERE id = 's058'")
    got = _assert_fresh(db, monkeypatch)
    assert _row(got, "s058")["input_tokens"] == 999
    # One ULP apart: a 15-digit comparison would call these equal.
    _write(db, "UPDATE sessions SET estimated_cost_usd = 0.3 WHERE id = 's058'")
    got = _assert_fresh(db, monkeypatch)
    assert _row(got, "s058")["estimated_cost_usd"] == 0.3
    _write(db, "UPDATE messages SET content = 'rewritten prompt' WHERE session_id = 's030'")
    got = _assert_fresh(db, monkeypatch)
    assert _row(got, "s030")["preview"] == "rewritten prompt"


def test_pin_unpin_delete_and_reinsert(db, monkeypatch):
    _seed(db)
    _sidebar(db)
    _write(db, "UPDATE sessions SET pinned = 1 WHERE id = 's001'")
    got = _assert_fresh(db, monkeypatch)
    assert _row(got, "s001")["pinned"] == 1
    _write(db, "UPDATE sessions SET pinned = 0 WHERE id = 's009'")
    got = _assert_fresh(db, monkeypatch)
    assert "s009" not in [r["id"] for r in got["recents"]]
    _write(db, "UPDATE sessions SET pinned = 1 WHERE id = 's009'")
    got = _assert_fresh(db, monkeypatch)
    assert _row(got, "s009")["pinned"] == 1
    # Delete a kept pin and insert another row under the same id with different content (no messages, so only
    # the session cursor's delete/insert stamps can tell).
    _write(db, "INSERT INTO sessions (id, source, started_at, message_count, title, pinned)"
               " VALUES ('p01', 'cli', ?, 1, 'first life', 1)", (_BASE + 21.5,))
    got = _assert_fresh(db, monkeypatch)
    assert _row(got, "p01")["title"] == "first life"
    _sidebar(db)
    _write(db, "DELETE FROM sessions WHERE id = 'p01'")
    got = _assert_fresh(db, monkeypatch)
    assert "p01" not in [r["id"] for k in got for r in got[k]]
    _write(db, "INSERT INTO sessions (id, source, started_at, message_count, title, pinned)"
               " VALUES ('p01', 'cli', ?, 1, 'reborn', 1)", (_BASE + 21.5,))
    got = _assert_fresh(db, monkeypatch)
    assert _row(got, "p01")["title"] == "reborn"
    _write(db, "DELETE FROM sessions WHERE id = 'p01'")
    _write(db, "INSERT INTO sessions (id, source, started_at, message_count, title, pinned)"
               " VALUES ('p01', 'cli', ?, 1, 'third life', 1)", (_BASE + 21.5,))
    got = _assert_fresh(db, monkeypatch)
    assert _row(got, "p01")["title"] == "third life"


def test_in_place_message_timestamp_move_reads_afresh(db, monkeypatch):
    """A message timestamp rewritten in place stamps no session and no counter: only the lowered counter (a
    whole-memo reset) tells. The kept rows must not survive it."""
    _seed(db)
    _sidebar(db)
    _write(db, "UPDATE sessions SET last_activity_at = NULL WHERE id = 's009'")
    _sidebar(db)  # kept again after the stamp
    _write(db, "UPDATE messages SET timestamp = ? WHERE session_id = 's009'", (_BASE + 1.5,))
    got = _assert_fresh(db, monkeypatch)
    assert _row(got, "s009")["last_active"] != _BASE + 9.25


def test_schema_change_gap_reads_afresh(db, monkeypatch):
    """Triggers dropped (DDL: the cursor cannot vouch), an unstamped write, triggers reinstalled: rows kept
    before the gap must never be served after it."""
    import hermes_state_session_change as sc
    _seed(db)
    _sidebar(db)
    db._execute_write(lambda conn: sc.drop_triggers(conn))
    _write(db, "UPDATE sessions SET title = 'during gap' WHERE id = 's009'")
    got = _assert_fresh(db, monkeypatch)
    assert _row(got, "s009")["title"] == "during gap"
    db._execute_write(lambda conn: sc.ensure_installed(conn))
    got = _assert_fresh(db, monkeypatch)
    assert _row(got, "s009")["title"] == "during gap"
    _sidebar(db)
    db._execute_write(lambda conn: sc.drop_triggers(conn))
    _write(db, "UPDATE sessions SET title = 'second gap' WHERE id = 's009'")
    db._execute_write(lambda conn: sc.ensure_installed(conn))  # no read in between: the epoch rotated
    got = _assert_fresh(db, monkeypatch)
    assert _row(got, "s009")["title"] == "second gap"


def test_replaced_file_reads_afresh(db, monkeypatch, tmp_path):
    _seed(db)
    _sidebar(db)
    copy = tmp_path / "copy.db"
    src = sqlite3.connect(db.db_path)  # health: allow HX015 -- test fixture copy of a tmp store
    dst = sqlite3.connect(copy)  # health: allow HX015 -- test fixture copy of a tmp store
    src.backup(dst)
    src.close()
    dst.execute("UPDATE sessions SET title = 'from the other file' WHERE id = 's009'")
    dst.commit()
    dst.close()
    db.close()
    for suffix in ("-wal", "-shm"):
        if os.path.exists(f"{db.db_path}{suffix}"):
            os.remove(f"{db.db_path}{suffix}")
    os.replace(copy, db.db_path)
    reader = SessionDB(db_path=db.db_path)
    try:
        got = _assert_fresh(reader, monkeypatch)
        assert _row(got, "s009")["title"] == "from the other file"
    finally:
        reader.close()


def test_stale_generation_never_serves_or_installs(db):
    """An older reader (its snapshot advanced the clock to an earlier generation) neither reads kept rows a newer
    one installed nor installs its own."""
    import hermes_state_row_memo as row_memo
    _seed(db)
    _sidebar(db)
    store = recency._store_for(str(db.db_path))
    memo = store.rank_memo
    with db._read_ctx() as conn:
        with store.lock:
            memo.advance(conn)
            generation = memo.generation
    ids_rows = [("s009", None, None, None)]
    fetched = []

    def fetch(rows):
        fetched.append([r[0] for r in rows])
        return [{"id": "s009", "transcript_revision": 0, "_preview_raw": "old", "_effective_last_active": None}]

    with db._read_ctx() as conn:
        assert row_memo.project(conn, memo, ids_rows, "", "HEAD", generation, fetch)[0]["_preview_raw"] == "old"
        assert memo.rows.get("HEAD", ["s009"])  # installed at the current generation
        memo.advance_for_test()  # a newer snapshot advanced the clock
        fetched.clear()
        row_memo.project(conn, memo, ids_rows, "", "HEAD", generation, fetch)
        assert fetched == [["s009"]], "a stale-generation reader was served kept rows"
        memo.rows.clear()
        row_memo.project(conn, memo, ids_rows, "", "HEAD", generation, fetch)
        assert not memo.rows.get("HEAD", ["s009"]), "a stale-generation reader installed rows"


def test_pin_ties_take_the_wide_read(db, monkeypatch):
    _seed(db)
    _write(db, "UPDATE sessions SET started_at = ? WHERE id IN ('s003', 's009', 's017')", (_BASE,))
    wide = []
    original = pack.read_chunks

    def chunks(conn, sql, params):
        wide.append(sql)
        return original(conn, sql, params)
    monkeypatch.setattr(pack, "read_chunks", chunks)
    for _ in range(2):
        fresh = _fresh(db, monkeypatch)
        del wide[:]
        assert _sidebar(db) == fresh
        assert wide  # the tied pins' order is the wide read's sorter order, never the ids'


def test_missing_preview_revision_trigger_keeps_nothing(db, monkeypatch):
    """Without the exactly installed ``messages_*_revision_update`` triggers a content rewrite moves nothing the
    kept rows can see (no cursor stamp, no counter): nothing may be kept."""
    _seed(db)
    _write(db, "DROP TRIGGER messages_transcript_revision_update")
    _sidebar(db)
    _sidebar(db)
    _write(db, "UPDATE messages SET content = 'rewritten without trigger' WHERE session_id = 's058'")
    got = _assert_fresh(db, monkeypatch)
    assert _row(got, "s058")["preview"] == "rewritten without trigger"


def test_commit_between_page_and_pin_reads_is_visible(db, monkeypatch):
    """The pin back-fill reads in its own snapshot: a pin row kept by an earlier read and changed after the page
    projection must come back changed (its read advances the clock itself)."""
    _seed(db)
    _sidebar(db)
    original = rank_memo.project
    done = []

    def project(conn, *args, **kwargs):
        rows = original(conn, *args, **kwargs)
        if not done and args[1] and args[1][0][3] is not None:  # the ranked page, before the pin read
            _write(db, "UPDATE sessions SET title = 'between' WHERE id = 's003'")
            done.append(True)
        return rows
    monkeypatch.setattr(rank_memo, "project", project)
    got = _sidebar(db)
    assert done and _row(got, "s003")["title"] == "between"


def test_lowering_between_page_and_pin_reads_invalidates_candidates(db, monkeypatch):
    """The pin back-fill's own clock advance can consume a 'lowered' reason: it must leave D1's candidates
    invalidated for the next ranked read, as ranked_ids' exact path does."""
    _seed(db, count=200, pins=(3, 9))
    _sidebar(db)
    _sidebar(db)
    original = rank_memo.project
    done = []

    def project(conn, *args, **kwargs):
        rows = original(conn, *args, **kwargs)
        if not done and args[1] and args[1][0][3] is not None:  # the ranked page, before the pin read
            _write(db, "UPDATE messages SET timestamp = ? WHERE session_id = 's010'", (_BASE + 9000,))
            done.append(True)
        return rows
    monkeypatch.setattr(rank_memo, "project", project)
    _sidebar(db)
    monkeypatch.setattr(rank_memo, "project", original)
    assert done
    got = _sidebar(db)
    assert next(r["id"] for r in got["recents"]) == "s010"
    assert got == _fresh(db, monkeypatch)


def test_kept_pin_read_shares_one_snapshot_and_releases_it(db, monkeypatch):
    """The ordered pin ids, the clock advance and the projection share one snapshot: a commit after the ids
    were read is not visible in this read, and is visible in the next one."""
    import hermes_state_pinned_backfill as pins
    _seed(db)
    _sidebar(db)
    original = rank_memo.project
    commits = []

    def project(conn, *args, **kwargs):
        if not commits and args[1] and args[1][0][3] is None:  # the pin back-fill (no ranking value)
            assert conn.in_transaction
            _write(db, "UPDATE sessions SET title = 'late' WHERE id = 's003'")
            commits.append(True)
        return original(conn, *args, **kwargs)
    monkeypatch.setattr(rank_memo, "project", project)
    got = _sidebar(db)
    assert commits and _row(got, "s003")["title"] == "title 3"
    monkeypatch.setattr(rank_memo, "project", original)
    got = _assert_fresh(db, monkeypatch)
    assert _row(got, "s003")["title"] == "late"

    def fail(*_args, **_kwargs):
        raise ValueError("decode failure")
    monkeypatch.setattr(rank_memo, "project", fail)
    borrowed = []
    real_ctx = db._read_ctx

    def ctx(*a, **k):
        cm = real_ctx(*a, **k)
        conn = cm.__enter__()
        borrowed.append(conn)

        class _Wrap:
            def __enter__(self):
                return conn

            def __exit__(self, *exc):
                return cm.__exit__(*exc)
        return _Wrap()
    monkeypatch.setattr(db, "_read_ctx", ctx)
    with pytest.raises(ValueError, match="decode failure"):
        pins.backfill_pins(db, [], *_compact_args(db))
    assert borrowed and not any(c.in_transaction for c in borrowed)


def _compact_args(db):
    from hermes_state_common import _sql_preview_raw
    head = f"SELECT {db._compact_session_cols()},\n                    {_sql_preview_raw()},\n                    "
    return head, "", ["s.pinned = 1"], []


def test_kept_pin_read_retries_ioerr(db, monkeypatch):
    import hermes_state_pinned_backfill as pins
    _seed(db)
    _sidebar(db)
    original = rank_memo.project
    attempts = []

    def project(conn, *args, **kwargs):
        attempts.append(conn)
        assert conn.in_transaction
        if len(attempts) == 1:
            raise sqlite3.OperationalError("disk I/O error")
        return original(conn, *args, **kwargs)
    monkeypatch.setattr(rank_memo, "project", project)
    rows = []
    pins.backfill_pins(db, rows, *_compact_args(db))
    assert len(attempts) == 2 and [r["id"] for r in rows] == ["s041", "s030", "s021", "s017", "s009", "s003"]


@pytest.mark.parametrize("seed_value", [3, 11, 29])
def test_randomized_write_mix_matches_fresh_reads(db, monkeypatch, seed_value):
    _seed(db, count=80, pins=(2, 7, 13, 19, 33, 47, 52))
    rng = random.Random(seed_value)
    _kept_stats().clear()
    clock = [_BASE + 1000]
    for _ in range(40):
        sid = f"s{rng.randrange(80):03d}"
        roll = rng.random()
        clock[0] += 1
        if roll < 0.2:
            _write(db, "UPDATE sessions SET last_activity_at = ? WHERE id = ?", (clock[0], sid))
        elif roll < 0.4:
            db.append_message(sid, role=rng.choice(["user", "assistant"]), content=f"m{clock[0]}")
        elif roll < 0.5:
            _write(db, "UPDATE sessions SET title = ? WHERE id = ?", (f"t{clock[0]}", sid))
        elif roll < 0.6:
            _write(db, "UPDATE sessions SET pinned = 1 - pinned WHERE id = ?", (sid,))
        elif roll < 0.68:
            _write(db, "UPDATE sessions SET archived = 1 - archived WHERE id = ?", (sid,))
        elif roll < 0.78:
            _write(db, "UPDATE sessions SET output_tokens = output_tokens + 5, actual_cost_usd = ? WHERE id = ?",
                   (rng.random(), sid))
        elif roll < 0.85:
            _write(db, "UPDATE messages SET content = ? WHERE id = (SELECT MIN(id) FROM messages"
                       " WHERE session_id = ?)", (f"edit {clock[0]}", sid))
        elif roll < 0.9:
            _write(db, "UPDATE messages SET display_kind = 'hidden' WHERE id = (SELECT MIN(id) FROM messages"
                       " WHERE session_id = ?)", (sid,))
        elif roll < 0.95:
            _write(db, "DELETE FROM messages WHERE id = (SELECT MAX(id) FROM messages WHERE session_id = ?)",
                   (sid,))
        else:
            _write(db, "UPDATE sessions SET message_count = 0 WHERE id = ?", (sid,))
        _assert_fresh(db, monkeypatch)
        if rng.random() < 0.3:
            _assert_fresh(db, monkeypatch)  # a second read without a write in between
    assert _kept_stats()["kept"] > _kept_stats()["projected"], _kept_stats()
