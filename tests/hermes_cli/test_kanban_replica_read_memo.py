"""A live replica read reuses the last projection while the replica's task rows are unchanged, and only then.

Every case compares the memoized live read with an explicit-``now`` read (which always reads every row) at the
same wall time, so a hit can never answer differently from a fresh read."""
import json
import sqlite3
import time
import types

import pytest

from hermes_cli import kanban_session_activity as activity
from hermes_cli import profiles

ROWS_SQL = 'SELECT gw, key, data, updated_at'


def card(tid, **extra):
    return {'id': tid, 'board': 'build', 'profile': 'work', 'gateway': 'owner', 'status': 'running',
            'sessions': [{'gw': 'origin', 'profile': 'default', 'session': 's1', 'rel': 'shepherd'}], **extra}


def fake_clock(monkeypatch, now):
    """Wall time for this module only (``time.time`` itself stays real for pytest and sqlite)."""
    monkeypatch.setattr(activity, 'time', types.SimpleNamespace(time=now, monotonic=time.monotonic, sleep=time.sleep))


@pytest.fixture
def hub(tmp_path, monkeypatch):
    """hermes-hub's replica schema: every write carries the feed's monotonic ``seq``."""
    path = tmp_path / 'hub.db'
    with sqlite3.connect(path) as db:
        # Match hub client's open_db: rollback journaling excludes concurrent readers during commit.
        db.execute('PRAGMA journal_mode = WAL')
        db.executescript('CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);'
                         'CREATE TABLE replica(gw TEXT, coll TEXT, key TEXT, version INTEGER, seq INTEGER, data TEXT,'
                         ' updated_at INTEGER, PRIMARY KEY (gw, coll, key));'
                         'CREATE TABLE outbox(id INTEGER PRIMARY KEY, data TEXT);')
        db.executemany('INSERT INTO meta VALUES (?,?)', [('gateway', 'origin'), ('last_ok', '1000'), ('cursor', '0')])
    seq = {'n': 0}

    def put(tid, **extra):
        seq['n'] += 1
        with sqlite3.connect(path) as db:
            db.execute("INSERT INTO replica VALUES ('owner','tasks',?,1,?,?,1000000) ON CONFLICT(gw, coll, key) DO UPDATE"
                       " SET version=version+1, seq=excluded.seq, data=excluded.data",
                       (f'work/build/{tid}', seq['n'], json.dumps(card(tid, **extra))))

    statements = []
    real_open = activity._open_ro

    def traced_open(p, check_same_thread=True):
        conn = real_open(p, check_same_thread)
        conn.set_trace_callback(statements.append)
        return conn

    clock = {'now': 1000.0}
    monkeypatch.setattr(activity, '_open_ro', traced_open)
    fake_clock(monkeypatch, lambda: clock['now'])
    monkeypatch.setattr(profiles, 'get_active_profile_name', lambda: 'default')
    activity._reset_cache_for_tests()
    yield path, put, statements, clock
    activity._reset_cache_for_tests()


def live_and_fresh(path, clock):
    """(live read, explicit read at the same instant, live owners_out, explicit owners_out)."""
    owners_live, owners_fresh = set(), set()
    live = activity._read_remote_links(path, owners_out=owners_live)
    fresh = activity._read_remote_links(path, now=clock['now'], owners_out=owners_fresh)
    return live, fresh, owners_live, owners_fresh


def row_reads(statements):
    return sum(1 for s in statements if s.startswith(ROWS_SQL))


def test_unchanged_rows_are_read_once_and_answer_the_same(hub):
    path, put, statements, clock = hub
    put('t_one')
    put('t_two', status='blocked')
    first = activity._read_remote_links(path)
    with sqlite3.connect(path) as db:  # hub-sync's other writes (outbox, cursor) leave the task rows alone
        db.execute("INSERT INTO outbox (data) VALUES ('x')")
        db.execute("UPDATE meta SET v = '7' WHERE k = 'cursor'")
    clock['now'] += 2
    statements.clear()
    for _ in range(5):
        assert activity._read_remote_links(path) == first
    assert row_reads(statements) == 0
    live, fresh, owners_live, owners_fresh = live_and_fresh(path, clock)
    assert live == fresh and owners_live == owners_fresh == {('owner', 'build', 't_one'), ('owner', 'build', 't_two')}


@pytest.mark.parametrize('change', ['update', 'insert', 'delete'])
def test_any_task_row_change_rereads(hub, change):
    path, put, statements, clock = hub
    put('t_one')
    put('t_two')
    before = activity._read_remote_links(path)
    if change == 'update':
        put('t_one', status='blocked')
    elif change == 'insert':
        put('t_three')
    else:
        with sqlite3.connect(path) as db:
            db.execute("DELETE FROM replica WHERE key = 'work/build/t_two'")
    statements.clear()
    live, fresh, owners_live, owners_fresh = live_and_fresh(path, clock)
    assert row_reads(statements) == 2  # the live read missed, then the explicit read
    assert live == fresh and owners_live == owners_fresh
    assert live != before


