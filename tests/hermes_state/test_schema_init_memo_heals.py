"""[daveey] Repairs the schema-init memo must not freeze (PR #2164 review, t_01a1d4c34f905bf3).

Before the memo every open ran the NULL-``active`` heal and retried a failed unique-title repair. With the
memo, a settled store's later opens skip the full init, so: the NULL-active heal runs from the throttled
per-store heal, a failed title repair is not memoized, and a heal that fails rolls back whole and leaves
the interval unclaimed so the next open retries it.
"""

from __future__ import annotations

import logging
import sqlite3

import pytest

import hermes_state_schema
from hermes_state import SessionDB


@pytest.fixture(autouse=True)
def fresh_memo():
    import hermes_state_schema_memo

    hermes_state_schema_memo.clear()
    yield
    hermes_state_schema_memo.clear()


@pytest.fixture
def reconciles(monkeypatch):
    seen = {"n": 0}
    real = hermes_state_schema.SessionSchemaMixin._reconcile_columns

    def spy(self, cursor):
        seen["n"] += 1
        return real(self, cursor)

    monkeypatch.setattr(hermes_state_schema.SessionSchemaMixin, "_reconcile_columns", spy)
    return seen


def _after_interval(monkeypatch):
    import hermes_state_schema_memo as memo

    now = memo.time.monotonic()
    monkeypatch.setattr(memo.time, "monotonic", lambda: now + memo.HEAL_INTERVAL_S + 1)


def _legacy_nullable_active_store(path):
    """A v12 store whose ``active`` column lacks NOT NULL DEFAULT 1 (#51646), as an older build left it."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE schema_version (version INTEGER);
        INSERT INTO schema_version VALUES (12);
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, source TEXT, started_at REAL, ended_at REAL,
            message_count INTEGER DEFAULT 0, tool_call_count INTEGER DEFAULT 0,
            title TEXT, parent_session_id TEXT, model_config TEXT
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT,
            tool_call_id TEXT, tool_calls TEXT, tool_name TEXT,
            timestamp REAL NOT NULL, token_count INTEGER, finish_reason TEXT,
            reasoning TEXT, reasoning_content TEXT, reasoning_details TEXT,
            codex_reasoning_items TEXT, codex_message_items TEXT,
            platform_message_id TEXT, observed INTEGER DEFAULT 0
        );
        CREATE TABLE state_meta (key TEXT PRIMARY KEY, value TEXT);
        """
    )
    conn.execute("ALTER TABLE messages ADD COLUMN active INTEGER")
    conn.execute("ALTER TABLE messages ADD COLUMN compacted INTEGER DEFAULT 0")
    conn.execute("INSERT INTO sessions (id, source, started_at) VALUES ('s1', 'discord', 1.0)")
    conn.commit()
    conn.close()


def test_a_null_active_row_written_after_the_first_init_is_healed_by_the_throttled_heal(
        tmp_path, reconciles, monkeypatch):
    path = tmp_path / "state.db"
    _legacy_nullable_active_store(path)
    SessionDB(db_path=path).close()  # full init: migrates and memoizes the store
    assert reconciles["n"] == 1
    con = sqlite3.connect(path)  # an older-build writer on the same store omits ``active``
    con.execute("INSERT INTO messages (session_id, role, content, timestamp, active) "
                "VALUES ('s1', 'user', 'late hidden turn', 2.0, NULL)")
    con.commit()
    con.close()
    _after_interval(monkeypatch)
    db = SessionDB(db_path=path)
    try:
        assert reconciles["n"] == 1  # a memo hit, not a full init
        active = db._conn.execute("SELECT active FROM messages WHERE content = 'late hidden turn'").fetchone()[0]
        assert active == 1
    finally:
        db.close()


def test_a_failed_unique_title_repair_is_retried_on_the_next_open(tmp_path, reconciles, monkeypatch):
    path = tmp_path / "state.db"
    db = SessionDB(db_path=path)
    db.create_session("a", "cli")
    db.create_session("b", "cli")
    db.close()
    con = sqlite3.connect(path)  # duplicates from before the constraint
    con.execute("DROP INDEX idx_sessions_title_unique")
    con.execute("UPDATE sessions SET title = 'dup', title_source = 'user'")
    con.commit()
    con.close()

    def locked(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")

    real_next = hermes_state_schema.next_title_in_lineage
    monkeypatch.setattr(hermes_state_schema, "next_title_in_lineage", locked)
    SessionDB(db_path=path).close()  # the repair fails: no index
    monkeypatch.setattr(hermes_state_schema, "next_title_in_lineage", real_next)
    db = SessionDB(db_path=path)
    try:
        index = db._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = 'idx_sessions_title_unique'").fetchone()
        assert index is not None
        assert sorted(r[0] for r in db._conn.execute("SELECT title FROM sessions")) == ["dup", "dup #2"]
    finally:
        db.close()


def _polluted_store_with_an_unindexed_message(path):
    db = SessionDB(db_path=path)
    db.create_session("old", "telegram", session_key="k", chat_id="c", chat_type="dm", user_id="u")
    db.create_session("live", "telegram", parent_session_id="old", model_config={"_reset_from": "old"},
                      session_key="k", chat_id="c", chat_type="dm", user_id="u")
    db.close()  # settled and memoized
    con = sqlite3.connect(path)
    con.execute("UPDATE sessions SET model_config = json_set(model_config, '$._delegate_from', 'old') "
                "WHERE id = 'live'")
    # Written while the marker hid the session: the trigram trigger skips it.
    con.execute("INSERT INTO messages (session_id, role, content, timestamp) "
                "VALUES ('live', 'user', 'zebracorn sighting', 3.0)")
    con.commit()
    con.close()


def _marker(con):
    return con.execute(
        "SELECT json_extract(model_config, '$._delegate_from') FROM sessions WHERE id = 'live'").fetchone()[0]


def _trigram_indexed(con):
    return con.execute(
        "SELECT COUNT(*) FROM messages_fts_trigram_docsize d JOIN messages m ON m.id = d.id "
        "WHERE m.content = 'zebracorn sighting'").fetchone()[0]


def test_a_heal_that_fails_mid_way_rolls_back_and_is_retried_by_the_next_open(
        tmp_path, reconciles, monkeypatch, caplog):
    path = tmp_path / "state.db"
    _polluted_store_with_an_unindexed_message(path)
    _after_interval(monkeypatch)
    real_index = hermes_state_schema.SessionSchemaMixin._index_healed_rows_for_trigram

    def locked(self, cursor, ids):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(hermes_state_schema.SessionSchemaMixin, "_index_healed_rows_for_trigram", locked)
    with caplog.at_level(logging.WARNING, logger="hermes_state"):
        SessionDB(db_path=path).close()
    assert reconciles["n"] == 1
    assert any(r.levelno == logging.WARNING and "heal" in r.getMessage() for r in caplog.records)
    con = sqlite3.connect(path)
    try:
        # Rolled back whole: the marker is still there, so the retry finds the row again.
        assert _marker(con) == "old"
        assert _trigram_indexed(con) == 0
    finally:
        con.close()

    monkeypatch.setattr(hermes_state_schema.SessionSchemaMixin, "_index_healed_rows_for_trigram", real_index)
    SessionDB(db_path=path).close()  # same clock: the failed claim was released
    assert reconciles["n"] == 1
    con = sqlite3.connect(path)
    try:
        assert _marker(con) is None
        assert _trigram_indexed(con) == 1
    finally:
        con.close()
