"""Direct loading boundaries and ordinary writer events without fallback masking."""
import sqlite3

import pytest

from hermes_cli import machine_throughput_incremental as inc
from hermes_cli import machine_throughput_metrics as mt
from hermes_cli import session_throughput_revision as rev
from hermes_state import SessionDB


@pytest.fixture
def store(tmp_path):
    path = tmp_path / 'state.db'
    db = SessionDB(db_path=path)
    db.close()
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO sessions(id,source,started_at) VALUES ('a','test',1)")
    conn.executemany("INSERT INTO session_model_usage(session_id,model,input_tokens,task) VALUES ('a','m',1,?)", [('one',), ('two',)])
    conn.commit()
    mt.reset_cache()
    yield path, conn
    conn.close()
    mt.reset_cache()


def test_loading_row_bound_precedes_aggregate_bound(store, monkeypatch):
    _path, conn = store
    monkeypatch.setattr(inc, 'MAX_ROWS', 1)
    with pytest.raises(inc.FullOnly, match='^usage shape or bound$'):
        inc._load(conn)


@pytest.mark.parametrize('sql,expected', [
    ("INSERT INTO sessions(id,source,started_at) VALUES ('new','test',1)", (2, 0, 2, 0)),
    ("DELETE FROM sessions WHERE id='a'", (2, 0, 0, 0)),
    ("UPDATE sessions SET ended_at=2 WHERE id='a'", (2, 0, 1, 1)),
    ("INSERT INTO session_model_usage(session_id,model,input_tokens,task) VALUES ('a','m',4,'new')", (6, 0, 1, 0)),
    ("DELETE FROM session_model_usage WHERE task='one'", (1, 0, 1, 0)),
    ("UPDATE session_model_usage SET input_tokens=3 WHERE task='one'", (4, 0, 1, 0)),
])
def test_each_ordinary_event_advances_and_reloads(store, sql, expected):
    path, conn = store
    assert mt._read(path) == (2, 0, 1, 0)
    before = rev.read_stamp(conn)
    conn.execute(sql)
    conn.commit()
    after = rev.read_stamp(conn)
    # A conflict-rotation/schema fallback must not mask a missing event stamp.
    assert before[:2] == after[:2]
    assert after[2] > before[2]
    assert mt._read(path) == expected
    assert str(path.resolve()) in inc._memos
