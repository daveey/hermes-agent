"""Pending exposure observations use actual HTTP credentials, never a display gateway."""
import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import kanban_sync_support as support


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(home, True)
    conn = kbc.connect(home / 'kanban.db')
    client.ensure_identity(conn, 'default', 'display-only')
    yield conn
    conn.close()


def history(conn, op_id):
    columns = {r[1] for r in conn.execute('PRAGMA table_info(kanban_sync_outbox)')}
    if 'exposure_history' not in columns:
        return None
    row = conn.execute('SELECT exposure_history FROM kanban_sync_outbox WHERE op_id=?', (op_id,)).fetchone()
    return json.loads(row[0]) if row is not None and row[0] is not None else None


@contextmanager
def serving(conn):
    path = conn.execute('PRAGMA database_list').fetchone()[2]
    observations = []
    principals = {'epoch-one': 'principal-one', 'epoch-two': 'principal-two', 'epoch-three': 'principal-one'}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def do_GET(self):
            key = self.headers.get('Authorization', '').removeprefix('Bearer ')
            if self.path.endswith('/replay/capability'):
                self.answer(dict(board_incarnation='incarnation-one', gateway_principal=principals[key]))
            else:
                self.answer({})  # optional status-origin capability is absent

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            with sqlite3.connect('file:' + path + '?mode=ro', uri=True) as observed:
                observed.row_factory = sqlite3.Row
                rows = {op['client_op_id']: history(observed, op['client_op_id']) for op in body['ops']}
            observations.append((self.headers.get('Authorization'), body['ops'], rows))
            # The controlled peer consumed the full body; the reply is lost.
            self.close_connection = True

        def answer(self, body):
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', observations
    finally:
        server.shutdown()
        thread.join(5)
        server.server_close()


def send_lost(conn, url, key):
    hub = client.Hub(client.Identity(url, key, 'not-the-authenticated-principal'))
    try:
        with pytest.raises(OSError):
            client.push(conn, hub, 'default', 'display-only', primary=False, max_batches=1)
    finally:
        hub.close()


def test_new_task_capture_is_immutable_and_exposure_is_durable_before_http(board):
    tid = kb.create_task(board, title='authorized fresh intent', assignee='worker')
    row = dict(board.execute('SELECT * FROM kanban_sync_outbox WHERE row_uid=?', (tid,)).fetchone())
    assert (row['capture_protocol'], row['original_op_id'], row['original_created_at']) == (
        'capture_candidate1', row['op_id'], row['created_at']), 'fresh capture tuple missing'
    assert row['identity_state'] is None, 'C has not authorized original certification'
    with serving(board) as (url, observations):
        send_lost(board, url, 'epoch-one')
        auth, ops, histories = observations[0]
        op = next(op for op in ops if op['client_op_id'] == row['op_id'])
        saved = histories[row['op_id']]
        assert saved is not None, 'exposure must commit before the first mutation HTTP body'
        assert saved['coverage'] == 'candidate' and saved['certified'] is False
        entry, = saved['exposures']
        assert entry['gateway_principal'] == 'principal-one'
        assert entry['board_incarnation'] == 'incarnation-one' and entry['board'] == 'default'
        assert entry['credential_epoch'] == hashlib.sha256(b'epoch-one').hexdigest()
        assert entry['wire'] == op and entry['endpoint'] == '/v2/boards/default/ops'
        assert auth == 'Bearer epoch-one' and 'epoch-one' not in json.dumps(saved)
        assert history(board, row['op_id']) == saved
        with pytest.raises(sqlite3.IntegrityError, match='capture provenance is immutable'):
            board.execute('UPDATE kanban_sync_outbox SET original_op_id=? WHERE op_id=?', ('other', row['op_id']))


def test_reopen_and_authenticated_reenrollment_keep_all_exposure_epochs(board):
    tid = kb.create_task(board, title='multiple enrollments', assignee='worker')
    op_id = board.execute('SELECT op_id FROM kanban_sync_outbox WHERE row_uid=?', (tid,)).fetchone()[0]
    path = board.execute('PRAGMA database_list').fetchone()[2]
    with serving(board) as (url, observations):
        send_lost(board, url, 'epoch-one')
        first = history(board, op_id)
        reopened = kbc.connect(Path(path))
        try:
            assert history(reopened, op_id) == first
            client.ensure_identity(reopened, 'default', 'reenrolled-display')
            for key in ('epoch-two', 'epoch-three'):
                send_lost(reopened, url, key)
            saved = history(reopened, op_id)
            assert saved['coverage'] == 'candidate' and saved['certified'] is False
            assert saved['exposures'][:len(first['exposures'])] == first['exposures']
            assert [e['gateway_principal'] for e in saved['exposures']] == [
                'principal-one', 'principal-two', 'principal-one']
            assert [e['credential_epoch'] for e in saved['exposures']] == [
                hashlib.sha256(k.encode()).hexdigest() for k in ('epoch-one', 'epoch-two', 'epoch-three')]
            for index, (_auth, _ops, histories) in enumerate(observations):
                assert len(histories[op_id]['exposures']) == index + 1, 'every epoch durable before its HTTP body'
            row = reopened.execute('SELECT original_op_id,identity_state FROM kanban_sync_outbox WHERE op_id=?', (op_id,)).fetchone()
            assert tuple(row) == (op_id, None)
        finally:
            reopened.close()


