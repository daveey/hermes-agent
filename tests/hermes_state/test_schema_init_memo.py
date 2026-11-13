"""[daveey] A settled store's schema init runs once per process; later opens restore its flags
(design t_01a1d45d75851804 §2d: ``_init_schema`` was 6.9% of a steady serve's samples)."""

from __future__ import annotations

import os
import shutil
import sqlite3

import pytest

import hermes_state_schema
from hermes_state import SessionDB


@pytest.fixture(autouse=True)
def fresh_memo():
    try:
        import hermes_state_schema_memo
    except ImportError:  # the pre-memo build: every open runs the full init
        yield
        return
    hermes_state_schema_memo.clear()
    yield
    hermes_state_schema_memo.clear()


@pytest.fixture
def counts(monkeypatch):
    """Calls of the column reconcile (a full-init step) and of the delegate-marker heal."""
    seen = {"reconcile": 0, "heal": 0}
    reconcile = hermes_state_schema.SessionSchemaMixin._reconcile_columns
    heal = hermes_state_schema.SessionSchemaMixin._heal_reset_rows_carrying_a_delegate_marker

    def spy_reconcile(self, cursor):
        seen["reconcile"] += 1
        return reconcile(self, cursor)

    def spy_heal(self, cursor):
        seen["heal"] += 1
        return heal(self, cursor)

    monkeypatch.setattr(hermes_state_schema.SessionSchemaMixin, "_reconcile_columns", spy_reconcile)
    monkeypatch.setattr(hermes_state_schema.SessionSchemaMixin,
                        "_heal_reset_rows_carrying_a_delegate_marker", spy_heal)
    return seen


def _open_close(path):
    db = SessionDB(db_path=path)
    flags = (db._fts_enabled, db._trigram_available, db._fts_stale)
    db.close()
    return flags


def test_a_second_open_of_a_settled_store_skips_the_schema_init(tmp_path, counts):
    path = tmp_path / "state.db"
    _open_close(path)
    _open_close(path)
    _open_close(path)
    assert counts["reconcile"] == 1


def test_a_skipped_init_restores_the_flags_the_first_init_set(tmp_path, counts):
    path = tmp_path / "state.db"
    first = _open_close(path)
    db = SessionDB(db_path=path)
    try:
        assert counts["reconcile"] == 1
        assert (db._fts_enabled, db._trigram_available, db._fts_stale) == first
        assert db._fts_enabled is True  # restored, not the constructor default
        db.create_session("s1", "cli")
        db.append_message("s1", "user", "needle in the haystack")
        assert [r["session_id"] for r in db.search_messages("needle")] == ["s1"]
    finally:
        db.close()


def test_a_runtime_without_the_trigram_index_does_not_inherit_the_flag(tmp_path, counts, monkeypatch):
    path = tmp_path / "state.db"
    assert _open_close(path)[1] is True
    real_probe = hermes_state_schema.SessionSchemaMixin._fts_table_probe

    def no_trigram(self, cursor, table_name):
        return None if table_name == "messages_fts_trigram" else real_probe(self, cursor, table_name)

    monkeypatch.setattr(hermes_state_schema.SessionSchemaMixin, "_fts_table_probe", no_trigram)
    db = SessionDB(db_path=path)
    try:
        assert counts["reconcile"] == 2  # the probe disagreed with the memo: full init
    finally:
        db.close()


def test_a_replaced_file_reruns_the_init(tmp_path, counts):
    path = tmp_path / "state.db"
    _open_close(path)
    copy = tmp_path / "state.db.new"
    shutil.copy2(path, copy)
    old_inode = os.stat(path).st_ino
    os.replace(copy, path)
    assert os.stat(path).st_ino != old_inode
    _open_close(path)
    assert counts["reconcile"] == 2


def test_ddl_by_another_process_reruns_the_init(tmp_path, counts):
    path = tmp_path / "state.db"
    _open_close(path)
    con = sqlite3.connect(path)  # e.g. an older or newer build's migration
    con.execute("ALTER TABLE sessions ADD COLUMN external_note TEXT")
    con.commit()
    con.close()
    _open_close(path)
    assert counts["reconcile"] == 2


def test_a_downgraded_data_version_reruns_the_migrations(tmp_path, counts):
    path = tmp_path / "state.db"
    _open_close(path)
    con = sqlite3.connect(path)  # schema_version (the table) is data, not DDL
    con.execute("UPDATE schema_version SET version = version - 1")
    con.commit()
    con.close()
    _open_close(path)
    assert counts["reconcile"] == 2
    con = sqlite3.connect(path)
    try:
        assert con.execute("SELECT version FROM schema_version").fetchone()[0] == hermes_state_schema.SCHEMA_VERSION
    finally:
        con.close()


def test_fts_stale_set_by_another_handle_is_seen_on_the_next_open(tmp_path, counts):
    path = tmp_path / "state.db"
    _open_close(path)
    other = SessionDB(db_path=path)
    try:
        other.set_meta(hermes_state_schema.FTS_STALE_KEY, "1")
    finally:
        other.close()
    db = SessionDB(db_path=path)
    try:
        # Seen: the open took the full init, whose stale-FTS recovery rebuilt the index and cleared the mark.
        assert counts["reconcile"] == 2
        assert db._fts_stale is False and db._fts_enabled is True
        assert db.get_meta(hermes_state_schema.FTS_STALE_KEY) is None
    finally:
        db.close()


def test_the_delegate_marker_heal_runs_at_most_once_per_interval(tmp_path, counts, monkeypatch):
    import hermes_state_schema_memo as memo

    path = tmp_path / "state.db"
    for _ in range(4):
        _open_close(path)
    assert counts["heal"] == 1
    now = memo.time.monotonic()
    monkeypatch.setattr(memo.time, "monotonic", lambda: now + memo.HEAL_INTERVAL_S + 1)
    _open_close(path)
    _open_close(path)
    assert counts["heal"] == 2
    assert counts["reconcile"] == 1


def test_the_throttled_heal_repairs_a_polluted_row(tmp_path, monkeypatch):
    import hermes_state_schema_memo as memo

    path = tmp_path / "state.db"
    db = SessionDB(db_path=path)
    db.create_session("old", "telegram", session_key="k", chat_id="c", chat_type="dm", user_id="u")
    db.create_session("live", "telegram", parent_session_id="old", model_config={"_reset_from": "old"},
                      session_key="k", chat_id="c", chat_type="dm", user_id="u")
    db.close()
    con = sqlite3.connect(path)
    con.execute("UPDATE sessions SET model_config = json_set(model_config, '$._delegate_from', 'old') "
                "WHERE id = 'live'")
    con.commit()
    con.close()
    now = memo.time.monotonic()
    monkeypatch.setattr(memo.time, "monotonic", lambda: now + memo.HEAL_INTERVAL_S + 1)
    con = sqlite3.connect(path)
    try:
        db = SessionDB(db_path=path)
        db.close()
        cfg = con.execute("SELECT json_extract(model_config, '$._delegate_from') FROM sessions WHERE id='live'")
        assert cfg.fetchone()[0] is None
    finally:
        con.close()
