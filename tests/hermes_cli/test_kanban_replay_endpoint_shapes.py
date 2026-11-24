"""Malformed strict endpoint envelopes stay board-local faults.

HTTP 200 with a non-object capability or a missing serving slot must not
escape as AttributeError/KeyError or abort a later healthy board.
"""
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_source_clones
from hermes_cli import kanban_sync_client as client
from hermes_cli import kanban_replay_journal as journal
from hermes_cli.kanban_replay_reply import capability_envelope, slot_envelope
from tests.hermes_cli.test_kanban_replay_http_session import serving, AckReceiver
from tests.hermes_cli.test_kanban_replay_sender import live

__all__ = ['live']


def test_envelope_helpers_refuse_non_object_payloads():
    with pytest.raises(ValueError, match='replay capability unavailable'):
        capability_envelope([])
    with pytest.raises(ValueError, match='replay serving envelope malformed'):
        slot_envelope([])
    with pytest.raises(ValueError, match='replay serving envelope malformed'):
        slot_envelope({})
    with pytest.raises(ValueError, match='replay serving envelope malformed'):
        slot_envelope({'slot': []})
    assert slot_envelope({'slot': {'mode': 'STRICT'}}) == {'mode': 'STRICT'}
    assert capability_envelope({'processed_prefix': True})['processed_prefix'] is True


@pytest.mark.parametrize('endpoint,body', [
    ('capability', []),
    ('inspect', {}),
    ('inspect', {'slot': []}),
    ('session', {}),
    ('session', {'slot': []}),
])
def test_malformed_endpoint_is_typed_board_local_refusal(live, endpoint, body):
    kb.create_task(live, title='malformed envelope custody', assignee='worker')
    peer = AckReceiver()
    before = journal.snapshot(live)

    def encode(path, reply):
        return json.dumps(body if path.endswith('/' + endpoint) else reply).encode()

    with serving(live, peer, encode_reply=encode) as (url, _requests, _):
        hub = client.Hub(client.Identity(url, 'fixture-key', 'display'))
        try:
            with pytest.raises((client.ReplayFault, client.HubError, client.SyncUnavailable)):
                client.push(live, hub, 'default', 'macbook', primary=False, max_batches=1)
            assert journal.snapshot(live) == before
            assert not peer.sent
        finally:
            hub.close()


@pytest.mark.parametrize('malformed', [True, False])
def test_missing_inspection_envelope_does_not_abort_later_board(live, tmp_path, monkeypatch, malformed):
    kb.create_task(live, title='broken first board', assignee='worker')
    broken_path = Path(live.execute('PRAGMA database_list').fetchone()[2])
    healthy_path = tmp_path / 'healthy.db'
    with kbc.connect(healthy_path) as healthy:
        client.ensure_identity(healthy, 'healthy', 'macbook')
        sync.set_mode(healthy, 'shadow')
    peer = AckReceiver()
    original = peer._call

    def route(method, path, body=None):
        if '/changes' in path:
            return dict(changes=[], next=0, head=0, more=False)
        if malformed and '/default/replay/inspect' in path:
            return {}
        return original(method, path, body)

    peer._call = route
    monkeypatch.setattr(client, 'publish_pickup_if_changed', lambda hub: None)
    monkeypatch.setattr(kanban_source_clones, 'attest_if_due', lambda hub: None)
    with serving(live, peer) as (url, requests, _):
        hub = client.Hub(client.Identity(url, 'fixture-key', 'display'))
        try:
            results = client.sync_all_once(hub, boards=[('default', broken_path), ('healthy', healthy_path)])
            assert [r.board for r in results] == ['default', 'healthy']
            assert bool(results[0].error) is malformed
            assert results[1].error is None
            assert any('/healthy/' in p for _, p, _, _ in requests)
        finally:
            hub.close()


def test_malformed_ack_envelope_is_typed_board_local_refusal(live):
    kb.create_task(live, title='malformed ack envelope', assignee='worker')
    peer = AckReceiver()

    def encode(path, reply):
        return json.dumps({} if path.endswith('/ack') else reply).encode()

    with serving(live, peer, encode_reply=encode) as (url, requests, _):
        hub = client.Hub(client.Identity(url, 'fixture-key', 'display'))
        try:
            with pytest.raises((client.ReplayFault, client.HubError, client.SyncUnavailable)):
                client.push(live, hub, 'default', 'macbook', primary=False, max_batches=1)
            assert len(peer.sent) == 1
            assert any(path.endswith('/ack') for _, path, _, _ in requests)
        finally:
            hub.close()
