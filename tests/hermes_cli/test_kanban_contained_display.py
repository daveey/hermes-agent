"""Query-count regression for the profiles-session external wait display path."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing

import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_contained_display as display
from hermes_cli import kanban_contained_wait as literal
from hermes_cli import kanban_external_activity as external
from hermes_cli.kanban_containment_state import fingerprint
from hermes_cli.kanban_db_sync import uuid7
from tests.hermes_cli import test_kanban_containment_replica as replica_helpers
from tests.hermes_cli import test_kanban_contained_strand as local_helpers

kanban_home = replica_helpers.kanban_home
synced_conn = replica_helpers.synced_conn
board = local_helpers.board


def seed(conn, count):
    conn.executescript('''
        CREATE TABLE tasks(id TEXT PRIMARY KEY,status TEXT,block_kind TEXT,human_request TEXT,
                           awaiting TEXT,containment_episode TEXT,completed_at REAL,title TEXT);
        CREATE TABLE task_events(id INTEGER PRIMARY KEY,uid TEXT,task_id TEXT,kind TEXT,payload TEXT,created_at REAL);
        CREATE TABLE task_runs(task_id TEXT,outcome TEXT,ended_at REAL);
        CREATE TABLE kanban_sync_state(board_id TEXT);
        INSERT INTO kanban_sync_state VALUES ('fixture');
        CREATE TABLE containment_canonical_receipts(uid TEXT PRIMARY KEY,evidence TEXT);
        CREATE TABLE containment_sync_acceptances(receipt_uid TEXT,evidence TEXT,accepted_at REAL);
    ''')
    ids = []
    for i in range(count):
        tid = f't_{i:016x}'
        ids.append(tid)
        target = {'board': 'fixture', 'gateway': 'fixture-home', 'task_id': tid}
        authority = {**target, 'task_id': 't_ffffffffffffffff'}
        stop = {'version': 1, 'target': target, 'policy': {'version': 1, 'authority': authority},
                'producer_baseline': None}
        episode = uuid7()
        release = {'version': 1, 'episode': episode, 'containment_sha256': fingerprint(stop),
                   'resume_status': 'ready', 'evidence': {'kind': 'authority', 'authority': authority, 'run_uid': uuid7()}}
        conn.execute("INSERT INTO tasks VALUES (?,'triage',NULL,NULL,NULL,?,NULL,'Held')", (tid, episode))
        for uid, kind, payload in [(episode, 'contained', stop), (uuid7(), 'containment_released', release)]:
            conn.execute('INSERT INTO task_events(uid,task_id,kind,payload,created_at) VALUES (?,?,?,?,100)',
                         (uid, tid, kind, json.dumps(payload)))
    conn.execute("INSERT INTO tasks VALUES ('t_ffffffffffffffff','done',NULL,NULL,NULL,NULL,150,'Authority')")
    conn.commit()
    return ids


def counted(conn, fn):
    statements = []
    conn.set_trace_callback(statements.append)
    try:
        value = fn()
    finally:
        conn.set_trace_callback(None)
    return value, statements


def test_display_is_constant_queries_and_matches_strict_reader():
    for count in (1, 30, 600):
        with closing(sqlite3.connect(':memory:')) as conn:
            ids = seed(conn, count)
            expected, old = counted(conn, lambda: literal.read(conn, ids, replica=None))
            actual, new = counted(conn, lambda: display.read(conn, ids, replica=None))
            assert len(actual) == count
            assert actual == expected
            assert len(new) <= 10, new
            assert len(old) >= count * 5
            print(f'rows={count} strict_queries={len(old)} display_queries={len(new)}')
            assert not conn.in_transaction


@pytest.mark.parametrize('legacy', [False, True])
def test_profiles_request_wait_queries_do_not_scale_with_sessions(tmp_path, monkeypatch, legacy):
    from hermes_cli.web_routers import profiles
    path = tmp_path / 'board.db'
    with closing(sqlite3.connect(path)) as conn:
        ids = seed(conn, 40)
    pairs = [(f's{i}', tid) for i, tid in enumerate(ids)]
    statements = []
    if legacy:
        monkeypatch.setattr(display, 'read', literal.read)

    def open_ro(_path):
        conn = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
        conn.set_trace_callback(statements.append)
        return conn

    def annotate(rows, **kwargs):
        external.stamp_boards(rows, lambda: [path], open_ro, lambda conn: pairs,
                              lambda row: [row['id']], signature=None)

    monkeypatch.setattr(profiles, '_profile_targets', lambda label: [('default', None)])
    monkeypatch.setattr(profiles, '_read_serial_profile_db', lambda *args: (
        [{'id': sid, 'source': 'cli', 'started_at': 100} for sid, _ in pairs], len(pairs)))
    monkeypatch.setattr(profiles, '_corrupt_profile_stores', lambda targets: {})
    monkeypatch.setattr(profiles, 'annotate_kanban_activity', annotate)
    for name in ('goal', 'cron', 'background', 'turn', 'subagent'):
        monkeypatch.setattr(profiles, f'annotate_{name}_activity', lambda rows: None)
    monkeypatch.setattr(profiles, 'annotate_review_wait', lambda rows: None)
    external.reset_for_tests()
    app = FastAPI()
    app.include_router(profiles.sessions_router)
    with TestClient(app) as client:
        response = client.get('/api/profiles/sessions?limit=50')
    assert response.status_code == 200, response.text
    assert len(response.json()['sessions']) == len(pairs)
    if legacy:
        assert len(statements) > 400, statements
    else:
        assert len(statements) <= 25, statements
    print(f'GET legacy={legacy} sessions={len(pairs)} board_queries={len(statements)}')
    external.reset_for_tests()


def test_certified_release_capture_and_next_revision(synced_conn):
    from hermes_cli import kanban_containment_replica as receipts
    conn = synced_conn
    tid, _, stop, release, hub = replica_helpers.scenario(conn)
    replica_helpers.apply(conn, hub, [replica_helpers.event(stop, hub, 1)])
    replica_helpers.apply(conn, hub, [replica_helpers.event(release, hub, 2)])
    events = [dict(r) for r in conn.execute('SELECT * FROM task_events WHERE task_id=?', (tid,))]
    s = next(e for e in events if e['kind'] == 'contained')
    r = next(e for e in events if e['kind'] == 'containment_released')
    captured = display.Capture(conn, [tid])
    assert captured.verify(r, s) is True
    assert receipts.verified(conn, r, s) is True
    conn.execute("UPDATE kanban_sync_state SET board_id='different'")
    assert display.Capture(conn, [tid]).verify(r, s) is False
    assert receipts.verified(conn, r, s) is False
    assert captured.verify(r, s) is True  # never installed into the strict/UDF reader


@pytest.mark.parametrize('producer', [False, True])
def test_local_display_matches_strict_and_preserves_caller_transaction(board, producer):
    tid, _, prod = local_helpers._contained(board, producer=producer)
    board.execute('BEGIN')
    if prod:
        board.execute("INSERT INTO task_runs(task_id,status,outcome,started_at,ended_at) "
                      "VALUES (?,'done','completed',1,2000000000)", (prod,))
    assert display.read(board, [tid], replica=None) == literal.read(board, [tid], replica=None)
    assert board.in_transaction
    board.rollback()


def test_local_attestation_is_checked_without_authorizing_strict_callers(board):
    from hermes_cli.kanban_containment_connections import verifier
    from tests.hermes_cli import test_kanban_containment_lifecycle as lifecycle
    tid, _, authority, target, spec = lifecycle.setup(board)
    episode = lifecycle.contain(board, tid, target, spec)['episode']
    lifecycle.release(board, target, episode, authority)
    events = [dict(r) for r in board.execute('SELECT * FROM task_events WHERE task_id=?', (tid,))]
    stop = next(e for e in events if e['kind'] == 'contained')
    receipt = next(e for e in events if e['kind'] == 'containment_released')
    captured = display.Capture(board, [tid])
    assert captured.verify(receipt, stop) is True
    assert verifier(board, receipt, stop) is True
    altered = {**receipt, 'payload': '{}'}
    assert captured.verify(altered, stop) is False
    assert verifier(board, altered, stop) is False


def test_failed_display_capture_releases_only_its_own_snapshot():
    with closing(sqlite3.connect(':memory:')) as conn:
        ids = seed(conn, 1)
        conn.execute('DROP TABLE task_events')
        with pytest.raises(sqlite3.Error):
            display.read(conn, ids, replica=None)
        assert not conn.in_transaction


@pytest.mark.parametrize('raw', ['null', '{"source":"new"}', 'malformed'])
def test_acceptance_precedence_and_lazy_decode_match_literal(raw):
    from hermes_cli import kanban_containment_replica as receipts
    with closing(sqlite3.connect(':memory:')) as conn:
        ids = seed(conn, 1)
        uid = conn.execute("SELECT uid FROM task_events WHERE kind='contained'").fetchone()[0]
        conn.execute('INSERT INTO containment_canonical_receipts VALUES (?,?)', (uid, '{"source":"first"}'))
        conn.execute('INSERT INTO containment_sync_acceptances VALUES (?,?,100)', (uid, '{"source":"older"}'))
        conn.execute('INSERT INTO containment_sync_acceptances VALUES (?,?,100)', (uid, raw))
        captured = display.Capture(conn, ids)
        if raw == 'malformed':
            with pytest.raises(ValueError):
                receipts._cached(conn, uid)
            with pytest.raises(ValueError):
                captured._cached(uid)
        else:
            assert captured._cached(uid) == receipts._cached(conn, uid)
