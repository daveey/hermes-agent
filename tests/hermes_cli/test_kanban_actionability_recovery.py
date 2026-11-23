"""Explicit recovery uses real custody/outbox/confirmation; never original admission."""
import copy
import json

import pytest

from hermes_cli import kanban_actionability_recovery as recovery
from hermes_cli import kanban_actionability_transport as transport
from hermes_cli import kanban_db_connect as kbc
from tests.hermes_cli import test_kanban_actionability_transport as support
from tests.hermes_cli import test_kanban_actionability_observation as observations

live = support.live


class Hub(observations.Hub):
    def __init__(self, conn, tid):
        super().__init__()
        self.conn, self.tid, self.sent, self.rows = conn, tid, [], {}
        self.ident = type(self.ident)(gateway='fixture')
        source = conn.execute("SELECT uid,created_at FROM task_events WHERE task_id=? AND kind='created'", (tid,)).fetchone()
        self.now = source['created_at'] + 10
        data = observations.inventory(tid)
        data['task']['created_on'] = 'fixture'
        data['events'][0].update(uid=source['uid'], created_at=source['created_at'])
        self.data = {tid: data}
        self.answer = 'applied'
        self.reason = 'actionability_source_changed'
        self.destination_kind = 'unavailable'

    def actionability_capability(self, board):
        assert not self.conn.in_transaction
        return {'actionability_record': 1, 'frozen_replay': 1, 'diagnostic_only': True,
                'hlc': f'{self.now * 1000:013d}-0000-server'}

    def ops(self, board, sent):
        assert not self.conn.in_transaction
        self.sent.extend(copy.deepcopy(sent))
        wire = sent[0]
        if self.answer == 'lost':
            raise OSError('reply lost')
        receipt = {'version': 1, 'request_uid': wire['client_op_id'],
                   'request_sha256': transport.digest(transport.canonical(wire)),
                   'home_gateway': 'fixture', 'contract': wire['payload']}
        uid = wire['contract_uid']
        if wire['phase'] == 'escalation':
            uid = wire['receipt_uid']
            envelope = {'root_task': self.tid, 'root_episode': wire['payload']['facts']['root_episode'],
                        'contract_key': wire['payload']['contract_key'], 'contract_event_uid': wire['contract_uid'],
                        'notice_event_uid': wire['notice_uid'], 'deadline_at': wire['payload']['deadline_at'],
                        'kind': 'administrative_only'}
            receipt.update(contract_event_uid=wire['contract_uid'], notice_event_uid=wire['notice_uid'],
                           destination={'task_id': self.tid, 'kind': self.destination_kind},
                           envelope=envelope, outcome='route_unavailable' if self.destination_kind == 'unavailable' else 'recorded')
            self._event(wire['notice_uid'], 'decision_requested', {'actionability': envelope})
        self._event(uid, 'actionability_contract' if wire['phase'] == 'contract' else 'actionability_escalation_recorded', receipt)
        return {'results': [{'client_op_id': wire['client_op_id'], 'status': self.answer,
                             'reason': self.reason}]}

    def _event(self, uid, kind, payload):
        self.rows[uid] = {'uid': uid, 'task_id': self.tid, 'kind': kind, 'version': 1,
                          'origin_gateway': 'fixture', 'deleted_at': None,
                          'ord': len(self.rows) + 2, 'created_at': self.now, 'payload': json.dumps(payload)}

    def row_evidence(self, board, table, uid):
        return {'board': board, 'table': table, 'uid': uid, 'state': 'live', 'row': self.rows[uid]}


def setup(conn):
    tid, _ = support.contract(conn)
    hub = Hub(conn, tid)
    obligation = dict(owner={'kind': 'assignee', 'refs': ['default']}, action='repair_lifecycle_contract',
                      instruction='Record administrative obligation; no scope release',
                      prerequisite='Actual source evidence', trigger='Explicit accountable reconsideration',
                      evidence=['operator recorded commitment'], deadline_at=hub.now + 600)
    return tid, hub, obligation


def preview(conn, hub, tid, obligation):
    return recovery.preview(conn, hub, 'default', tid, **obligation)


def apply(conn, hub, value):
    return recovery.apply(conn, hub, 'default', value['op_id'], value['request_sha256'])


def test_preview_freezes_without_issuing_and_apply_confirms_no_task_changes(live):
    tid, hub, obligation = setup(live)
    before = dict(live.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone())
    value = preview(live, hub, tid, obligation)
    assert value['issued'] is False and not hub.sent
    assert not live.execute("SELECT 1 FROM kanban_sync_outbox WHERE op='actionability_record'").fetchone()
    assert preview(live, hub, tid, obligation) == value
    result = apply(live, hub, value)
    assert result['delivery_state'] == 'confirmed' and result['execution_authority'] is False
    assert dict(live.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone()) == before
    assert hub.sent == [value['request']]


@pytest.mark.parametrize('field,value', [('deadline_at', 1), ('instruction', 'different bytes'),
                                        ('evidence', ['different evidence'])])
def test_same_identity_cannot_renew_or_replace_preview(live, field, value):
    tid, hub, obligation = setup(live)
    preview(live, hub, tid, obligation)
    if field == 'deadline_at':
        value = obligation[field] + 1
    with pytest.raises(ValueError, match='frozen'):
        preview(live, hub, tid, {**obligation, field: value})
    assert not hub.sent


def test_wrong_digest_and_board_issue_nothing(live):
    tid, hub, obligation = setup(live)
    value = preview(live, hub, tid, obligation)
    for board, sha in [('default', 'wrong'), ('other', value['request_sha256'])]:
        with pytest.raises(ValueError, match='mismatch'):
            recovery.apply(live, hub, board, value['op_id'], sha)
    assert not hub.sent and not transport.command(live, value['op_id'])


