"""Canonical safety evidence cannot consume an unresolved release replay slot."""
from contextlib import contextmanager
import json
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_retain as service, kanban_retain_transport as transport
from hermes_cli import kanban_replay_journal as journal, kanban_sync_client as client
from tests.hermes_cli import test_kanban_replay_controls as support
from tests.hermes_cli import test_kanban_retain_transport as retained

live = support.live


def safety_evidence(conn, receiver, tid, owned):
    run_uid = owned['run_uid']
    proof = dict(cause='safety_refusal', source_result_event_uid='safety-source')
    rows = {
        ('task_runs', run_uid): dict(task_id=tid, outcome='safety_refused', ended_at=2, metadata=json.dumps(proof)),
        ('tasks', tid): dict(uid=tid, version=5, status='triage', current_run_uid=None),
        ('task_events', 'safety-source'): dict(uid='safety-source', task_id=tid, run_uid=run_uid,
            kind='worker_result', version=1, origin_gateway='macbook', payload=json.dumps(dict(
                failed=True, failure_reason='content_policy_blocked', failure_retryable=False))),
        ('task_events', 'safety-annotation'): dict(uid='safety-annotation', task_id=tid, run_uid=run_uid,
            kind='decision_hold_restricted', payload=json.dumps(dict(proof, episode='safety-episode'))),
    }
    with kbc.write_txn(conn):
        rid = conn.execute('SELECT id FROM task_runs WHERE uid=?', (run_uid,)).fetchone()[0]
        kb._append_event(conn, tid, 'decision_hold_restricted', proof, run_id=rid)
        conn.execute("UPDATE task_events SET uid='safety-annotation' WHERE task_id=? AND kind='decision_hold_restricted'", (tid,))
    receiver.ident = SimpleNamespace(gateway='macbook')
    ordinary = receiver.row_evidence
    def evidence(board, table, uid):
        assert not conn.in_transaction
        if (table, uid) not in rows:
            return ordinary(board, table, uid)
        return dict(board=board, table=table, uid=uid, state='live', head=10, row=rows[table, uid])
    receiver.row_evidence = evidence


def source(conn, owned):
    return dict(conn.execute('SELECT * FROM kanban_sync_outbox WHERE op_id=?', (owned['op_id'],)).fetchone())


def test_safety_recovery_keeps_lost_reply_release_until_exact_slot_settles(live, monkeypatch):
    tid, owned, _ = support.reserve(live, 'release')
    receiver = support.Controls()
    receiver.lose_reply = True
    with pytest.raises(OSError, match='reply lost'):
        client.push(live, receiver, 'default', 'macbook', primary=True, max_batches=1)
    reopened = support.sender.reopen(live)
    try:
        safety_evidence(reopened, receiver, tid, owned)
        actor = SimpleNamespace(task=tid, run_uid=owned['run_uid'])
        projections = []
        monkeypatch.setattr(transport, 'project', lambda *args: projections.append(args[-1]))
        before = transport.command(reopened, owned['run_uid'])
        frozen, original = journal.pending_wire(reopened), source(reopened, owned)
        # This is the production retain release-first branch, not a direct DELETE probe.
        with pytest.raises(ValueError, match='release still in replay custody'):
            service._primary(reopened, receiver, 'default', actor, retained.PAYLOAD)
        assert transport.command(reopened, owned['run_uid']) == before
        assert source(reopened, owned) == original and journal.pending_wire(reopened) == frozen
        assert not projections and len(receiver.sent) == 1 and receiver.slot['a'] == '0'
        receiver.lose_reply = False
        client.push(reopened, receiver, 'default', 'macbook', primary=True, max_batches=1)
        assert journal.pending_wire(reopened) is None and receiver.slot['a'] == '1'
        result = service._primary(reopened, receiver, 'default', actor, retained.PAYLOAD)
        assert result['disposition'] == 'superseded_by_safety_stop' and result['terminal_for_run']
        assert projections == [result] and len(receiver.sent) == 1
        assert transport.command(reopened, owned['run_uid'])['delivery_state'] == 'confirmed'
        assert not reopened.execute('SELECT 1 FROM kanban_sync_outbox WHERE op_id=?', (owned['op_id'],)).fetchone()
    finally:
        reopened.close()


@pytest.mark.parametrize('other_pending', [False, True])
def test_safety_confirmation_rolls_back_and_does_not_hold_other_operation(live, monkeypatch, other_pending):
    from hermes_cli import kanban_retain_safety as safety
    tid, owned, _ = support.reserve(live, 'release')
    receiver = support.Controls()
    if other_pending:
        client.push(live, receiver, 'default', 'macbook', primary=True, max_batches=1)
        kb.create_task(live, title='independent pending', assignee='worker')
        receiver.lose_reply = True
        with pytest.raises(OSError):
            client.push(live, receiver, 'default', 'macbook', primary=False, max_batches=1)
        assert json.loads(journal.pending_wire(live))['client_op_id'] != owned['op_id']
    safety_evidence(live, receiver, tid, owned)
    before = transport.command(live, owned['run_uid'])
    rows = [tuple(r) for r in live.execute('SELECT * FROM kanban_sync_outbox ORDER BY op_id')]
    frozen = journal.pending_wire(live)
    transaction = kb.write_txn
    @contextmanager
    def failed_commit(conn):
        with transaction(conn):
            yield
            raise RuntimeError('safety confirmation before commit')
    with monkeypatch.context() as patch:
        patch.setattr(kb, 'write_txn', failed_commit)
        with pytest.raises(RuntimeError, match='before commit'):
            safety.recover(live, receiver, 'default', owned['run_uid'])
    assert transport.command(live, owned['run_uid']) == before
    assert [tuple(r) for r in live.execute('SELECT * FROM kanban_sync_outbox ORDER BY op_id')] == rows
    assert journal.pending_wire(live) == frozen
    result = safety.recover(live, receiver, 'default', owned['run_uid'])
    assert result['disposition'] == 'superseded_by_safety_stop'
    assert transport.command(live, owned['run_uid'])['delivery_state'] == 'confirmed'
    assert journal.pending_wire(live) == frozen
    assert not live.execute('SELECT 1 FROM kanban_sync_outbox WHERE op_id=?', (owned['op_id'],)).fetchone()
