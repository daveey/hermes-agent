"""Empty killed-notice proofs must avoid rescanning unchanged session tables."""
import sqlite3

from tui_gateway import kanban_unloaded_gates as gates


def test_empty_scan_reuses_revision_but_rechecks_writes(tmp_path):
    conn = sqlite3.connect(tmp_path / 'state.db')
    conn.execute('CREATE TABLE sessions(parent_session_id TEXT, end_reason TEXT, ended_at REAL)')
    conn.commit()
    calls = []
    conn.set_trace_callback(calls.append)
    try:
        assert gates._killed_empty(conn)
        assert gates._killed_empty(conn)
        assert sum('SELECT 1 FROM sessions' in q for q in calls) == 1
        conn.execute("INSERT INTO sessions VALUES ('parent', 'startup_orphan_reap', strftime('%s','now'))")
        conn.commit()
        assert not gates._killed_empty(conn)
        conn.execute('DELETE FROM sessions')
        conn.commit()
        assert gates._killed_empty(conn)
    finally:
        gates.reset_for_tests()
        conn.close()


def test_external_write_and_transactions_never_reuse_empty(tmp_path):
    path = tmp_path / 'state.db'
    conn = sqlite3.connect(path)
    writer = sqlite3.connect(path)
    conn.execute('CREATE TABLE sessions(parent_session_id TEXT, end_reason TEXT, ended_at REAL)')
    conn.commit()
    try:
        assert gates._killed_empty(conn)
        writer.execute("INSERT INTO sessions VALUES ('parent', 'startup_orphan_reap', strftime('%s','now'))")
        writer.commit()
        assert not gates._killed_empty(conn)
        conn.execute('DELETE FROM sessions')
        assert gates._killed_empty(conn)
        conn.rollback()
        assert not gates._killed_empty(conn)
    finally:
        gates.reset_for_tests()
        writer.close()
        conn.close()


def test_reset_clears_empty_proof(tmp_path):
    conn = sqlite3.connect(tmp_path / 'state.db')
    conn.execute('CREATE TABLE sessions(parent_session_id TEXT, end_reason TEXT, ended_at REAL)')
    conn.commit()
    calls = []
    conn.set_trace_callback(calls.append)
    try:
        assert gates._killed_empty(conn)
        gates.reset_for_tests()
        assert gates._killed_empty(conn)
        assert sum('SELECT 1 FROM sessions' in q for q in calls) == 2
    finally:
        gates.reset_for_tests()
        conn.close()


def _store(path):
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE sessions(parent_session_id TEXT, end_reason TEXT, ended_at REAL)')
    conn.commit()
    return conn


def test_positive_result_is_never_memoized(tmp_path):
    conn = _store(tmp_path / 'state.db')
    try:
        assert gates._killed_empty(conn)
        conn.execute("INSERT INTO sessions VALUES ('parent', 'startup_orphan_reap', strftime('%s','now'))")
        conn.commit()
        assert not gates._killed_empty(conn)
        assert not gates._killed_empty(conn)  # still positive, and no proof retained for it
        assert conn not in gates._killed_empty_keys
    finally:
        gates.reset_for_tests()
        conn.close()


def test_retained_connections_are_bounded_and_not_shared(tmp_path):
    conns = [_store(tmp_path / f'state{i}.db') for i in range(33)]
    try:
        for conn in conns[:32]:
            assert gates._killed_empty(conn)
        assert len(gates._killed_empty_keys) == 32
        assert gates._killed_empty(conns[32])  # bound reached: older proofs dropped
        assert len(gates._killed_empty_keys) == 1 and conns[32] in gates._killed_empty_keys
        a, b = conns[0], conns[1]
        gates.reset_for_tests()
        assert gates._killed_empty(a)
        b.execute("INSERT INTO sessions VALUES ('p', 'startup_orphan_reap', strftime('%s','now'))")
        b.commit()
        assert not gates._killed_empty(b)
    finally:
        gates.reset_for_tests()
        for conn in conns:
            conn.close()


def test_child_reaped_beyond_the_retry_window_is_not_work(tmp_path):
    """The consumer re-offers only children reaped inside RETRY_WINDOW_S: older rows (73 on the hub) kept the pass hot."""
    from tui_gateway.reap_notices import RETRY_WINDOW_S
    import time
    conn = _store(tmp_path / 'state.db')
    try:
        old = time.time() - RETRY_WINDOW_S - 60
        conn.execute("INSERT INTO sessions VALUES ('p', 'startup_orphan_reap', ?)", (old,))
        conn.commit()
        assert gates._killed_empty(conn)
        conn.execute("INSERT INTO sessions VALUES ('q', 'startup_orphan_reap', ?)", (time.time() - 60,))
        conn.commit()
        assert not gates._killed_empty(conn)
    finally:
        gates.reset_for_tests()
        conn.close()
