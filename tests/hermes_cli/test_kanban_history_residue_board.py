"""Configured entry points compose real SQLite custody, local fences and cut reports."""
import json
import sqlite3
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_history_residue_board as board
from hermes_cli import kanban_history_residue_local as local
from hermes_cli import kanban_sync_client as client
from hermes_cli.kanban_history_residue_source import Refused
from hermes_cli.kanban_sync_guard import board_sync_lock, SyncRoundBusy
from tests.hermes_cli import test_kanban_history_residue_local as fixtures
from tests.hermes_cli.test_kanban_history_residue_proof import proof

stores = fixtures.stores


@pytest.fixture
def configured(stores, tmp_path, monkeypatch):
    paths = [tmp_path / 'live.db', tmp_path / 'shadow.db']
    for source, path in zip(stores[:2], paths):
        with sqlite3.connect(path) as target:
            source.backup(target)
    connections = []

    def connect(path):
        conn = sqlite3.connect(path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        connections.append(conn)
        return conn

    monkeypatch.setattr(kanban_db, 'kanban_db_path', lambda **kw: paths[0])
    monkeypatch.setattr(client, 'shadow_path', lambda slug: paths[1])
    monkeypatch.setattr(kbc, 'connect', connect)
    monkeypatch.setattr(client, 'open_shadow', lambda *a: connect(paths[1]))
    ident = client.Identity('https://board.example', 'key', 'home')
    hub = SimpleNamespace(ident=ident, close=lambda: None,
                          changes=lambda *a: dict(changes=[], more=False, head=100, next=100))
    monkeypatch.setattr(client, 'load_identity', lambda: ident)
    monkeypatch.setattr(client, 'Hub', lambda *a: hub)
    calls = []
    state = {}

    def pull(slug, remote, gateway, path, **kw):
        assert kw == {'diagnostic': True}
        with board_sync_lock(path):  # proves refresh is not nested inside the same lock
            return client.Pulled()

    def request(remote, method, path, body):
        for conn in connections:
            try:
                assert not conn.in_transaction
            except sqlite3.ProgrammingError:
                pass  # observe_board closed its owned handles before audit_diff opens new ones
        calls.append((method, path, body))
        if method == 'POST':
            req = json.loads(body)
            _, response = proof()
            response['receipt']['source_json'] = req['source_json']
            with sqlite3.connect(paths[0]) as conn:
                conn.row_factory = sqlite3.Row
                parent = local.snapshot(conn, req['source_json'], board='default', gateway='home', replica=False)['portable']
            response['receipt']['parent'].update(parent)
            response['current']['parent'].update(parent)
            response['receipt']['reason'] = req['reason']
            state['proof'] = response
        response = state.get('proof')
        return json.dumps(response if 'current=1' in path else response['receipt'] if response else None).encode()

    monkeypatch.setattr(client, '_pull_replica', pull)
    monkeypatch.setattr(board.transport, '_request', request)
    yield paths, hub, calls, state, connect
    for conn in connections:
        conn.close()


def test_observe_then_fresh_diff_preserves_all_original_bytes(configured):
    paths, hub, calls, _state, connect = configured
    live, shadow = (connect(path) for path in paths)
    before = [list(conn.iterdump()) for conn in (live, shadow)]
    assert board.observe_board('default', 'op-original', 'Keep history')['recorded'] is True
    assert [c[0] for c in calls] == ['POST','GET']
    assert board.observe_board('default', 'op-original', 'Keep history')['recorded'] is True
    assert [c[0] for c in calls] == ['POST','GET','GET']
    result = board.audit_diff(live, shadow, hub, 'default', pulled=client.Pulled(), started=board.time.monotonic())
    assert result['diff']['clean'] is True and result['diff']['rejected_ops'] == 1
    assert [list(conn.iterdump()) for conn in (live, shadow)] == before
    assert board.ledger_path('default').stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('fault', ['busy','more','wrongpath','lock'])
def test_audit_entry_refuses_wrong_refresh_or_paths_or_lock(configured, fault):
    paths, hub, _calls, _state, connect = configured
    board.observe_board('default', 'op-original', 'Keep history')
    live, shadow = (connect(path) for path in paths)
    pulled = client.Pulled()
    if fault == 'busy': pulled.busy = True
    if fault == 'more': pulled.more = True
    if fault == 'wrongpath': live, shadow = shadow, live
    with board_sync_lock(paths[0]) if fault == 'lock' else board.ExitStack():
        with pytest.raises((Refused, SyncRoundBusy)):
            board.audit_diff(live, shadow, hub, 'default', pulled=pulled, started=board.time.monotonic())


def test_observe_cannot_change_source_or_replace_reason(configured):
    paths, _hub, calls, _state, connect = configured
    board.observe_board('default', 'op-original', 'Keep history')
    with pytest.raises(Refused, match='intent_conflict'):
        board.observe_board('default', 'op-original', 'different')
    live = connect(paths[0])
    live.execute('UPDATE kanban_sync_outbox SET attempts=3')
    with pytest.raises(Refused, match='local_source_changed'):
        board.observe_board('default', 'op-original', 'Keep history')
    assert [c[0] for c in calls] == ['POST','GET']