def test_changed_canonical_source_stales_without_reserve(live):
    tid, hub, obligation = setup(live)
    value = preview(live, hub, tid, obligation)
    hub.data[tid]['events'][0]['version'] = 2
    with pytest.raises(ValueError, match='stale'):
        apply(live, hub, value)
    assert not transport.command(live, value['op_id']) and not hub.sent
    hub.data[tid]['events'][0]['version'] = 1
    with pytest.raises(ValueError, match='stale'):
        apply(live, hub, value)


def test_unknown_delivery_replays_frozen_bytes_without_renewal(live, monkeypatch):
    tid, hub, obligation = setup(live)
    value = preview(live, hub, tid, obligation)
    hub.answer = 'lost'
    with pytest.raises(OSError):
        apply(live, hub, value)
    assert transport.command(live, value['op_id'])['delivery_state'] == 'pending'
    # Simulated restart/changed current source must not replace an already issued request.
    monkeypatch.setattr(recovery.observation, 'observe', lambda *a: pytest.fail('re-derived uncertain request'))
    hub.answer = 'applied'
    hub.now += 100
    apply(live, hub, value)
    assert hub.sent == [value['request'], value['request']]
    assert transport.command(live, value['op_id'])['request_sha256'] == value['request_sha256']


def test_reserve_and_preview_state_rollback_together(live, monkeypatch):
    tid, hub, obligation = setup(live)
    value = preview(live, hub, tid, obligation)
    original = transport.reserve
    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('crash before commit')
    monkeypatch.setattr(transport, 'reserve', fail)
    with pytest.raises(RuntimeError):
        apply(live, hub, value)
    assert not transport.command(live, value['op_id']) and not hub.sent
    assert live.execute('SELECT state FROM kanban_actionability_previews').fetchone()[0] == 'previewed'


@pytest.mark.parametrize('reason', ['actionability_source_changed', 'actionability_command_conflict', 'forbidden', 'invalid'])
def test_definitive_confirm_rejection_is_not_unknown_and_never_resent(live, reason):
    tid, hub, obligation = setup(live)
    value = preview(live, hub, tid, obligation)
    hub.answer, hub.reason = 'rejected', reason
    with pytest.raises(ValueError, match='reject'):
        apply(live, hub, value)
    assert transport.command(live, value['op_id'])['delivery_state'] == 'rejected'
    with pytest.raises(ValueError, match='rejected'):
        apply(live, hub, value)
    assert len(hub.sent) == 1


def test_escalation_requires_confirmed_root_and_due_original_deadline(live):
    tid, hub, obligation = setup(live)
    value = preview(live, hub, tid, obligation)
    args = (live, hub, 'default', value['op_id'], value['request_sha256'])
    with pytest.raises(ValueError, match='confirmed'):
        recovery.escalate(*args)
    apply(live, hub, value)
    with pytest.raises(ValueError, match='not_due'):
        recovery.escalate(*args)
    assert len(hub.sent) == 1
    hub.now = obligation['deadline_at']
    result = recovery.escalate(*args)
    assert result['outcome'] == 'route_unavailable' and result['execution_authority'] is False
    assert hub.sent[-1]['payload'] == value['request']['payload']


def test_local_board_mismatch_refuses_before_network(live, monkeypatch):
    tid, hub, obligation = setup(live)
    with kbc.write_txn(live):
        live.execute("UPDATE kanban_sync_state SET board_id='different'")
    monkeypatch.setattr(hub, 'actionability_capability', lambda *a: pytest.fail('cross-board network'))
    with pytest.raises(ValueError, match='board'):
        preview(live, hub, tid, obligation)
    assert not hub.sent


@pytest.mark.parametrize('status,reason', [('deferred', 'actionability_not_due'),
                                            ('deferred', 'actionability_contract_pending'),
                                            ('rejected', 'storage_error')])
def test_canonical_prerequisite_or_storage_reply_preserves_replayable_custody(live, status, reason):
    tid, hub, obligation = setup(live)
    value = preview(live, hub, tid, obligation)
    hub.answer, hub.reason = status, reason
    with pytest.raises(ValueError, match='unknown'):
        apply(live, hub, value)
    assert transport.command(live, value['op_id'])['delivery_state'] != 'rejected'
    hub.answer = 'applied'
    assert apply(live, hub, value)['delivery_state'] == 'confirmed'


@pytest.mark.parametrize('change', ['self', 'terminal-contained'])
def test_invalid_task_owner_refuses_before_preview(live, change):
    tid, hub, obligation = setup(live)
    pid = tid if change == 'self' else 't_parent'
    if pid != tid:
        hub.data[pid] = observations.inventory(pid, 'done')
        hub.data[pid]['task']['containment_episode'] = pid + '_created'
    hub.pages[0].update(rows=[{'uid': 'link', 'child_id': tid, 'parent_id': pid}], next='link')
    with pytest.raises(ValueError, match='owner'):
        preview(live, hub, tid, {**obligation, 'owner': {'kind': 'task', 'refs': [pid]}})
    assert not hub.sent


@pytest.mark.parametrize('change', [
    {'owner': {'kind': 'unavailable', 'refs': []}},
    {'owner': {'kind': 'assignee', 'refs': ['wrong']}},
    {'owner': {'kind': 'task', 'refs': ['unlinked']}},
    {'deadline_at': True}, {'deadline_at': None}, {'instruction': '\ufeff'},
    {'instruction': '\U0001f600' * 501}, {'evidence': ['x'] * 9}, {'action': 'release_scope'},
])
def test_invalid_or_unsupported_obligation_never_issues(live, change):
    tid, hub, obligation = setup(live)
    with pytest.raises(ValueError):
        preview(live, hub, tid, {**obligation, **change})
    assert not hub.sent
