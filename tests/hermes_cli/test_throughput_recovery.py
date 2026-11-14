"""History, snapshot and interrupted-delta recovery of throughput totals."""
import sqlite3

import pytest

from hermes_cli import machine_throughput_incremental as inc
from hermes_cli import machine_throughput_metrics as mt
from hermes_cli import session_throughput_revision as rev
from tests.hermes_cli import test_throughput_differential as helpers

store = helpers.store
assert_oracle = helpers.assert_oracle


@pytest.mark.parametrize('extra', [0, 2])
def test_same_inode_backup_fork_at_equal_or_higher_revision(store, extra):
    path, conn = store
    fork = sqlite3.connect(':memory:')
    conn.backup(fork)
    conn.execute("UPDATE session_model_usage SET input_tokens=90 WHERE session_id='a'")
    conn.commit()
    assert_oracle(path, conn)
    old = inc._memos[str(path.resolve())]
    inode = path.stat().st_ino
    fork.execute("UPDATE session_model_usage SET input_tokens=7 WHERE session_id='b'")
    for _ in range(extra):
        fork.execute("UPDATE session_model_usage SET input_tokens=input_tokens+1 WHERE session_id='b'")
    fork.commit()
    fork.backup(conn)
    fork.close()
    # SQLite backup increments the destination schema cookie. Pin it back to
    # isolate history proof from the independent schema fence.
    conn.execute(f'PRAGMA schema_version={old.stamp[0]}')
    assert path.stat().st_ino == inode
    assert rev.read_stamp(conn)[2] >= old.stamp[2]
    assert rev.read_stamp(conn)[:2] == old.stamp[:2]
    assert_oracle(path, conn)
    assert inc._memos[str(path.resolve())] is not old


def test_ring_wrap_rebuilds_even_when_no_keys_were_lost(store, monkeypatch):
    path, conn = store
    monkeypatch.setattr(rev, 'RING_SIZE', 4)
    monkeypatch.setattr(rev, '_TRIGGERS', rev._build_triggers())
    assert rev.install(conn)
    # Re-stamp into the newly small ring (the original fixture used 65536).
    conn.execute("UPDATE session_model_usage SET input_tokens=input_tokens WHERE session_id='a'")
    conn.commit()
    assert_oracle(path, conn)
    old = inc._memos[str(path.resolve())]
    for _ in range(4):
        conn.execute("UPDATE session_model_usage SET input_tokens=input_tokens+1 WHERE session_id='a'")
    conn.commit()
    assert_oracle(path, conn)
    assert inc._memos[str(path.resolve())] is not old


def test_partial_delta_exception_discards_memo_before_retry(store, monkeypatch):
    path, conn = store
    assert_oracle(path, conn)
    conn.execute('UPDATE session_model_usage SET input_tokens=8')
    conn.commit()
    original = inc._load
    calls = []

    def fail_second(db, key=None):
        calls.append(key)
        if len(calls) == 2:
            raise sqlite3.OperationalError('injected read failure')
        return original(db, key)

    with monkeypatch.context() as scoped:
        scoped.setattr(inc, '_load', fail_second)
        assert mt.counters(path.parent) == {}
    assert len(calls) == 2
    assert str(path.resolve()) not in inc._memos
    assert_oracle(path, conn)


def test_writer_commit_between_stamp_and_delta_keeps_one_snapshot(store, monkeypatch):
    path, conn = store
    conn.execute('PRAGMA journal_mode=WAL')
    assert_oracle(path, conn)
    conn.execute("UPDATE session_model_usage SET input_tokens=8 WHERE session_id='a'")
    conn.commit()
    expected = mt._full_read(conn)
    original = rev.read_stamp

    def commit_after_stamp(db):
        stamp = original(db)
        conn.execute("UPDATE session_model_usage SET input_tokens=99 WHERE session_id='a'")
        conn.commit()
        return stamp

    with monkeypatch.context() as scoped:
        scoped.setattr(rev, 'read_stamp', commit_after_stamp)
        result = mt._read(path)
        assert result == (sum(r[1] for r in expected[0]), sum(r[2] for r in expected[0]), *expected[1:])
    assert_oracle(path, conn)


