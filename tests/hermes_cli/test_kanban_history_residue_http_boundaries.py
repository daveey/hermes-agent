"""Producer network/commit boundaries, not current-source authorization."""
import sqlite3

import pytest

from hermes_cli import kanban_history_residue_custody as custody
from hermes_cli import kanban_history_residue_http as transport
from hermes_cli.kanban_history_residue_source import Refused
from hermes_cli.kanban_sync_client import Hub, Identity
from tests.hermes_cli import test_kanban_history_residue_http as helpers
from tests.hermes_cli.test_kanban_history_residue_custody import request

server = helpers.server
db = helpers.db
scope = helpers.scope


@pytest.mark.parametrize('endpoint', ['https://Board.example', 'https://board.example/',
                                     'https://board.example:443', 'https://board.example/prefix'])
def test_endpoint_alias_is_rejected_not_silently_given_new_custody(db, endpoint):
    raw, _ = request()
    hub = Hub(Identity(endpoint, 'key', 'home'))
    with pytest.raises(Refused, match='endpoint_noncanonical'):
        transport.observe(db, hub, 'default', raw)
    assert not db.execute('SELECT name FROM sqlite_master').fetchall()


@pytest.mark.parametrize('key', ['bad\r\nkey', 'bad key', '\u00e9key'])
def test_invalid_bearer_fails_before_persisting_intent(db, key):
    raw, _ = request()
    hub = Hub(Identity('https://board.example', key, 'home'))
    with pytest.raises(Refused, match='endpoint_identity'):
        transport.observe(db, hub, 'default', raw)
    assert not db.execute('SELECT name FROM sqlite_master').fetchall()


def test_loopback_does_not_use_environment_proxy(db, server, monkeypatch):
    hub, state, raw = server
    monkeypatch.setattr(transport.urllib.request, 'getproxies', lambda: {'http': 'http://127.0.0.1:1'})
    monkeypatch.setattr(transport.urllib.request, 'proxy_bypass', lambda host: False)
    assert transport.observe(db, hub, 'default', raw)['recorded']
    assert len(state['calls']) == 2


def test_failed_post_and_get_remain_get_only_on_repeat(db, server):
    hub, state, raw = server
    state['status'] = 500
    for _ in range(2):
        with pytest.raises(Exception):
            transport.observe(db, hub, 'default', raw)
    assert [c[0] for c in state['calls']] == ['POST', 'GET', 'GET']
    assert custody.lookup(db, scope(hub), 'op-original')['receipt_json'] is None


def test_current_is_inert_get_without_local_custody(db, server):
    hub, state, _ = server
    state['receipt'] = b'{"execution_authority":true}'
    assert transport.current(hub, 'default', 'op-original') == state['receipt'].decode()
    assert [c[0] for c in state['calls']] == ['GET']
    assert state['calls'][0][1] == '/v2/boards/default/history-residue?op_id=op-original&current=1'
    assert state['calls'][0][3] == b''
    assert not db.execute('SELECT name FROM sqlite_master').fetchall()


def test_real_reopen_after_successful_remote_commit_and_local_seal_failure(db, server, monkeypatch):
    hub, state, raw = server
    original = custody.seal

    def fail(*args):
        raise sqlite3.OperationalError('injected disk failure')

    monkeypatch.setattr(custody, 'seal', fail)
    with pytest.raises(sqlite3.OperationalError):
        transport.observe(db, hub, 'default', raw)
    monkeypatch.setattr(custody, 'seal', original)
    path = db.execute('PRAGMA database_list').fetchone()[2]
    with sqlite3.connect(path, isolation_level=None) as other:
        assert transport.observe(other, hub, 'default', raw)['recorded']
    assert [c[0] for c in state['calls']] == ['POST', 'GET', 'GET']


def test_only_one_transaction_gets_first_send_right(db, server):
    hub, _, raw = server
    assert custody.stage_once(db, scope(hub), raw) is True
    path = db.execute('PRAGMA database_list').fetchone()[2]
    with sqlite3.connect(path, isolation_level=None) as other:
        assert custody.stage_once(other, scope(hub), raw) is False


def test_truncated_get_does_not_seal_or_replay(db, server):
    hub, state, raw = server
    custody.stage(db, scope(hub), raw)
    state['truncated'] = True
    with pytest.raises(OSError):
        transport.observe(db, hub, 'default', raw)
    assert [c[0] for c in state['calls']] == ['GET']
    assert custody.lookup(db, scope(hub), 'op-original')['receipt_json'] is None


def test_incomplete_body_deadline_joins_reader_and_does_not_seal(db, server, monkeypatch):
    import threading
    from hermes_cli.kanban_sync_attachment_deadline import Transfer

    hub, state, raw = server
    custody.stage(db, scope(hub), raw)
    state.update(slow=True, release=threading.Event(), peer_finished=threading.Event())
    hub.timeout = 0.2
    readers = []
    original = Transfer.run

    def capture(self, operation):
        def recorded():
            readers.append(threading.current_thread())
            return operation()
        return original(self, recorded)

    monkeypatch.setattr(Transfer, 'run', capture)
    try:
        with pytest.raises((TimeoutError, OSError)):
            transport.observe(db, hub, 'default', raw)
        readers[0].join(1)
        assert not readers[0].is_alive()
        assert custody.lookup(db, scope(hub), 'op-original')['receipt_json'] is None
    finally:
        state['release'].set()
        assert state['peer_finished'].wait(2)


def test_post_commit_failure_during_stage_never_sends_on_retry(db, server, monkeypatch):
    hub, state, raw = server
    original = custody.stage_once

    def uncertain(*args):
        original(*args)
        raise OSError('lost local commit answer')

    monkeypatch.setattr(custody, 'stage_once', uncertain)
    with pytest.raises(OSError):
        transport.observe(db, hub, 'default', raw)
    assert state['calls'] == []
    monkeypatch.setattr(custody, 'stage_once', original)
    with pytest.raises(Refused, match='observation_unconfirmed'):
        transport.observe(db, hub, 'default', raw)
    assert [c[0] for c in state['calls']] == ['GET']
