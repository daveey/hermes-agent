"""Compare refreshed incremental results with the unchanged SQL oracle."""
import sqlite3

import pytest

from hermes_cli import machine_throughput_metrics as mt
from hermes_cli import machine_throughput_incremental as inc
from hermes_cli import session_throughput_revision as rev
from hermes_state import SessionDB


@pytest.fixture
def store(tmp_path):
    path = tmp_path / 'state.db'
    db = SessionDB(db_path=path)
    db.close()
    conn = sqlite3.connect(path)
    conn.executemany("INSERT INTO sessions(id,source,started_at,title) VALUES (?,'test',1,?)",
                     [('a', 'A'), ('b', 'B'), ('c', 'C')])
    conn.executemany('INSERT INTO session_model_usage(session_id,model,input_tokens) VALUES (?,?,?)',
                     [('a', 'm', 10), ('b', 'm', 20), ('c', 'n', 30)])
    conn.commit()
    mt.reset_cache()
    yield path, conn
    conn.close()
    mt.reset_cache()


def assert_oracle(path, conn):
    rows, started, ended = mt._full_read(conn)
    expected = {}
    for model, io, cache in rows:
        values = expected.setdefault(str(model or 'unknown')[:64], [0, 0])
        values[0] += int(io)
        values[1] += int(cache)
    actual = {}
    counts = mt._read(path, actual)
    assert actual == expected
    assert counts == (sum(int(r[1]) for r in rows), sum(int(r[2]) for r in rows), started, ended)


@pytest.mark.parametrize('sql', [
    "INSERT OR REPLACE INTO sessions(rowid,id,source,started_at) VALUES ((SELECT rowid FROM sessions WHERE id='b'),'new','test',1)",
    "INSERT OR REPLACE INTO session_model_usage(rowid,session_id,model,input_tokens) VALUES ((SELECT rowid FROM session_model_usage WHERE session_id='b'),'orphan','z',9)",
    "UPDATE session_model_usage SET input_tokens=2 WHERE session_id='a'",
    "UPDATE session_model_usage SET input_tokens=222 WHERE session_id='a'",
    "UPDATE session_model_usage SET input_tokens=-2 WHERE session_id='a'",
    "UPDATE session_model_usage SET input_tokens=2.5 WHERE session_id='a'",
    "UPDATE session_model_usage SET input_tokens='abc' WHERE session_id='a'",
    "UPDATE session_model_usage SET input_tokens=X'01' WHERE session_id='a'",
    "UPDATE session_model_usage SET model='n' WHERE session_id='a'",
    "UPDATE session_model_usage SET model=X'6d' WHERE session_id='a'",
    "UPDATE session_model_usage SET session_id='orphan' WHERE session_id='a'",
    "INSERT INTO session_model_usage(session_id,model,input_tokens,task) VALUES ('a','m',7,'aux')",
    "DELETE FROM session_model_usage WHERE session_id='a'",
    "DELETE FROM sessions WHERE id='a'",
    "UPDATE sessions SET ended_at=5 WHERE id='a'",
    "UPDATE sessions SET id='renamed' WHERE id='a'",
    "INSERT OR REPLACE INTO sessions(id,source,started_at,title) VALUES ('a','test',1,'B')",
    "UPDATE OR REPLACE sessions SET title='B' WHERE id='a'",
    "UPDATE OR REPLACE sessions SET id='b' WHERE id='a'",
    "UPDATE OR REPLACE sessions SET rowid=(SELECT rowid FROM sessions WHERE id='b') WHERE id='a'",
    "UPDATE OR REPLACE sessions SET _rowid_=(SELECT rowid FROM sessions WHERE id='b') WHERE id='a'",
    "UPDATE OR REPLACE sessions SET oid=(SELECT rowid FROM sessions WHERE id='b') WHERE id='a'",
    "UPDATE OR REPLACE session_model_usage SET rowid=(SELECT rowid FROM session_model_usage WHERE session_id='b') WHERE session_id='a'",
])
@pytest.mark.parametrize('recursive', [0, 1])
def test_mutations_match_original_sql(store, sql, recursive):
    path, conn = store
    conn.execute(f'PRAGMA recursive_triggers={recursive}')
    assert_oracle(path, conn)
    assert str(path.resolve()) in inc._memos
    conn.execute(sql)
    conn.commit()
    assert_oracle(path, conn)
    # Recovery after an opaque/full-only result must rebuild, not apply a delta
    # to untracked totals. It must also see old-row, not just append, changes.
    conn.execute('UPDATE session_model_usage SET input_tokens=42')
    conn.commit()
    assert_oracle(path, conn)


