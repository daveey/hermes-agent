"""Prepared cold capture ranks bounded candidates without rebuilding every message maximum."""
import sqlite3
import time

import pytest

import hermes_state_recency as recency
from hermes_state import SessionDB
from hermes_state_recency_build import prepare_batch
from tests.hermes_state import test_recency_hot_rows as helpers


def prepare(db):
    for _ in range(10_000):
        if prepare_batch(db):
            return
    pytest.fail('recency preparation did not finish')


def test_cold_30k_page_avoids_rebuild_and_finishes_in_capture_budget(tmp_path, monkeypatch):
    db = SessionDB(db_path=tmp_path / 'state.db')
    try:
        helpers._seed(db, 30_000, messages=10)
        prepare(db)
        recency.clear()
        monkeypatch.setattr(recency, '_rebuilt_maxima', lambda conn: pytest.fail('cold full-store maxima scan'))
        started = time.monotonic()
        actual = helpers._page(db, limit=50, include_pinned=True)
        elapsed = time.monotonic() - started
        assert elapsed < 5.0, elapsed
        assert len(actual) == 50
        assert actual == helpers._exact(db, monkeypatch, limit=50, include_pinned=True)
    finally:
        db.close()
        recency.clear()


def test_index_candidates_preserve_old_messages_chains_and_mutations(tmp_path, monkeypatch):
    db = SessionDB(db_path=tmp_path / 'state.db')
    try:
        helpers._seed(db, 200)
        prepare(db)
        helpers._write(db, 'UPDATE messages SET timestamp=? WHERE session_id=?', (helpers._BASE + 500, 's00000'))
        helpers._write(db, "UPDATE sessions SET end_reason='compression' WHERE id='s00001'")
        helpers._write(db, "UPDATE sessions SET parent_session_id='s00001' WHERE id='s00002'")
        helpers._write(db, 'UPDATE messages SET timestamp=? WHERE session_id=?', (helpers._BASE + 600, 's00002'))
        for sql, params in [('', ()), ('DELETE FROM messages WHERE session_id=?', ('s00002',)),
                            ('UPDATE messages SET timestamp=? WHERE session_id=?', (helpers._BASE - 1, 's00000'))]:
            if sql:
                helpers._write(db, sql, params)
            recency.clear()
            assert helpers._page(db, limit=50) == helpers._exact(db, monkeypatch, limit=50)
    finally:
        db.close()
        recency.clear()


def test_legacy_store_keeps_existing_fallback(tmp_path, monkeypatch):
    db = SessionDB(db_path=tmp_path / 'state.db')
    try:
        helpers._seed(db, 200)
        recency.clear()
        actual = helpers._page(db, limit=50)
        assert actual == helpers._exact(db, monkeypatch, limit=50)
        assert recency._store_for(str(db.db_path)).watermark is not None
    finally:
        db.close()
        recency.clear()


def test_index_is_ineligible_for_unmodified_timestamp_readers(tmp_path):
    db = SessionDB(db_path=tmp_path / 'state.db')
    try:
        prepare(db)
        with db._read_ctx() as conn:
            with pytest.raises(sqlite3.OperationalError, match='no such index'):
                conn.execute('SELECT id FROM messages INDEXED BY idx_message_recency_range WHERE timestamp >= ?', (0,))
    finally:
        db.close()


@pytest.mark.parametrize('value', [0.0, recency.EPOCH_MAX, recency.EPOCH_MAX + 1, -1.0, 'bad', float('inf')])
def test_index_timestamp_boundaries_and_live_changes_match_literal(tmp_path, monkeypatch, value):
    db = SessionDB(db_path=tmp_path / 'state.db')
    try:
        helpers._seed(db, 200)
        prepare(db)
        for sql, params in [
            ('UPDATE messages SET timestamp=? WHERE session_id=?', (value, 's00000')),
            ('UPDATE messages SET timestamp=? WHERE session_id=?', (helpers._BASE + 1000, 's00000')),
            ('DELETE FROM messages WHERE session_id=?', ('s00000',)),
        ]:
            helpers._write(db, sql, params)
            actual = helpers._page(db, limit=25, offset=25, include_pinned=True)
            assert actual == helpers._exact(db, monkeypatch, limit=25, offset=25, include_pinned=True)
        with db._read_ctx() as conn:
            plan = conn.execute(
                'EXPLAIN QUERY PLAN SELECT DISTINCT session_id FROM message_recency_candidates '
                'INDEXED BY idx_message_recency_range WHERE timestamp BETWEEN ? AND ?',
                (helpers._BASE, recency.EPOCH_MAX)).fetchall()
            assert any('SEARCH' in row[3] and 'idx_message_recency_range' in row[3] for row in plan)
    finally:
        db.close()
        recency.clear()
