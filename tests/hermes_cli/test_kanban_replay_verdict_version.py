"""Verdict versions are validated before SQLite settlement, including HTTP recovery."""
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_replay_journal as journal
from hermes_cli import kanban_replay_reconcile as reconcile
from hermes_cli import kanban_replay_reply as reply
from hermes_cli import kanban_source_clones
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import test_kanban_replay_sender as helpers
from tests.hermes_cli.test_kanban_replay_http_session import AckReceiver, serving
from tests.hermes_cli.test_kanban_replay_reconcile import local, pending, settled_remote, slot

live = helpers.live
INVALID = [2 ** 70, -(2 ** 63) - 1, -1, [3], {'value': 3}, '3', 3.5, True, None]


def snapshots(version, *, absent=False):
    verdict = dict(client_op_id='operation-one', status='applied')
    if not absent:
        verdict['version'] = version
    remote = settled_remote()
    remote['last_verdict'] = json.dumps(verdict)
    wire = dict(client_op_id='operation-one', replay=dict(sequence='1', payload_sha256='a' * 64))
    result = dict(verdict, consumed=True, sequence='1', h='1', payload_sha256='a' * 64)
    response = dict(protocol='processed_prefix1', results=[result])
    return verdict, remote, wire, response


@pytest.mark.parametrize('version', INVALID)
def test_invalid_version_refuses_inspection_and_consuming_reply(version):
    _, remote, wire, response = snapshots(version)
    state = local(pending=pending())
    with pytest.raises(ValueError, match='invalid retained verdict version'):
        reconcile.reconcile(state, remote)
    assert reply.consumed_slot(response, wire, slot(), state) is None


@pytest.mark.parametrize('version,absent', [(None, True), (0, False), (3, False),
                                          (2 ** 63 - 1, False)])
def test_storable_integer_and_absent_versions_keep_both_settlement_paths(version, absent):
    verdict, remote, wire, response = snapshots(version, absent=absent)
    state = local(pending=pending())
    assert reconcile.reconcile(state, remote) == dict(action='record', verdict=verdict)
    candidate = reply.consumed_slot(response, wire, slot(), state)
    assert candidate is not None
    assert reconcile.reconcile(state, candidate) == dict(action='record', verdict=verdict)


class VersionReceiver(AckReceiver):
    def __init__(self, version):
        super().__init__()
        self.version = version

    def _call(self, method, path, body=None):
        if '/changes?' in path:
            return dict(changes=[], next=0, head=0, more=False)
        response = super()._call(method, path, body)
        if path.endswith('/replay/ops'):
            verdict = json.loads(self.slot['last_verdict'])
            verdict['version'] = self.version
            self.slot['last_verdict'] = json.dumps(verdict, separators=(',', ':'))
            response['results'][0]['version'] = self.version
        return response


@pytest.mark.parametrize('order', ['broken-first', 'healthy-first'])
@pytest.mark.parametrize('version', [3, 2 ** 70, [3]])
def test_http_version_fault_keeps_pending_and_both_board_pulls(live, tmp_path, monkeypatch, version, order):
    kb.create_task(live, title='version response', assignee='worker')
    with kbc.write_txn(live):
        live.execute("DELETE FROM kanban_sync_outbox WHERE table_name != 'tasks'")
    broken_path = Path(live.execute('PRAGMA database_list').fetchone()[2])
    healthy_path = tmp_path / 'healthy.db'
    with kbc.connect(healthy_path) as healthy:
        client.ensure_identity(healthy, 'healthy', 'macbook')
        sync.set_mode(healthy, 'shadow')
    monkeypatch.setattr(client, 'publish_pickup_if_changed', lambda hub: None)
    monkeypatch.setattr(kanban_source_clones, 'attest_if_due', lambda hub: None)
    boards = [('default', broken_path), ('healthy', healthy_path)]
    if order == 'healthy-first':
        boards.reverse()
    receiver = VersionReceiver(version)
    with serving(live, receiver) as (url, requests, _):
        hub = client.Hub(client.Identity(url, 'fixture-key', 'macbook'))
        frozen = None
        try:
            for _round in range(2):
                start = len(requests)
                results = client.sync_all_once(hub, boards=boards)
                assert [result.board for result in results] == [board for board, _ in boards]
                by_board = {result.board: result for result in results}
                assert not by_board['healthy'].error
                pulls = {p.split('/')[3] for _, p, _, _ in requests[start:] if '/changes?' in p}
                assert pulls == {'default', 'healthy'}
                if version == 3:
                    assert not by_board['default'].error
                    assert journal.snapshot(live)['settled'] == '1'
                    assert journal.pending_wire(live) is None
                else:
                    assert 'invalid retained verdict version' in by_board['default'].error
                    assert journal.snapshot(live)['settled'] == '0'
                    current = journal.pending_wire(live)
                    assert current is not None
                    if frozen is not None:
                        assert current == frozen
                    frozen = current
                    row = live.execute('SELECT state FROM kanban_sync_outbox').fetchone()
                    assert row is not None and row['state'] == 'pending'
        finally:
            hub.close()
    assert sum(p.endswith('/replay/ops') for _, p, _, _ in requests) == 1
