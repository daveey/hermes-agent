"""Raw JSON/encoding failures are typed transport failures, never replayed.

Exercise both HTTP implementations and the real multi-board strict sender.
"""
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_replay_journal as journal
from hermes_cli import kanban_source_clones
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import test_kanban_replay_sender as helpers
from tests.hermes_cli.test_kanban_replay_http_session import AckReceiver, serving

live = helpers.live


@pytest.fixture(autouse=True)
def portable_depth_decoder(monkeypatch):
    """Exercise real recursion failure independently of the C decoder's stack.

    CPython builds differ in how many array levels their C scanner accepts.
    Only the depth-limit response uses the stdlib Python scanner; all other
    payloads retain the production decoder and both real HTTP transports.
    """
    original = json.loads
    decoder = json.JSONDecoder()
    decoder.scan_once = json.scanner.py_make_scanner(decoder)
    depth_reply = b'[' * 100000 + b'0' + b']' * 100000

    def decode(value, *args, **kwargs):
        if value == depth_reply:
            return decoder.decode(value.decode())
        return original(value, *args, **kwargs)

    monkeypatch.setattr(json, 'loads', decode)


BAD_REPLIES = [
    b'{', b'\xff',
    pytest.param(b'[' * 100000 + b'0' + b']' * 100000, id='depth-limit'),
    pytest.param(b'9' * 100000, id='integer-limit'),
]


@pytest.mark.parametrize('proxied', [False, True])
@pytest.mark.parametrize('raw', [b'{', b'\xff', b'{}'])
@pytest.mark.parametrize('method', ['GET', 'POST'])
def test_response_decode_failure_is_typed_and_not_retried(live, monkeypatch, proxied, raw, method):
    monkeypatch.setattr(client.Hub, '_proxied', staticmethod(lambda parts: proxied))
    with serving(live, AckReceiver(), encode_reply=lambda path, reply: raw) as (url, requests, _):
        hub = client.Hub(client.Identity(url, 'fixture-key', 'macbook'))
        try:
            if raw == b'{}':
                assert hub._call(method, '/v2/boards/default/replay/capability') == {}
            else:
                with pytest.raises(client.HubError, match='invalid JSON response') as caught:
                    hub._call(method, '/v2/boards/default/replay/capability')
                assert caught.value.status == 502
                assert isinstance(caught.value.__cause__, (json.JSONDecodeError, UnicodeDecodeError))
            assert len(requests) == 1
        finally:
            hub.close()


@pytest.mark.parametrize('proxied', [False, True])
@pytest.mark.parametrize('raw', BAD_REPLIES)
@pytest.mark.parametrize('endpoint', ['capability', 'inspect', 'session', 'ops', 'ack'])
def test_strict_raw_reply_preserves_custody_and_other_board_progress(live, tmp_path, monkeypatch,
                                                                   proxied, raw, endpoint):
    monkeypatch.setattr(client.Hub, '_proxied', staticmethod(lambda parts: proxied))
    kb.create_task(live, title='strict raw reply', assignee='worker')
    if endpoint in ('ops', 'ack'):
        with kbc.write_txn(live):
            live.execute("DELETE FROM kanban_sync_outbox WHERE table_name != 'tasks'")
    broken_path = Path(live.execute('PRAGMA database_list').fetchone()[2])
    custody = [row[0] for row in live.execute('SELECT op_id FROM kanban_sync_outbox ORDER BY op_id')]
    healthy_path = tmp_path / 'healthy.db'
    with kbc.connect(healthy_path) as healthy:
        client.ensure_identity(healthy, 'healthy', 'macbook')
        sync.set_mode(healthy, 'shadow')
    peer = AckReceiver()
    original = peer._call

    def route(method, path, body=None):
        if '/changes?' in path:
            return dict(changes=[], next=0, head=0, more=False)
        return original(method, path, body)

    def encode(path, reply):
        return raw if path.endswith('/replay/' + endpoint) else json.dumps(reply).encode()

    peer._call = route
    monkeypatch.setattr(client, 'publish_pickup_if_changed', lambda hub: None)
    monkeypatch.setattr(kanban_source_clones, 'attest_if_due', lambda hub: None)
    with serving(live, peer, encode_reply=encode) as (url, requests, _):
        hub = client.Hub(client.Identity(url, 'fixture-key', 'macbook'))
        try:
            results = client.sync_all_once(hub, boards=[('default', broken_path), ('healthy', healthy_path)])
            assert [result.board for result in results] == ['default', 'healthy']
            if endpoint in ('ops', 'ack'):
                # The next cooperative send can inspect and reconcile the
                # already-applied mutation. That is success, not a lost fault.
                assert results[0].error is None
                assert journal.pending_wire(live) is None
                assert journal.snapshot(live)['settled'] == '1'
                assert sum(path.endswith('/replay/ops') for _, path, _, _ in requests) == 1
            else:
                assert results[0].error and 'HubError' in results[0].error
                assert 'invalid JSON response' in results[0].error
                assert [row[0] for row in live.execute(
                    'SELECT op_id FROM kanban_sync_outbox ORDER BY op_id')] == custody
            assert results[1].error is None
            assert any('/default/changes?' in path for _, path, _, _ in requests)
            assert any('/healthy/changes?' in path for _, path, _, _ in requests)
        finally:
            hub.close()


@pytest.mark.parametrize('proxied', [False, True])
@pytest.mark.parametrize('raw', BAD_REPLIES)
@pytest.mark.parametrize('endpoint', ['ops', 'ack'])
def test_strict_mutation_decode_failure_keeps_durable_progress(live, monkeypatch, proxied, raw, endpoint):
    monkeypatch.setattr(client.Hub, '_proxied', staticmethod(lambda parts: proxied))
    kb.create_task(live, title='strict unreadable mutation reply', assignee='worker')
    with kbc.write_txn(live):
        live.execute("DELETE FROM kanban_sync_outbox WHERE table_name != 'tasks'")
    peer = AckReceiver()
    broken = [True]

    def encode(path, reply):
        return raw if broken[0] and path.endswith('/replay/' + endpoint) else json.dumps(reply).encode()

    with serving(live, peer, encode_reply=encode) as (url, requests, _):
        hub = client.Hub(client.Identity(url, 'fixture-key', 'macbook'))
        try:
            with pytest.raises(client.HubError, match='invalid JSON response'):
                client.push(live, hub, 'default', 'macbook', primary=False)
            assert sum(path.endswith('/replay/' + endpoint) for _, path, _, _ in requests) == 1
            if endpoint == 'ops':
                assert journal.pending_wire(live) is not None
                assert live.execute('SELECT COUNT(*) FROM kanban_sync_outbox').fetchone()[0] > 0
            else:
                assert journal.snapshot(live)['settled'] == '1'
            # Reconciliation may observe the already-applied mutation/ACK; it
            # must not re-send that operation after the unreadable answer.
            broken[0] = False
            client.push(live, hub, 'default', 'macbook', primary=False)
            assert sum(path.endswith('/replay/ops') for _, path, _, _ in requests) == 1
            assert journal.pending_wire(live) is None
            assert journal.snapshot(live)['settled'] == '1'
        finally:
            hub.close()