def test_missing_history_is_not_reconstructed_from_a_new_authenticated_send(board):
    with kbc.write_txn(board):
        board.execute("INSERT INTO kanban_sync_outbox(op_id,table_name,row_uid,op,fields,hlc,created_at) "
                      "VALUES ('historical','tasks','t_historical','upsert','{}','',123)")
    with serving(board) as (url, observations):
        send_lost(board, url, 'epoch-one')
        saved = history(board, 'historical')
        assert saved is not None and saved['coverage'] == 'unknown' and saved['certified'] is False
        assert observations[0][2]['historical'] == saved
        assert len(saved['exposures']) == 1
        assert tuple(board.execute('SELECT capture_protocol,original_op_id,original_created_at,identity_state '
                                   "FROM kanban_sync_outbox WHERE op_id='historical'").fetchone()) == (None,) * 4


def test_http_first_io_reads_already_committed_exposure(board):
    tid = kb.create_task(board, title='first I/O boundary', assignee='worker')
    op_id = board.execute('SELECT op_id FROM kanban_sync_outbox WHERE row_uid=?', (tid,)).fetchone()[0]
    with serving(board) as (url, observations):
        send_lost(board, url, 'epoch-one')
        assert observations[0][2][op_id] is not None, 'the HTTP peer saw no durable exposure'
        assert len(observations[0][2][op_id]['exposures']) == 1, 'exposure was written after HTTP I/O'


def test_exposure_commit_failure_forbids_http_mutation(board):
    tid = kb.create_task(board, title='retain on journal write failure', assignee='worker')
    op_id = board.execute('SELECT op_id FROM kanban_sync_outbox WHERE row_uid=?', (tid,)).fetchone()[0]
    path = board.execute('PRAGMA database_list').fetchone()[2]
    before = history(board, op_id)
    with serving(board) as (url, observations):
        with sqlite3.connect('file:' + path + '?mode=ro', uri=True) as readonly:
            readonly.row_factory = sqlite3.Row
            hub = client.Hub(client.Identity(url, 'epoch-one', 'display'))
            try:
                with pytest.raises(sqlite3.OperationalError, match='readonly'):
                    hub.ops('default', [dict(client_op_id=op_id)], custody=readonly)
            finally:
                hub.close()
        assert observations == []
        assert history(board, op_id) == before


def test_probe_and_http_headers_pin_the_same_credential_snapshot(board, monkeypatch):
    tid = kb.create_task(board, title='credential transition during probe', assignee='worker')
    op_id = board.execute('SELECT op_id FROM kanban_sync_outbox WHERE row_uid=?', (tid,)).fetchone()[0]
    with serving(board) as (url, observations):
        outer = client.Hub(client.Identity(url, 'epoch-one', 'same-display'))
        original = client.Hub._headers

        def change_enrollment(transport):
            headers = original(transport)
            outer.ident = client.Identity(url, 'epoch-two', 'same-display')
            return headers

        monkeypatch.setattr(client.Hub, '_headers', change_enrollment)
        try:
            with pytest.raises(OSError):
                outer.ops('default', [dict(client_op_id=op_id)], custody=board)
        finally:
            outer.close()
        assert observations[0][0] == 'Bearer epoch-one'
        entry, = history(board, op_id)['exposures']
        assert entry['gateway_principal'] == 'principal-one'
        assert entry['credential_epoch'] == hashlib.sha256(b'epoch-one').hexdigest()


@pytest.mark.parametrize('replacement', [None, '{}'])
def test_recorded_exposure_cannot_be_erased_or_relabelled(board, replacement):
    tid = kb.create_task(board, title='sticky history', assignee='worker')
    op_id = board.execute('SELECT op_id FROM kanban_sync_outbox WHERE row_uid=?', (tid,)).fetchone()[0]
    with serving(board) as (url, _observations):
        send_lost(board, url, 'epoch-one')
    saved = history(board, op_id)
    assert saved is not None
    with pytest.raises(sqlite3.IntegrityError, match='exposure history is append-only'):
        with kbc.write_txn(board):
            board.execute('UPDATE kanban_sync_outbox SET exposure_history=? WHERE op_id=?', (replacement, op_id))
    assert history(board, op_id) == saved
