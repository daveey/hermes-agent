"""Lineage enumeration must stay bounded when unrelated writes invalidate its cache."""
import json

from hermes_state import SessionDB
from hermes_state_local import POLICY_PREFIX, _cached_local_lineage_index, local_lineage_index


def test_unrelated_writes_do_not_rescan_single_segment_receipts(tmp_path):
    db = SessionDB(db_path=tmp_path / 'state.db')
    try:
        conn = db._conn
        assert conn is not None
        raw = json.dumps({'session_id': 'single', 'lineage': ['single'], 'policy': 'x' * 12000})
        conn.executemany('INSERT INTO state_meta VALUES (?,?)',
                         [(POLICY_PREFIX + str(i), raw) for i in range(1000)])
        conn.commit()
        with db._read_ctx() as reader:
            assert _cached_local_lineage_index(db, reader) == (set(), {})
            for i in range(3):
                conn.execute('INSERT OR REPLACE INTO state_meta VALUES (?,?)', ('unrelated', str(i)))
                conn.commit()
                steps = []
                reader.set_progress_handler(lambda: steps.append(1) or 0, 100)
                try:
                    assert _cached_local_lineage_index(db, reader) == (set(), {})
                finally:
                    reader.set_progress_handler(None, 0)
                # Bound executed work, not wall-clock time on a busy CI runner.
                assert len(steps) < 10, len(steps)
    finally:
        db.close()


def test_index_upgrade_and_receipt_mutations_preserve_projection(tmp_path):
    path = tmp_path / 'state.db'
    db = SessionDB(db_path=path)
    assert db._conn is not None
    db._conn.execute('DROP INDEX IF EXISTS idx_state_meta_local_lineage')
    db.close()
    db = SessionDB(db_path=path)
    try:
        conn = db._conn
        assert conn is not None
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='idx_state_meta_local_lineage'").fetchone()
        db.create_session('owner', source='cli')
        db.create_session('head', source='cli')
        conn.execute("UPDATE sessions SET end_reason='session_reset' WHERE id='owner'")
        odd_values = ['malformed', '[]', '{}', 'null', '{"lineage":"text"}',
                      '{"lineage":{}}', '{"lineage":["owner",1]}']
        conn.executemany('INSERT INTO state_meta VALUES (?,?)',
                         [(POLICY_PREFIX + str(i), raw) for i, raw in enumerate(odd_values)])
        key = POLICY_PREFIX + 'owner'
        conn.execute('INSERT INTO state_meta VALUES (?,?)', (key, json.dumps(
            {'session_id': 'owner', 'lineage': ['owner', 'head']})))
        conn.commit()
        expected = ({'owner'}, {'head': ('owner', ['owner', 'head'])})
        assert local_lineage_index(conn) == expected
        conn.execute('BEGIN')
        conn.execute('UPDATE state_meta SET value=? WHERE key=?', ('broken', key))
        assert local_lineage_index(conn) == (set(), {})
        conn.rollback()
        assert local_lineage_index(conn) == expected
        conn.execute('BEGIN')
        conn.execute('DELETE FROM state_meta WHERE key=?', (key,))
        assert local_lineage_index(conn) == (set(), {})
        conn.rollback()
        conn.execute('DROP INDEX idx_state_meta_local_lineage')
        assert local_lineage_index(conn) == expected  # legacy/read-only fallback
    finally:
        db.close()
