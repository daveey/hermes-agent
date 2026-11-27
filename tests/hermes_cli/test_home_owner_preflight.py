"""Unavailable Board protocols must not strand a new local preparation."""
from __future__ import annotations

import http.server
import json
import threading
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as dbc
from hermes_cli import kanban_home_owner as owner, kanban_home_transfer as transfer
from hermes_cli.kanban_sync_client import Hub, HubError, Identity
from tests.hermes_cli import test_home_transfer_capture as support

board = support.board


def candidate(conn):
    task = kb.create_task(conn, title='portable idle card')
    with dbc.write_txn(conn):
        conn.execute("UPDATE tasks SET status='triage',created_on='worker-a3',sync_version=7 WHERE id=?", (task,))
        conn.execute("UPDATE kanban_sync_state SET gateway_id='worker-a3'")
        conn.execute('DELETE FROM kanban_sync_outbox')
    bid = conn.execute('SELECT board_id FROM kanban_sync_state').fetchone()[0]
    return bid, dict(task=task, source='worker-a3', destination='hub', nonce='preflight-owner-00001', expected_version=7)


@contextmanager
def peer(conn, cap_status=200, cap=None, post_status=200):
    state = dict(cap_status=cap_status, cap={'home_transfer': 1} if cap is None else cap,
                 post_status=post_status, requests=[], frozen_at_post=[])

    class Handler(http.server.BaseHTTPRequestHandler):
        def reply(self, status, body):
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            state['requests'].append(('GET', self.path))
            self.reply(state['cap_status'], state['cap'])

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            state['requests'].append(('POST', body))
            # A separate connection proves the freeze committed before bytes left.
            observed = dbc.connect(Path(db_path))
            try:
                state['frozen_at_post'].append(transfer.frozen(observed, body['task']))
            finally:
                observed.close()
            if state['post_status'] != 200:
                self.reply(state['post_status'], {'error': 'unavailable'})
                return
            phase = 'prepared' if body['step'] == 'prepare' else 'committed'
            self.reply(200, {**{k: v for k, v in body.items() if k != 'step'}, 'phase': phase, 'rows': []})

        def log_message(self, *args):
            pass

    db_path = conn.execute('PRAGMA database_list').fetchone()[2]
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    hub = Hub(Identity(f'http://127.0.0.1:{server.server_address[1]}', 'fixture-key', 'worker-a3'))
    try:
        yield hub, state
    finally:
        hub.close()
        server.shutdown()
        thread.join(5)
        server.server_close()
        assert not thread.is_alive()


@pytest.mark.parametrize('status,cap', [(404, {}), (405, {}), (401, {}), (503, {}),
                                      (200, {}), (200, {'home_transfer': 0}),
                                      (200, {'home_transfer': True}), (200, {'home_transfer': '1'}),
                                      (200, {'home_transfer': 2})])
def test_unavailable_protocol_never_prepares_or_sends(board, status, cap):
    bid, ident = candidate(board)
    before = sorted(board.iterdump())
    with peer(board, status, cap) as (hub, state):
        with pytest.raises((HubError, ValueError)):
            owner.source_step(board, hub, bid, ident)
        assert sorted(board.iterdump()) == before
        assert not transfer.frozen(board, ident['task'])
        assert state['requests'] == [('GET', f'/v2/boards/{quote(bid, safe="")}/home-transfer/capability')]
    kb.add_comment(board, ident['task'], 'human', 'still writable')
    assert kb.promote_task(board, ident['task'], actor='human')[0]


def test_capable_peer_sees_committed_freeze_and_exact_tuple_retries(board):
    bid, ident = candidate(board)
    with peer(board) as (hub, state):
        assert owner.source_step(board, hub, bid, ident)['phase'] == 'committed'
        assert state['frozen_at_post'] == [True, True]
        before = sorted(board.iterdump())
        assert owner.source_step(board, hub, bid, ident)['phase'] == 'committed'
        assert sorted(board.iterdump()) == before
        assert [r[1] for r in state['requests'] if r[0] == 'POST'] == [
            {**ident, 'step': step} for step in ('prepare', 'commit', 'prepare', 'commit')]
        assert [r for r in state['requests'] if r[0] == 'GET'] == [
            ('GET', f'/v2/boards/{quote(bid, safe="")}/home-transfer/capability')] * 2


@pytest.mark.parametrize('post_status', [404, 503])
def test_after_prepare_unknown_response_never_unfreezes(board, post_status):
    bid, ident = candidate(board)
    with peer(board, post_status=post_status) as (hub, state):
        with pytest.raises(HubError):
            owner.source_step(board, hub, bid, ident)
        assert transfer.frozen(board, ident['task'])
        assert state['frozen_at_post'] == [True]
        before = sorted(board.iterdump())
        state['cap_status'] = 404
        with pytest.raises(HubError):
            owner.source_step(board, hub, bid, ident)
        assert sorted(board.iterdump()) == before
        assert transfer.frozen(board, ident['task'])
        assert len(state['frozen_at_post']) == 1


def test_capability_is_not_reused_for_later_new_preparation(board):
    bid, ident = candidate(board)
    with peer(board) as (hub, state):
        owner.source_step(board, hub, bid, ident)
        bid2, other = candidate(board)
        before = sorted(board.iterdump())
        state['cap_status'] = 404
        with pytest.raises(HubError):
            owner.source_step(board, hub, bid2, other)
        assert sorted(board.iterdump()) == before
        assert not transfer.frozen(board, other['task'])
        assert transfer.frozen(board, ident['task'])