@pytest.mark.parametrize('foreign_keys', [0, 1])
@pytest.mark.parametrize('recursive', [0, 1])
def test_replace_cascade_and_orphan_modes(store, foreign_keys, recursive):
    path, conn = store
    conn.execute(f'PRAGMA foreign_keys={foreign_keys}')
    conn.execute(f'PRAGMA recursive_triggers={recursive}')
    assert_oracle(path, conn)
    conn.execute("INSERT OR REPLACE INTO sessions(id,source,started_at,title) VALUES ('a','test',1,'B')")
    conn.commit()
    assert_oracle(path, conn)


@pytest.mark.parametrize('recursive', [0, 1])
@pytest.mark.parametrize('foreign_keys', [0, 1])
@pytest.mark.parametrize('sql', [
    "UPDATE OR REPLACE sessions SET id='b' WHERE id='a'",
    "UPDATE OR REPLACE sessions SET rowid=(SELECT rowid FROM sessions WHERE id='b') WHERE id='a'",
    "UPDATE OR REPLACE sessions SET id='b',title='C' WHERE id='a'",
    "UPDATE OR REPLACE session_model_usage SET rowid=(SELECT rowid FROM session_model_usage WHERE session_id='b') WHERE session_id='a'",
])
def test_conflict_matrix_and_statement_rollback(store, recursive, foreign_keys, sql):
    path, conn = store
    conn.execute(f'PRAGMA foreign_keys={foreign_keys}')
    conn.execute(f'PRAGMA recursive_triggers={recursive}')
    assert_oracle(path, conn)
    before = {name: conn.execute(f'SELECT * FROM {name}').fetchall()
              for name in ('sessions', 'session_model_usage', *rev._TABLE_SQL)}
    try:
        conn.execute(sql)
    except sqlite3.IntegrityError:
        assert foreign_keys
        assert {name: conn.execute(f'SELECT * FROM {name}').fetchall()
                for name in before} == before
        conn.rollback()
    else:
        conn.commit()
    assert_oracle(path, conn)


def test_cap_rotation_preserves_oracle_and_existing_key(store, monkeypatch):
    path, conn = store
    monkeypatch.setattr(rev, 'KEY_CAP', 3)
    monkeypatch.setattr(rev, '_TRIGGERS', rev._build_triggers())
    assert rev.install(conn)
    conn.commit()
    for key in ('a', 'b', 'c'):
        conn.execute('UPDATE session_model_usage SET input_tokens=input_tokens+1 WHERE session_id=?', (key,))
    conn.commit()
    assert conn.execute('SELECT keys FROM throughput_revision').fetchone()[0] == 3
    assert_oracle(path, conn)
    epoch = rev.read_stamp(conn)[1]
    conn.execute("UPDATE session_model_usage SET input_tokens=input_tokens+1 WHERE session_id='a'")
    conn.commit()
    assert rev.read_stamp(conn)[1] == epoch
    assert_oracle(path, conn)
    conn.execute("INSERT INTO sessions(id,source,started_at) VALUES ('new','test',1)")
    conn.commit()
    assert rev.read_stamp(conn)[1] != epoch
    assert conn.execute('SELECT keys FROM throughput_revision').fetchone()[0] == 1
    assert_oracle(path, conn)


