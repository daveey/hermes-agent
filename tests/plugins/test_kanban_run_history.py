"""Run-history retention keeps excluded bookkeeping exact, even for old grants."""
import sqlite3

import pytest

from hermes_cli import kanban_diagnostics as kd
from plugins.kanban.dashboard import board_read as br
from tests.plugins import test_kanban_board_incremental as helpers

board = helpers.board


@pytest.mark.parametrize('column', ['claim_lock', 'worker_pid', 'worker_started_at', 'last_heartbeat_at'])
def test_bookkeeping_only_run_update_matches_exact_board(board, monkeypatch, column):
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_task_change as ktc
    monkeypatch.setattr(kd.time, 'time', lambda: 9100.0)
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title='old grant', assignee='worker')
        conn.execute("UPDATE tasks SET status='ready', created_at=1 WHERE id=?", (tid,))
        conn.execute('DELETE FROM task_events WHERE task_id=?', (tid,))
        rid = conn.execute("INSERT INTO task_runs (task_id,profile,status,outcome,started_at,ended_at,metadata) "
                           "VALUES (?,'worker','released','released',9000,9001,?)", (tid, '{"unused_grant":true}')).lastrowid
        conn.executemany('INSERT INTO task_events (task_id,run_id,kind,payload,created_at) VALUES (?,?,?,?,?)', [
            (tid, None, 'created', '{}', 1), (tid, rid, 'claimed', '{}', 9000),
            (tid, rid, 'released', '{"unused_grant":true}', 9001)])
        conn.commit()
        baseline = helpers._stable(helpers._read(board))
        _, cursor = ktc.changed_since(conn, None)
        assert cursor is not None
        for value in (0, None):
            conn.execute(f'UPDATE task_runs SET {column}=? WHERE id=?', (value, rid))
            conn.commit()
            changed, cursor = ktc.changed_since(conn, cursor)
            assert changed == frozenset(), 'fixture must exercise excluded bookkeeping'
            actual = helpers._stable(helpers._read(board))
            assert actual == helpers._cold(board)
            assert (actual == baseline) == (value is None)
    finally:
        conn.close()



