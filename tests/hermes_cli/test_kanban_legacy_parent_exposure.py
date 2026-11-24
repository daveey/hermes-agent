"""Legacy synthetic parents keep rebuild semantics and durable supporting exposure.

Production Hub/loopback transport; no deployed Board or migration certification.
The initial three cases reproduce independent review 6035459985.
"""
import json
import sqlite3
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync, kanban_source_clones
from hermes_cli import kanban_sync_client as client
from hermes_cli import kanban_sync_exposure as exposure
from tests.hermes_cli import kanban_sync_support as support


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(home, True)
    conn = kbc.connect(home / 'kanban.db')
    client.ensure_identity(conn, 'default', 'macbook')
    yield conn
    conn.close()


def pending_parent(conn, *, missing_origin=False):
    tid = kb.create_task(conn, title='parent', assignee='worker')
    with kbc.write_txn(conn):
        conn.execute("UPDATE kanban_sync_outbox SET state='rejected' WHERE table_name='tasks' AND row_uid=?", (tid,))
        if missing_origin:
            conn.execute('DELETE FROM kanban_sync_status_origins WHERE task_id=?', (tid,))
    return tid


@contextmanager
def serving(conn, *, origins=False, lost=False, remove_source=False):
    path = conn.execute('PRAGMA database_list').fetchone()[2]
    state = SimpleNamespace(sent=[], histories=[], lost=lost, remove_source=remove_source)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.endswith('/replay/capability'):
                if state.remove_source and '/default/' in self.path:
                    with sqlite3.connect(path) as other:
                        other.execute("UPDATE kanban_sync_outbox SET op_id='lost-'||op_id WHERE table_name='task_events'")
                self.reply(dict(gateway_principal='principal', board_incarnation='incarnation'))
            elif '/changes?' in self.path:
                self.reply(dict(changes=[], next=0, head=0, more=False))
            else:
                self.reply(dict(status_origin=1, frozen_replay=1, hlc='1800000000000-0000-server') if origins else {})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            state.sent.append(body['ops'])
            with sqlite3.connect('file:' + path + '?mode=ro', uri=True) as other:
                state.histories.append({row[0]: json.loads(row[1]) for row in other.execute(
                    'SELECT op_id,exposure_history FROM kanban_sync_outbox WHERE exposure_history IS NOT NULL')})
            if state.lost:
                self.close_connection = True
                return
            self.reply({'results': [dict(client_op_id=o['client_op_id'], status='applied', version=1) for o in body['ops']]})

        def reply(self, body):
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever)
    worker.start()
    hub = client.Hub(client.Identity(f'http://127.0.0.1:{server.server_port}', 'fixture-key', 'macbook'))
    try:
        yield hub, state
    finally:
        hub.close()
        server.shutdown()
        worker.join(5)
        server.server_close()


@pytest.mark.parametrize('origins,missing_origin', [(False, False), (True, True), (True, False)])
def test_legacy_parent_http(board, origins, missing_origin):
    tid = pending_parent(board, missing_origin=missing_origin)
    with serving(board, origins=origins) as (hub, state):
        result = client.push(board, hub, 'default', 'macbook', primary=False, max_batches=1)
    assert result.applied > 0
    wire, = state.sent
    parent = next(o for o in wire if o['table'] == 'tasks')
    children = [o for o in wire if o['table'] == 'task_events']
    assert parent['uid'] == tid and children
    if not origins or missing_origin:
        assert 'status_origin' not in parent and 'frozen_replay' not in parent
        assert not board.execute('SELECT 1 FROM kanban_sync_parent_envelopes').fetchone()
        for child in children:
            history = state.histories[0][child['client_op_id']]
            assert history['coverage'] == 'unknown' and history['certified'] is False
            assert any(e['wire'] == parent for e in history['exposures']), 'parent exposure must commit before POST'
    assert board.execute('SELECT sync_version FROM tasks WHERE id=?', (tid,)).fetchone()[0] == 1