def test_uncommitted_and_rollback_do_not_publish(store):
    path, conn = store
    before = mt._read(path)
    conn.execute("UPDATE session_model_usage SET input_tokens=88 WHERE session_id='a'")
    assert mt._read(path) == before
    conn.rollback()
    assert_oracle(path, conn)
    conn.execute("UPDATE sessions SET ended_at=5 WHERE id='a'")
    conn.commit()
    assert_oracle(path, conn)
    conn.execute("UPDATE sessions SET ended_at=NULL WHERE id='a'")
    conn.commit()
    assert_oracle(path, conn)


def test_zero_groups_and_normalization_collisions(store):
    path, conn = store
    conn.executemany('INSERT INTO session_model_usage(session_id,model,input_tokens) VALUES (?,?,?)',
                     [('a', f'zero{i}', 0) for i in range(40)] +
                     [('a', 'z' * 64 + suffix, 3) for suffix in ('x', 'y')])
    conn.commit()
    assert_oracle(path, conn)
    conn.execute("DELETE FROM session_model_usage WHERE model='zero0'")
    conn.commit()
    assert_oracle(path, conn)


def test_trigger_gap_and_repair_rotates_cursor(store):
    path, conn = store
    assert_oracle(path, conn)
    epoch = rev.read_stamp(conn)[1]
    conn.execute('DROP TRIGGER throughput_usage_au')
    conn.execute('UPDATE session_model_usage SET input_tokens=9')
    conn.commit()
    assert_oracle(path, conn)
    assert str(path.resolve()) not in inc._memos
    assert rev.install(conn)
    conn.commit()
    assert rev.read_stamp(conn)[1] != epoch
    assert_oracle(path, conn)
    conn.execute('UPDATE session_model_usage SET input_tokens=11')
    conn.commit()
    assert_oracle(path, conn)


@pytest.mark.parametrize('sql', [
    "UPDATE session_model_usage SET input_tokens=2.5 WHERE session_id='a'",
    "UPDATE session_model_usage SET input_tokens=-1 WHERE session_id='a'",
    "UPDATE session_model_usage SET input_tokens=9223372036854775807,output_tokens=1 WHERE session_id='a'",
    "UPDATE session_model_usage SET cache_read_tokens=9223372036854775807,cache_write_tokens=1 WHERE session_id='a'",
    "INSERT INTO sessions(id,source,started_at) VALUES (X'6162','test',1)",
    "INSERT INTO sessions(id,source,started_at) VALUES (NULL,'test',1)",
    "INSERT INTO session_model_usage(session_id,model,input_tokens) VALUES (X'6162','m',7)",
])
def test_unsupported_pairs_and_keys_drop_memo(store, sql):
    path, conn = store
    assert_oracle(path, conn)
    conn.execute(sql)
    conn.commit()
    assert_oracle(path, conn)
    assert str(path.resolve()) not in inc._memos
    with pytest.raises(inc.FullOnly):
        inc._load(conn)
    mt.reset_cache()
    assert_oracle(path, conn)
    assert str(path.resolve()) not in inc._memos


def test_initial_model_bound_is_enforced_during_loading(store, monkeypatch):
    _path, conn = store
    monkeypatch.setattr(inc, 'MAX_MODELS', 1)
    with pytest.raises(inc.FullOnly, match='model bound'):
        inc._load(conn)


@pytest.mark.parametrize('bound,value', [('MAX_IDS', 2), ('MAX_MODELS', 1), ('MAX_ROWS', 2)])
def test_memory_overflow_is_full_only_then_recovers(store, monkeypatch, bound, value):
    path, conn = store
    with monkeypatch.context() as scoped:
        scoped.setattr(inc, bound, value)
        assert_oracle(path, conn)
        assert str(path.resolve()) not in inc._memos
    assert_oracle(path, conn)
    assert str(path.resolve()) in inc._memos
