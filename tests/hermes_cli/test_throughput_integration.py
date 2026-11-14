"""Throughput tracking across real schema rebuild, recovery and scope boundaries."""
import sqlite3

from hermes_state import SessionDB
from hermes_cli import machine_throughput_incremental as inc
from hermes_cli import machine_throughput_metrics as mt
from hermes_cli import session_throughput_revision as rev
from tests.hermes_cli import test_throughput_differential as helpers

store = helpers.store
assert_oracle = helpers.assert_oracle


def test_rebuild_usage_with_tracking_installed(store):
    from hermes_state_schema import SessionSchemaMixin
    path, conn = store
    assert_oracle(path, conn)
    old = inc._memos[str(path.resolve())]
    ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name='session_model_usage'").fetchone()[0]
    SessionSchemaMixin._rebuild_table_statements(
        conn.cursor(), 'session_model_usage', 'usage_old', ddl,
        'INSERT INTO session_model_usage SELECT * FROM usage_old', [])
    assert rev.read_stamp(conn) is None
    conn.execute("UPDATE session_model_usage SET input_tokens=3 WHERE session_id='a'")
    assert rev.install(conn)
    conn.commit()
    assert_oracle(path, conn)
    assert inc._memos[str(path.resolve())] is not old
    conn.execute("UPDATE session_model_usage SET input_tokens=17 WHERE session_id='b'")
    conn.commit()
    assert_oracle(path, conn)


def test_nullable_sql_pairs_preserve_null_propagation(store):
    from hermes_state_schema import SessionSchemaMixin
    path, conn = store
    ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name='session_model_usage'").fetchone()[0]
    for column in ('input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens'):
        ddl = ddl.replace(f'{column} INTEGER NOT NULL', f'{column} INTEGER')
    SessionSchemaMixin._rebuild_table_statements(
        conn.cursor(), 'session_model_usage', 'usage_old', ddl,
        'INSERT INTO session_model_usage SELECT * FROM usage_old', [])
    assert rev.install(conn)
    conn.execute("UPDATE session_model_usage SET input_tokens=NULL,output_tokens=999 WHERE session_id='a'")
    conn.commit()
    assert_oracle(path, conn)
    assert str(path.resolve()) in inc._memos
    conn.execute("UPDATE session_model_usage SET input_tokens=5,output_tokens=NULL,cache_read_tokens=NULL,cache_write_tokens=999 WHERE session_id='b'")
    conn.commit()
    assert_oracle(path, conn)


def test_recovery_rebuilds_protocol_instead_of_copying_history(store, tmp_path):
    from hermes_cli.session_recovery import recover_session_database
    path, conn = store
    before = rev.read_stamp(conn)
    output = tmp_path / 'recovered.db'
    report = recover_session_database(path, output)
    assert output.exists(), report
    with sqlite3.connect(output) as recovered:
        assert mt._full_read(recovered) == mt._full_read(conn)
        assert rev.read_stamp(recovered) is not None
        assert rev.read_stamp(recovered)[1] != before[1]
        assert_oracle(output, recovered)
        recovered.execute("UPDATE session_model_usage SET input_tokens=4 WHERE session_id='a'")
        recovered.commit()
        assert_oracle(output, recovered)
    assert rev.read_stamp(conn) == before


def test_alternating_homes_profiles_and_symlinks(store, tmp_path):
    path, conn = store
    root = tmp_path / 'other'
    profile = root / 'profiles' / 'worker'
    profile.mkdir(parents=True)
    other = profile / 'state.db'
    with sqlite3.connect(other) as target:
        conn.backup(target)
        target.execute('UPDATE session_model_usage SET input_tokens=1')
        target.commit()
    alias = root / 'profiles' / 'alias'
    alias.mkdir()
    (alias / 'state.db').symlink_to(other)
    first = mt.cached_counters(path.parent, clock=lambda: 100)
    second = mt.cached_counters(root, clock=lambda: 100)
    assert first['tokens_io_total'] == 60
    assert second['tokens_io_total'] == 3
    assert second['sessions_started_total'] == 3
    assert mt.cached_counters(path.parent, clock=lambda: 100) == first
    conn.execute('UPDATE session_model_usage SET input_tokens=2')
    conn.commit()
    assert mt.cached_counters(path.parent, clock=lambda: 160)['tokens_io_total'] == 6
    assert mt.cached_counters(root, clock=lambda: 160) == second
