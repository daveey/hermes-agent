"""Field/value cross-product through HTTP and multi-board dispatch in both orders.

Some probe values are valid for optional text/bools or decimal-string counters.
Those are positive controls, not grounds for changing the wire protocol.
"""
import json

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_replay_journal as journal
from hermes_cli import kanban_source_clones
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import test_kanban_replay_sender as helpers
from tests.hermes_cli.test_kanban_replay_http_session import AckReceiver, serving

live = helpers.live
VALUES = [2 ** 70, [3], None, '3', True, -1, 1.5]
FIELDS = {
    'capability': ('processed_prefix', 'board_incarnation', 'gateway_principal', 'deferred_observation'),
    'slot': ('mode', 'h', 'a', 'generation', 'session', 'board_incarnation', 'gateway_principal',
             'journal_id', 'last_op_id', 'last_payload_sha256', 'last_verdict'),
    'verdict': ('client_op_id', 'status', 'version', 'row', 'source_predecessor', 'reason', 'detail', 'deferred'),
    'reply': ('protocol', 'results', 'consumed', 'outcome_unknown', 'dup', 'sequence', 'h', 'payload_sha256'),
    'evidence': ('board', 'table', 'uid', 'state', 'head', 'row'),
    'image': ('uid', 'version', 'deleted_at', 'hlc', 'origin_gateway', 'title', 'created_at'),
}
REPLY_TRANSPORT_ONLY = {('verdict', 'row'), ('verdict', 'source_predecessor')}
CASES = [(envelope, field) for envelope, fields in FIELDS.items() for field in fields]


class FieldReceiver(AckReceiver):
    def __init__(self, envelope, field, value, tid):
        super().__init__()
        self.envelope, self.field, self.value, self.tid = envelope, field, value, tid
        self.changed_slot = False

    def _call(self, method, path, body=None):
        if '/changes?' in path:
            return dict(changes=[], next=0, head=0, more=False)
        if '/evidence' in path:
            image = dict(uid=self.tid, version=7, title='current', status='todo', created_at=1)
            result = dict(board='default', table='tasks', uid=self.tid, state='live', head=1, row=image)
            target = image if self.envelope == 'image' else result
            target[self.field] = self.value
            return result
        if self.envelope == 'slot' and path.endswith('/inspect') and not self.changed_slot:
            self.slot[self.field] = self.value
            self.changed_slot = True
        result = super()._call(method, path, body)
        if self.envelope == 'capability' and path.endswith('/capability'):
            result[self.field] = self.value
        if path.endswith('/replay/ops'):
            if self.envelope == 'verdict':
                verdict = json.loads(self.slot['last_verdict'])
                verdict[self.field] = self.value
                self.slot['last_verdict'] = json.dumps(verdict)
                result['results'][0][self.field] = self.value
            elif self.envelope == 'reply':
                target = result if self.field in ('protocol', 'results') else result['results'][0]
                target[self.field] = self.value
        return result


def valid_control(envelope, field, value):
    if envelope == 'capability':
        return ((field == 'processed_prefix' and value is True) or
                (field == 'deferred_observation' and (value is None or value == '3')))
    if envelope == 'slot':
        return ((field in ('last_op_id', 'last_payload_sha256', 'last_verdict') and value is None) or
                (field == 'session' and value == '3'))
    if envelope == 'verdict':
        return ((field in ('row', 'source_predecessor', 'deferred') and value is None) or
                (field in ('reason', 'detail') and (value is None or value == '3')))
    if envelope == 'reply':
        # This reply cannot certify consumption; authoritative inspect is required.
        return True
    if envelope == 'image':
        return ((field == 'deleted_at' and value is None) or
                (field in ('hlc', 'origin_gateway') and (value is None or value == '3')) or
                (field == 'title' and value == '3') or
                (field == 'created_at' and type(value) is int and value == -1))
    return False


def check_field(live, tmp_path, monkeypatch, envelope, field):
    healthy_path = tmp_path / 'healthy.db'
    with kbc.connect(healthy_path) as healthy:
        client.ensure_identity(healthy, 'healthy', 'macbook')
        sync.set_mode(healthy, 'shadow')
    monkeypatch.setattr(client, 'publish_pickup_if_changed', lambda hub: None)
    monkeypatch.setattr(kanban_source_clones, 'attest_if_due', lambda hub: None)
    for index, value in enumerate(VALUES):
        for reverse in (False, True):
            path = tmp_path / f'{index}-{reverse}.db'
            with kbc.connect(path) as conn:
                live.backup(conn)
                tid = kb.create_task(conn, title='field matrix', assignee='worker')
                with kbc.write_txn(conn):
                    conn.execute("DELETE FROM kanban_sync_outbox WHERE table_name != 'tasks'")
                if envelope in ('evidence', 'image'):
                    sync.set_mode(conn, 'primary')
                peer = FieldReceiver(envelope, field, value, tid)
                with serving(conn, peer) as (url, requests, _):
                    hub = client.Hub(client.Identity(url, 'fixture-key', 'macbook'))
                    boards = [('default', path), ('healthy', healthy_path)]
                    if reverse:
                        boards.reverse()
                    try:
                        results = client.sync_all_once(hub, boards=boards)
                    finally:
                        hub.close()
                context = (envelope, field, value, reverse, [(r.board, r.error) for r in results])
                assert [r.board for r in results] == [board for board, _ in boards], context
                by_board = {r.board: r for r in results}
                assert by_board['healthy'].error is None, context
                pulls = {p.split('/')[3] for _, p, _, _ in requests if '/changes?' in p}
                assert pulls == {'default', 'healthy'}, context
                if valid_control(envelope, field, value):
                    assert not by_board['default'].error, context
                    assert journal.pending_wire(conn) is None, context
                    assert journal.snapshot(conn)['settled'] == '1', context
                    if envelope == 'reply' and not (field == 'consumed' and value is True):
                        assert peer.calls.count('inspect') >= 2, context
                else:
                    assert by_board['default'].error, context
                    # The returned board fault is separate from healthy feed progress.
                    # The ops reply strips row/source_predecessor as transport fields, so
                    # that case may settle; the corrupt retained slot verdict is then
                    # refused at the next authenticated inspect, still as this board's fault.
                    if (envelope, field) not in REPLY_TRANSPORT_ONLY:
                        assert journal.snapshot(conn)['settled'] == '0', context
                        assert conn.execute('SELECT 1 FROM kanban_sync_outbox').fetchone(), context
                        if peer.sent:
                            assert journal.pending_wire(conn) is not None, context
                assert len(peer.sent) <= 1, context
