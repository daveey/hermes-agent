"""Malformed Board payloads stay on one board in sync_all_once."""
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_replay_existence as existence
from hermes_cli import kanban_replay_journal as journal
from hermes_cli import kanban_source_clones
from hermes_cli import kanban_sync_client as client
from hermes_cli.kanban_replay_reply import mapping_envelope, row_evidence_envelope
from tests.hermes_cli.test_kanban_replay_http_session import serving, AckReceiver
from tests.hermes_cli import test_kanban_replay_sender as helpers

live = helpers.live

# Inventory of Board parses in the multi-board loop: capability/inspect/session
# envelopes, ACK, current-row evidence, feed changes, and error bodies.
SHAPES = (
    ('capability', []),
    ('inspect', {}),
    ('inspect', {'slot': []}),
    ('session', {}),
    ('session', {'slot': []}),
    ('ack', {}),
    ('evidence', []),
    ('evidence', None),
    ('evidence', 'x'),
    ('changes', []),
    ('changes', None),
)


def test_mapping_helpers_refuse_non_objects():
    with pytest.raises(ValueError, match='envelope malformed'):
        mapping_envelope([], 'current-row')
    with pytest.raises(ValueError, match='envelope malformed'):
        row_evidence_envelope(None)
    assert row_evidence_envelope({'state': 'live'})['state'] == 'live'


@pytest.mark.parametrize('endpoint,body', SHAPES)
def test_malformed_board_payload_does_not_abort_later_board(live, tmp_path, monkeypatch, endpoint, body):
    kb.create_task(live, title='broken first board', assignee='worker')
    if endpoint == 'evidence':
        sync.set_mode(live, 'primary')
    broken_path = Path(live.execute('PRAGMA database_list').fetchone()[2])
    healthy_path = tmp_path / 'healthy.db'
    with kbc.connect(healthy_path) as healthy:
        client.ensure_identity(healthy, 'healthy', 'macbook')
        sync.set_mode(healthy, 'shadow')
    peer = AckReceiver()
    original = peer._call
    frozen = journal.snapshot(live)

    def route(method, path, body_arg=None):
        if endpoint == 'changes' and '/default/' in path and '/changes' in path:
            return body
        if endpoint == 'evidence' and '/default/' in path and '/evidence' in path:
            return body
        if endpoint not in ('changes', 'evidence') and f'/default/replay/{endpoint}' in path:
            return body
        if '/changes' in path:
            return dict(changes=[], next=0, head=0, more=False)
        return original(method, path, body_arg)

    peer._call = route
    monkeypatch.setattr(client, 'publish_pickup_if_changed', lambda hub: None)
    monkeypatch.setattr(kanban_source_clones, 'attest_if_due', lambda hub: None)
    with serving(live, peer) as (url, requests, _):
        hub = client.Hub(client.Identity(url, 'fixture-key', 'display'))
        try:
            results = client.sync_all_once(hub, boards=[('default', broken_path), ('healthy', healthy_path)])
            assert [r.board for r in results] == ['default', 'healthy']
            assert results[0].error
            assert results[1].error is None
            if endpoint == 'changes':
                assert 'HubError' in results[0].error and 'envelope malformed' in results[0].error
            if endpoint == 'evidence':
                assert 'CurrentRowHold' in results[0].error or 'ValueError' in results[0].error
            assert any('/healthy/' in p for _, p, _, _ in requests)
            if endpoint == 'evidence':
                assert journal.snapshot(live)['pending'] is not None
            else:
                assert journal.snapshot(live)['pending'] == frozen['pending']
        finally:
            hub.close()


def test_malformed_evidence_same_pending_repairs_after_resume(live):
    from tests.hermes_cli.test_kanban_replay_primary import Primary
    tid = kb.create_task(live, title='repair after malformed evidence', assignee='worker')
    receiver = Primary(live)
    receiver.evidence = []
    with pytest.raises((client.ReplayFault, client.HubError, client.SyncUnavailable, OSError, ValueError)):
        client.push(live, receiver, 'default', 'macbook', primary=True, max_batches=1)
    frozen = journal.pending_wire(live)
    assert frozen
    fault = existence.fault(live)
    assert fault is not None
    existence.resume(live, fault['op_id'], 'owner checked serving Board current-row envelope')
    receiver.evidence = dict(board='default', table='tasks', uid=tid, state='live', head=4,
                             row=dict(uid=tid, title='restored', status='todo', version=4))
    result = client.push(live, receiver, 'default', 'macbook', primary=True, max_batches=1)
    assert result.merged == 1
    assert journal.pending_wire(live) is None
    assert existence.fault(live) is None
    assert len(receiver.sent) == 1


def test_synthetic_parent_non_object_evidence_is_current_row_hold(live):
    import json
    tid = kb.create_task(live, title='parent version unavailable', assignee='worker')
    with kbc.write_txn(live):
        live.execute('DELETE FROM kanban_sync_outbox WHERE 1=1')
    kb.add_comment(live, tid, 'author', 'child')

    class MissingVersion(helpers.Receiver):
        def _call(self, method, path, body=None):
            result = super()._call(method, path, body)
            if path.endswith('/ops'):
                encoded = self.slot['last_verdict']
                assert isinstance(encoded, str)
                verdict = json.loads(encoded)
                verdict.pop('version')
                self.slot['last_verdict'] = json.dumps(verdict)
            return result

        def row_evidence(self, board, table, uid):
            return []

    with pytest.raises((client.ReplayFault, client.HubError, client.SyncUnavailable, ValueError)):
        client.push(live, MissingVersion(), 'default', 'macbook', primary=False, max_batches=1)