def test_inode_replacement_during_read_rejects_result(store, monkeypatch, tmp_path):
    path, conn = store
    assert_oracle(path, conn)
    replacement = tmp_path / 'replacement.db'
    target = sqlite3.connect(replacement)
    conn.backup(target)
    target.close()
    original = inc.read

    def replace_after_read(db, key, identity):
        result = original(db, key, identity)
        replacement.replace(path)
        return result

    with monkeypatch.context() as scoped:
        scoped.setattr(inc, 'read', replace_after_read)
        assert mt.counters(path.parent) == {}
    assert str(path.resolve()) not in inc._memos
    assert_oracle(path, conn)


def test_path_cap_prune_and_reset(store, monkeypatch, tmp_path):
    path, conn = store
    monkeypatch.setattr(inc, 'MAX_PATHS', 2)
    for i in range(3):
        other = tmp_path / f'copy{i}.db'
        target = sqlite3.connect(other)
        conn.backup(target)
        target.close()
        assert_oracle(other, conn)
        assert len(inc._memos) <= 2
    assert str((tmp_path / 'copy0.db').resolve()) not in inc._memos
    mt.counters(path.parent)
    assert set(inc._memos) == {str(path.resolve())}
    mt.cached_counters(path.parent)
    assert mt._cache is not None
    mt.reset_cache()
    assert not inc._memos and mt._cache is None


@pytest.mark.parametrize('bound,limit,sql', [
    ('MAX_IDS', 3, "INSERT INTO sessions(id,source,started_at) VALUES ('d','test',1)"),
    ('MAX_MODELS', 2, "INSERT INTO session_model_usage(session_id,model,input_tokens) VALUES ('a','new',1)"),
    ('MAX_ROWS', 3, "INSERT INTO session_model_usage(session_id,model,input_tokens,task) VALUES ('a','m',1,'aux')"),
])
def test_delta_global_caps_discard_then_recover(store, monkeypatch, bound, limit, sql):
    path, conn = store
    monkeypatch.setattr(inc, bound, limit)
    assert_oracle(path, conn)
    assert str(path.resolve()) in inc._memos
    conn.execute(sql)
    conn.commit()
    assert_oracle(path, conn)
    assert str(path.resolve()) not in inc._memos
    monkeypatch.setattr(inc, bound, 100)
    assert_oracle(path, conn)
    assert str(path.resolve()) in inc._memos


@pytest.mark.parametrize('guard', ['identity', 'schema', 'epoch', 'floor', 'regression'])
def test_each_cursor_fence_rebuilds_independently(store, guard):
    path, conn = store
    assert_oracle(path, conn)
    old = inc._memos[str(path.resolve())]
    statements = {
        'schema': ['CREATE TABLE unrelated_schema_change (id INTEGER)'],
        'epoch': ['UPDATE throughput_revision SET epoch=randomblob(16)'],
        'floor': ["UPDATE session_model_usage SET input_tokens=8 WHERE session_id='a'",
                  'UPDATE throughput_revision SET floor=rev,keys=0',
                  'DELETE FROM throughput_changes'],
    }
    for statement in statements.get(guard, []):
        conn.execute(statement)
    if guard == 'identity':
        old.identity = (old.identity[0], old.identity[1] + 1)
    if guard == 'regression':
        # Keep history evidence valid while isolating the counter regression.
        current = rev.read_stamp(conn)
        old.stamp = (*old.stamp[:2], current[2] + 1, old.stamp[3], old.stamp[4])
        conn.execute('INSERT OR REPLACE INTO throughput_anchors(slot,rev,nonce) VALUES (?,?,?)',
                     ((current[2]+1) % rev.RING_SIZE, current[2]+1, old.stamp[3]))
    conn.commit()
    assert_oracle(path, conn)
    assert inc._memos[str(path.resolve())] is not old


def test_sql_int64_sum_overflow_omits_whole_host_then_recovers(store):
    path, conn = store
    assert_oracle(path, conn)
    conn.execute("UPDATE session_model_usage SET input_tokens=? WHERE model='m'", ((1 << 63) - 1,))
    conn.commit()
    with pytest.raises(sqlite3.OperationalError, match='integer overflow'):
        mt._full_read(conn)
    assert mt.counters(path.parent) == {}
    assert str(path.resolve()) not in inc._memos
    conn.execute('UPDATE session_model_usage SET input_tokens=1')
    conn.commit()
    assert_oracle(path, conn)