@pytest.fixture
def source(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript('''
        CREATE TABLE tasks (id TEXT, status TEXT, created_at INTEGER, assignee TEXT);
        INSERT INTO tasks VALUES ('t', 'ready', 1, 'worker');
        CREATE TABLE task_events (id INTEGER, task_id TEXT, run_id INTEGER, kind TEXT, payload TEXT, created_at INTEGER);
        INSERT INTO task_events VALUES (1,'t',NULL,'created','{}',1);
        INSERT INTO task_events VALUES (2,'t',7,'claimed','{}',9000);
        INSERT INTO task_events VALUES (3,'t',7,'released','{"unused_grant":true}',9001);
        CREATE TABLE task_runs (id INTEGER, task_id TEXT, status TEXT, outcome TEXT, ended_at INTEGER,
          metadata TEXT, summary TEXT, error TEXT, claim_lock, claim_expires, last_heartbeat_at,
          worker_pid, worker_started_at, sync_version);
        INSERT INTO task_runs (id,task_id,status,outcome,ended_at,metadata)
          VALUES (7,'t','released','released',9001,'{"unused_grant":true}');
    ''')
    monkeypatch.setattr(br, '_wake_states', lambda *_: {})
    yield conn
    conn.close()


def inputs(conn):
    rows = conn.execute('SELECT * FROM tasks').fetchall()
    events = br._rows_by_task(conn, 'task_events', ['t'])
    runs = br._rows_by_task(conn, 'task_runs', ['t'])
    return rows, events, runs


def memo(conn):
    rows, events, runs = inputs(conn)
    return br._diag_memo(rows, events, runs, br._event_marks(conn, ['t']))


def refresh(conn, old):
    return br._incremental_diag_inputs(conn, conn.execute('SELECT * FROM tasks').fetchall(), old,
                                      lambda *_args, **_kwargs: pytest.fail('unchanged events reloaded'),
                                      graphs={}, event_changed=frozenset())


def test_unchanged_run_histories_are_not_loaded_again(source, monkeypatch):
    old = memo(source)
    def unexpected(*_):
        pytest.fail('full run histories must not be loaded for an empty trusted delta')
    monkeypatch.setattr(br, '_rows_by_task', unexpected)
    got, new = refresh(source, old)
    assert got[2]['t'][0] is old['t'][4][0]
    assert new['t'][4][0] is old['t'][4][0]


@pytest.mark.parametrize('column', ['claim_lock', 'worker_pid', 'worker_started_at', 'last_heartbeat_at'])
@pytest.mark.parametrize('value', [0, '', 99])
def test_old_grant_bookkeeping_is_fresh_and_changes_visible_diagnostics(source, column, value):
    old = memo(source)
    task = dict(source.execute('SELECT * FROM tasks').fetchone())
    _rows, events, runs = inputs(source)
    baseline = kd.compute_task_diagnostics(task, events['t'], list(runs['t']), now=9100)
    assert kd._last_ready_entry_ts(events['t'], runs['t']) == 1
    for updated in (value, None):
        source.execute(f'UPDATE task_runs SET {column} = ?', (updated,))
        got, old = refresh(source, old)
        _, fresh_events, fresh_runs = inputs(source)
        actual = kd.compute_task_diagnostics(task, got[1]['t'], list(got[2]['t']), now=9100)
        expected = kd.compute_task_diagnostics(task, fresh_events['t'], list(fresh_runs['t']), now=9100)
        assert actual == expected
        assert (actual == baseline) == (updated is None)
        assert br._raw(got[2]['t']) == br._raw(fresh_runs['t'])


def test_untrusted_run_path_reloads(source, monkeypatch):
    old = memo(source)
    loaded = []
    real = br._rows_by_task
    def load(conn, table, ids):
        loaded.append(table)
        return real(conn, table, ids)
    monkeypatch.setattr(br, '_rows_by_task', load)
    rows = source.execute('SELECT * FROM tasks').fetchall()
    br._incremental_diag_inputs(source, rows, old, lambda *_args, **_kwargs: None, graphs={})
    assert 'task_runs' in loaded


def test_unchanged_run_bookkeeping_keeps_decoded_rows(source):
    """Only the narrow signature query runs; no second overlay/history load."""
    old = memo(source)
    _, old = refresh(source, old)  # first trusted refresh records the signature
    retained = old['t'][4]
    statements = []
    source.set_trace_callback(statements.append)
    got, old = refresh(source, old)
    source.set_trace_callback(None)
    reads = [s for s in statements if 'FROM task_runs' in s]
    assert len(reads) == 1
    # [daveey] Only NULL tests for candidate IDs, never typed text over all task runs.
    assert reads[0].startswith('SELECT id, worker_pid IS NOT NULL')
    assert got[2]['t'] is retained
    source.execute("UPDATE task_runs SET worker_pid = 42")
    got, old = refresh(source, old)
    assert got[2]['t'][0]['worker_pid'] == 42
    got, old = refresh(source, old)
    assert got[2]['t'][0]['worker_pid'] == 42


@pytest.mark.parametrize('column', ['claim_expires', 'sync_version', 'worker_pid'])
def test_unread_value_changes_keep_retained_history(source, column):
    """[daveey] Exact responses do not require exact unread bookkeeping values."""
    source.execute('UPDATE task_runs SET worker_pid=1')
    old = memo(source)
    _, old = refresh(source, old)
    retained = old['t'][4]
    for value in (2, 3):
        source.execute(f'UPDATE task_runs SET {column} = ?', (value,))
        got, old = refresh(source, old)
        assert got[2]['t'] is retained
        _, events, runs = inputs(source)
        task = dict(source.execute('SELECT * FROM tasks').fetchone())
        assert kd.compute_task_diagnostics(task, got[1]['t'], list(got[2]['t']), now=9100) == (
            kd.compute_task_diagnostics(task, events['t'], list(runs['t']), now=9100))


@pytest.mark.parametrize('assignment', ["status='completed'", "outcome='completed'", 'ended_at=NULL',
                                       "metadata='{}'", "summary='ran'", "error='failed'"])
def test_non_candidates_issue_no_signature_query(source, assignment):
    """[daveey] Ordinary historical runs cost zero SQL rows on a clean refresh."""
    source.execute(f'UPDATE task_runs SET {assignment}')
    old = memo(source)
    statements = []
    source.set_trace_callback(statements.append)
    got, _ = refresh(source, old)
    source.set_trace_callback(None)
    assert not [s for s in statements if 'FROM task_runs' in s]
    assert got[2]['t'][0] is old['t'][4][0]


def test_unused_grant_bookkeeping_read_set(source, monkeypatch):
    """[daveey] Observe real reads so a new excluded-column reader cannot silently drift."""
    from hermes_cli import kanban_unused_grant as ug, kanban_activity_revision as ar
    run = dict(source.execute('SELECT * FROM task_runs').fetchone())
    seen = set()
    get = ug._get
    def record(row, key):
        if row is run:
            seen.add(key)
        return get(row, key)
    monkeypatch.setattr(ug, '_get', record)
    assert ug.unused_grant_candidate(run)
    assert not seen & ar.BOOKKEEPING_COLUMNS['task_runs']
    seen.clear()
    assert ug.unused_grant_run(run, [{'kind': 'released', 'payload': {'unused_grant': True}}])
    assert seen & ar.BOOKKEEPING_COLUMNS['task_runs'] == set(ug.UNUSED_GRANT_BOOKKEEPING)


def _grant(conn):
    from hermes_cli import kanban_db as kb
    tid = kb.create_task(conn, title='retained grant', assignee='worker')
    conn.execute("UPDATE tasks SET status='ready', created_at=1 WHERE id=?", (tid,))
    conn.execute('DELETE FROM task_events WHERE task_id=?', (tid,))
    rid = conn.execute("INSERT INTO task_runs (task_id,profile,status,outcome,started_at,ended_at,metadata) "
                       "VALUES (?,'worker','released','released',9000,9001,?)", (tid, '{"unused_grant":true}')).lastrowid
    conn.executemany('INSERT INTO task_events (task_id,run_id,kind,payload,created_at) VALUES (?,?,?,?,?)', [
        (tid, None, 'created', '{}', 1), (tid, rid, 'claimed', '{}', 9000),
        (tid, rid, 'released', '{"unused_grant":true}', 9001)])
    return tid, rid


@pytest.mark.parametrize('mutation', ["status='completed'", "outcome='completed'", 'ended_at=NULL',
                                      "metadata='{}'", "summary='ran'", "error='failed'", 'insert', 'delete'])
def test_p2_run_changes_refresh_next_board_read(board, monkeypatch, mutation):
    """[daveey] Run membership and every candidate gate are covered by real P2 triggers."""
    from contextlib import closing
    from hermes_cli import kanban_db_connect as kbc, kanban_task_change as ktc
    monkeypatch.setattr(kd.time, 'time', lambda: 9100.0)
    with closing(kbc.connect()) as conn:
        tid, rid = _grant(conn)
        if mutation == 'delete':
            conn.execute('UPDATE task_runs SET worker_pid=1 WHERE id=?', (rid,))
            conn.execute("DELETE FROM task_events WHERE task_id=? AND kind!='created'", (tid,))
        conn.commit()
        baseline = helpers._stable(helpers._read(board))
        _, cursor = ktc.changed_since(conn, None)
        if mutation == 'insert':
            conn.execute("INSERT INTO task_runs (task_id,profile,status,outcome,started_at,ended_at) "
                         "VALUES (?,'worker','completed','completed',9000,9002)", (tid,))
        elif mutation == 'delete':
            conn.execute('DELETE FROM task_runs WHERE id=?', (rid,))
        else:
            conn.execute(f'UPDATE task_runs SET {mutation} WHERE id=?', (rid,))
        conn.commit()
        changed, _ = ktc.changed_since(conn, cursor)
        assert changed == frozenset({tid})
        actual = helpers._stable(helpers._read(board))
        assert actual != baseline
        assert actual == helpers._cold(board)


def test_clean_board_signs_only_candidate_ids_after_sync_bump(board, monkeypatch):
    """[daveey] A clean GET must not scan ordinary runs, even on the same card as a grant."""
    from contextlib import closing
    from hermes_cli import kanban_db_connect as kbc, kanban_task_change as ktc
    from plugins.kanban.dashboard import board_run_history as rh
    from hermes_cli import kanban_db_sync, kanban_activity_revision as ar
    monkeypatch.setattr(kd.time, 'time', lambda: 9100.0)
    with closing(kbc.connect()) as conn:
        kanban_db_sync.apply_sync_schema_steps(conn)
        ar.ensure_revision(conn, ar.LOCAL_TABLES)
        tid, rid = _grant(conn)
        conn.executemany("INSERT INTO task_runs (task_id,profile,status,outcome,started_at,ended_at) "
                         "VALUES (?,'worker','completed','completed',1,2)", [(tid,)] * 80)
        conn.commit()
        baseline = helpers._stable(helpers._read(board))
        _, cursor = ktc.changed_since(conn, None)
        signed = []
        real = rh._signatures
        def sign(conn, ids, columns):
            signed.extend(ids)
            return real(conn, ids, columns)
        monkeypatch.setattr(rh, '_signatures', sign)
        conn.execute('UPDATE task_runs SET sync_version=COALESCE(sync_version,0)+1 WHERE task_id=?', (tid,))
        conn.commit()
        changed, _ = ktc.changed_since(conn, cursor)
        assert changed == frozenset()
        actual = helpers._stable(helpers._read(board))
        assert signed == [rid]
        assert actual == baseline == helpers._cold(board)