def test_hub_staleness_cutoff_is_never_served_from_the_memo(hub):
    path, put, _, clock = hub
    put('t_one')
    assert [r['stale'] for r in activity._read_remote_links(path)] == [False]
    clock['now'] = 1000 + activity.HUB_STALE_AFTER_S + 1  # no row changed, but the hub sync is now too old
    live, fresh, _, _ = live_and_fresh(path, clock)
    assert live == fresh and [r['stale'] for r in live] == [True]


def test_meta_and_heartbeat_changes_reread(hub):
    path, put, statements, clock = hub
    put('t_one')
    activity._read_remote_links(path)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO meta VALUES ('offline_since', '1001')")
    statements.clear()
    live, fresh, _, _ = live_and_fresh(path, clock)
    assert row_reads(statements) == 2 and live == fresh and live[0]['stale'] is True
    with sqlite3.connect(path) as db:  # a heartbeat row that makes a scope fresh changes the answer's inputs
        db.execute("INSERT INTO replica VALUES ('owner','gateway','info',1,99,?,0)",
                   (json.dumps({'gateway': 'owner', 'task_scopes': [
                       {'profile': 'work', 'board': 'build', 'state': 'ok', 'observed_at': 1000.0}]}),))
    statements.clear()
    activity._read_remote_links(path)
    assert row_reads(statements) == 1


def test_heartbeat_freshness_bounds_the_reuse_window(hub):
    """Each scope heartbeat flips release eligibility at ``beat`` and ``beat + SCOPE_HEARTBEAT_FRESH_S``."""
    info = [('owner', json.dumps({'gateway': 'owner', 'task_scopes': [
        {'profile': 'work', 'board': 'build', 'state': 'ok', 'observed_at': 1100.0}]}))]
    horizon = []
    activity._project_replica(hub[0], 1000.0, 'origin', [], info, 1000.0, None, horizon_out=horizon)
    assert sorted(horizon) == [1100.0, 1000 + activity.HUB_STALE_AFTER_S, 1100 + activity.SCOPE_HEARTBEAT_FRESH_S]


def test_a_replica_without_seq_is_read_every_time(tmp_path, monkeypatch):
    path = tmp_path / 'hub.db'
    with sqlite3.connect(path) as db:
        db.executescript('CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);'
                         'CREATE TABLE replica(gw TEXT, coll TEXT, key TEXT, data TEXT, updated_at INTEGER);')
        db.executemany('INSERT INTO meta VALUES (?,?)', [('gateway', 'origin'), ('last_ok', '1000')])
        db.execute("INSERT INTO replica VALUES ('owner','tasks','work/build/t_one',?,0)", (json.dumps(card('t_one')),))
    monkeypatch.setattr(profiles, 'get_active_profile_name', lambda: 'default')
    fake_clock(monkeypatch, lambda: 1000.0)
    activity._reset_cache_for_tests()
    first = activity._read_remote_links(path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE replica SET data = ?", (json.dumps(card('t_one', status='blocked')),))
    assert activity._read_remote_links(path) != first
    assert activity._remote_read_memo is None


def test_origin_profile_is_part_of_the_key(hub, monkeypatch):
    path, put, _, clock = hub
    put('t_one')
    assert activity._read_remote_links(path)
    monkeypatch.setattr(profiles, 'get_active_profile_name', lambda: 'work')
    live, fresh, _, _ = live_and_fresh(path, clock)
    assert live == fresh == []


def test_extending_the_returned_list_never_reaches_the_memo(hub):
    """``_legacy_remote_links`` appends ghost rows to the list it gets back."""
    path, put, _, _ = hub
    put('t_one')
    activity._read_remote_links(path).append({'bogus': True})
    assert [r['id'] for r in activity._read_remote_links(path)] == ['t_one']


def test_a_new_hub_pull_alone_serves_the_memo(hub):
    """[daveey] ``last_ok`` moves on every pull (~2 s); with the task rows unchanged, the stale verdict and every
    card's ``synced_at`` (min of owner rows' update time and ``last_ok``) are unchanged, so is the answer."""
    path, put, statements, clock = hub
    put('t_one')
    first = activity._read_remote_links(path)
    assert first[0]['synced_at'] == 1000.0
    for pull in (1002, 1004, 1010):
        with sqlite3.connect(path) as db:
            db.execute("UPDATE meta SET v = ? WHERE k = 'last_ok'", (str(pull),))
        clock['now'] = pull + 0.5
        statements.clear()
        assert activity._read_remote_links(path) == first
        assert row_reads(statements) == 0
        live, fresh, owners_live, owners_fresh = live_and_fresh(path, clock)
        assert live == fresh and owners_live == owners_fresh


def test_a_pull_that_moves_a_cards_synced_at_rereads(hub):
    """An owner whose rows carry no update time is stamped with ``last_ok`` itself: a pull changes its cards."""
    path, put, statements, clock = hub
    put('t_one')
    with sqlite3.connect(path) as db:
        db.execute("UPDATE replica SET updated_at = 0")
    assert activity._read_remote_links(path)[0]['synced_at'] == 1000.0
    with sqlite3.connect(path) as db:
        db.execute("UPDATE meta SET v = '1002' WHERE k = 'last_ok'")
    clock['now'] = 1002.5
    statements.clear()
    live, fresh, _, _ = live_and_fresh(path, clock)
    assert row_reads(statements) == 2 and live == fresh and live[0]['synced_at'] == 1002.0