@pytest.mark.parametrize('origins', [False, True])
def test_lost_legacy_parent_rebuild_keeps_prior_exposure_on_each_child(board, origins):
    tid = pending_parent(board, missing_origin=True)
    path = Path(board.execute('PRAGMA database_list').fetchone()[2])
    with serving(board, origins=origins, lost=True) as (hub, state):
        with pytest.raises(OSError):
            client.push(board, hub, 'default', 'macbook', primary=False, max_batches=1)
        original = state.sent[0][0]
        children = [op for op in state.sent[0] if op['table'] != 'tasks']
        first = state.histories[0]
        with kbc.connect(path) as reopened:
            for child in children:
                row = reopened.execute('SELECT exposure_history,capture_protocol,identity_state FROM kanban_sync_outbox '
                                       'WHERE op_id=?', (child['client_op_id'],)).fetchone()
                assert json.loads(row[0]) == first[child['client_op_id']]
                assert tuple(row)[1:] == (None, None)
            with kbc.write_txn(reopened):
                reopened.execute('UPDATE kanban_sync_outbox SET created_at=0')
                reopened.execute('UPDATE tasks SET title=? WHERE id=?', ('later local edit', tid))
                reopened.execute("UPDATE kanban_sync_outbox SET state='rejected' WHERE table_name='tasks'")
            with pytest.raises(OSError):
                client.push(reopened, hub, 'default', 'macbook', primary=False, max_batches=1)
            replacement = state.sent[1][0]
            assert replacement['client_op_id'] != original['client_op_id']
            assert replacement['fields']['title'] == 'later local edit'
            assert not reopened.execute('SELECT 1 FROM kanban_sync_parent_envelopes').fetchone()
            for child in children:
                history = state.histories[1][child['client_op_id']]
                assert history['exposures'][:len(first[child['client_op_id']]['exposures'])] == first[child['client_op_id']]['exposures']
                for parent in (original, replacement):
                    entry = next(e for e in history['exposures'] if e['wire'] == parent)
                    assert entry['gateway_principal'] == 'principal' and entry['board_incarnation'] == 'incarnation'
                assert history['coverage'] == 'unknown' and history['certified'] is False


@pytest.mark.parametrize('declared,frozen,child_kind,replay', [
    pytest.param(False, False, 'valid', False, id='undeclared'),
    pytest.param(True, True, 'valid', False, id='frozen'),
    pytest.param(True, False, 'none', False, id='no_child'),
    pytest.param(True, False, 'lost', False, id='missing_child'),
    pytest.param(True, False, 'unrelated', False, id='unrelated_child'),
    pytest.param(True, False, 'valid', True, id='replay'),
])
def test_missing_custody_never_becomes_a_legacy_exemption(board, declared, frozen, child_kind, replay):
    tid = pending_parent(board, missing_origin=True)
    row = board.execute("SELECT op_id,row_uid FROM kanban_sync_outbox WHERE table_name='task_events'").fetchone()
    parent = dict(client_op_id='ephemeral', table='tasks', uid=tid, op='upsert', fields={'title': 'support'})
    child = dict(client_op_id='lost-child' if child_kind == 'lost' else row[0],
                 table='task_events', uid=row[1], op='upsert',
                 fields={'task_id': 'another-task' if child_kind == 'unrelated' else tid})
    if frozen:
        parent['frozen_replay'] = 1
    ops = [parent] + ([] if child_kind == 'none' else [child])
    before = [tuple(r) for r in board.execute('SELECT * FROM kanban_sync_outbox')]
    with serving(board) as (hub, state):
        with pytest.raises(exposure.ExposureCustodyError, match='custody missing'):
            exposure.send(hub, board, 'default', ops, legacy_parents=[parent] if declared else [],
                          replay={} if replay else None)
        assert state.sent == []
    assert [tuple(r) for r in board.execute('SELECT * FROM kanban_sync_outbox')] == before


def test_link_support_exposure_is_kept_for_both_unsaved_parents(board):
    left, right = pending_parent(board), pending_parent(board)
    with kbc.write_txn(board):
        board.execute("UPDATE kanban_sync_outbox SET state='rejected' WHERE table_name='task_events'")
        board.execute("INSERT INTO kanban_sync_outbox(op_id,table_name,row_uid,op,fields,hlc,created_at) "
                      "VALUES ('link','task_links',?,'upsert','{}','',1)", (left + '>' + right,))
    with serving(board) as (hub, state):
        result = client.push(board, hub, 'default', 'macbook', primary=False, max_batches=1)
    assert result.applied == 1
    ops, = state.sent
    parents = [op for op in ops if op['table'] == 'tasks']
    assert {op['uid'] for op in parents} == {left, right}
    history = state.histories[0]['link']
    for parent in parents:
        assert any(e['wire'] == parent for e in history['exposures'])


def test_lost_custody_refusal_isolates_board_in_actual_http_round(board, tmp_path, monkeypatch):
    pending_parent(board)
    broken_path = Path(board.execute('PRAGMA database_list').fetchone()[2])
    healthy_path = tmp_path / 'healthy.db'
    with kbc.connect(healthy_path) as healthy:
        client.ensure_identity(healthy, 'healthy', 'macbook')
        sync.set_mode(healthy, 'shadow')
        tid = kb.create_task(healthy, title='healthy work', assignee='worker')
    monkeypatch.setattr(client, 'publish_pickup_if_changed', lambda hub: None)
    monkeypatch.setattr(kanban_source_clones, 'attest_if_due', lambda hub: None)
    with serving(board, remove_source=True) as (hub, state):
        results = client.sync_all_once(hub, boards=[('default', broken_path), ('healthy', healthy_path)])
    assert [r.board for r in results] == ['default', 'healthy']
    assert 'ExposureCustodyError' in results[0].error
    assert results[1].error is None, results[1].error
    assert any(op.get('uid') == tid for batch in state.sent for op in batch)
    with kbc.connect(healthy_path) as healthy:
        assert not healthy.execute("SELECT 1 FROM kanban_sync_outbox WHERE state='pending'").fetchone()
