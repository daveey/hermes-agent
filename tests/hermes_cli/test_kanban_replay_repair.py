"""Authorized repair keeps physical custody while issuing fresh normalized wire identities."""
import json

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_replay_journal as journal
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import test_kanban_replay_sender as helpers
from tests.hermes_cli import test_kanban_replay_primary as primary
from tests.hermes_cli import test_kanban_sync_reserved_key as reserved

live = helpers.live
kanban_home = reserved.kanban_home
reserved_live = reserved.live


def row(conn, op_id):
    hit = conn.execute('SELECT * FROM kanban_sync_outbox WHERE op_id=?', (op_id,)).fetchone()
    return dict(hit) if hit else None


def push(conn, receiver, *, primary_mode=False, batches=1):
    try:
        return client.push(conn, receiver, 'default', 'macbook', primary=primary_mode, max_batches=batches)
    except ValueError as exc:
        pending = [tuple(r) for r in conn.execute("SELECT table_name,op,state,row_uid FROM kanban_sync_outbox ORDER BY created_at,rowid")]
        pytest.fail(f'authorized repair cannot reach the next replay identity: {exc}; pending={pending}')


@pytest.mark.parametrize('physical,repair', [('upsert', 'compat'), ('import', 'compat_import')])
def test_schema_refusal_reopen_repairs_then_sequence_two_and_unrelated_progress(live, physical, repair):
    tid = kb.create_task(live, title='repair original', body='retain omitted body', assignee='worker')
    original = live.execute("SELECT op_id FROM kanban_sync_outbox WHERE table_name='tasks'").fetchone()[0]
    with kbc.write_txn(live):
        live.execute('UPDATE kanban_sync_outbox SET op=?,created_at=created_at-1000 WHERE op_id=?',
                     (physical, original))
    receiver = primary.Primary(live)
    receiver.lose_reply = True
    with pytest.raises(OSError, match='reply lost'):
        push(live, receiver, primary_mode=True)
    frozen = journal.pending_wire(live)
    original_row = row(live, original)
    kb.add_comment(live, tid, 'user', 'dependent comment retained')
    other = kb.create_task(live, title='unrelated progress', assignee='worker')
    dependent = [dict(r) for r in live.execute("SELECT * FROM kanban_sync_outbox WHERE table_name='task_comments'")]
    assert dependent
    assert row(live, original) == original_row
    assert not live.execute("SELECT 1 FROM kanban_sync_outbox WHERE op IN ('compat','compat_import')").fetchone()
    verdict = json.loads(receiver.slot['last_verdict'])
    verdict.update(status='rejected', reason='forbidden_field', detail='not writable on tasks: body')
    receiver.slot['last_verdict'] = json.dumps(verdict)
    reopened = helpers.reopen(live)
    receiver.conn = reopened
    receiver.lose_reply = False
    receiver.evidence = dict(board='default', table='tasks', uid=tid, state='live', head=90,
                             row=dict(uid=tid, title='repair original', status='todo', version=19))
    try:
        assert journal.pending_wire(reopened) == frozen
        assert push(reopened, receiver, primary_mode=True).rejected == 1
        assert receiver.slot['a'] == '1' and len(receiver.sent) == 1
        replacement = dict(reopened.execute('SELECT * FROM kanban_sync_outbox WHERE op=?', (repair,)).fetchone())
        assert replacement['op_id'] != original and row(reopened, original)['state'] == 'rejected'
        assert [row(reopened, r['op_id']) for r in dependent] == dependent
        assert replacement['wire_json'] is None and journal.snapshot(reopened)['settled'] == '1'
        receiver.status = 'applied'
        receiver.lose_reply = True
        with pytest.raises(OSError, match='reply lost'):
            push(reopened, receiver)
        sent = receiver.sent[-1]
        assert sent['client_op_id'] == replacement['op_id'] and sent['replay']['sequence'] == '2'
        assert sent['op'] == 'upsert' and bool(sent.get('import')) == (repair == 'compat_import')
        assert 'body' not in sent['fields']
        assert row(reopened, replacement['op_id'])['op'] == repair
        assert [row(reopened, r['op_id']) for r in dependent] == dependent
        pending = journal.pending_wire(reopened)
        reopened2 = helpers.reopen(reopened)
        receiver.conn = reopened2
        receiver.lose_reply = False
        try:
            assert journal.pending_wire(reopened2) == pending
            assert push(reopened2, receiver).applied == 1 and len(receiver.sent) == 2
            assert receiver.slot['a'] == '2' and row(reopened2, replacement['op_id']) is None
            for _ in range(12):
                push(reopened2, receiver)
            assert any(op['uid'] == other for op in receiver.sent)
            assert any(op['client_op_id'] == dependent[0]['op_id'] for op in receiver.sent)
            assert row(reopened2, original)['state'] == 'rejected'
        finally:
            reopened2.close()
    finally:
        reopened.close()


class ReservedReplay(helpers.Receiver):
    """Replay custody fixture composed with the existing row-contract fake Board."""
    def __init__(self):
        super().__init__()
        self.service = reserved.StrictService()
        self.rows = self.service.hub('macbook')
        self.import_fault = False

    def __getattr__(self, name):
        return getattr(self.rows, name)

    def row_evidence(self, *args, **kwargs):
        return self.rows.row_evidence(*args, **kwargs)

    def _call(self, method, path, body=None):
        if not path.endswith('/ops'):
            return super()._call(method, path, body)
        losing, self.lose_reply = self.lose_reply, False
        reply = super()._call(method, path, body)
        self.lose_reply = losing
        wire, = body['ops']
        if self.import_fault and wire.get('import'):
            result = dict(client_op_id=wire['client_op_id'], status='rejected', reason='storage_error')
        else:
            result, = self.rows.ops('default', [wire])['results']
        self.slot['last_verdict'] = json.dumps({k: v for k, v in result.items() if k != 'row'})
        if losing:
            raise OSError('reply lost after commit')
        return reply


@pytest.mark.parametrize('transient', [False, True])
def test_reserved_key_correction_import_lost_reply_reopen_preserves_receipt(reserved_live, monkeypatch, transient):
    from hermes_cli import kanban_sync_reserved_key as rk
    from tests.hermes_cli.test_kanban_replay_journal import binding
    live = reserved_live
    receiver = ReservedReplay()
    tid, _ = reserved.bad_card(live, receiver.service, monkeypatch)
    original = reserved.key_op(live)
    with kbc.write_txn(live):
        journal.initialize(live, binding(), board_id='default')
    reopened = helpers.reopen(live)
    try:
        rejected = row(reopened, original)
        report = rk.plan(reopened, receiver, 'default', tid, reserved.GOOD, reserved.REASON)
        assert report['eligible'], json.dumps(report)
        applied = rk.apply(reopened, receiver, 'default', tid, reserved.GOOD, report['token'], reserved.REASON)
        assert applied['action'] == 'applied', applied
        receipt = rk.receipt_for_task(reopened, tid)
        replacement = row(reopened, receipt['parent_op_id'])
        assert replacement['op'] == 'import' and replacement['op_id'] != original
        receiver.lose_reply = True
        receiver.import_fault = transient
        with pytest.raises(OSError, match='reply lost'):
            push(reopened, receiver)
        wire = receiver.sent[-1]
        assert wire['replay']['sequence'] == '1' and wire['op'] == 'upsert' and wire['import'] is True
        assert wire['fields']['idempotency_key'] == reserved.GOOD
        pending = journal.pending_wire(reopened)
        assert row(reopened, replacement['op_id'])['op'] == 'import'
        reopened2 = helpers.reopen(reopened)
        receiver.lose_reply = False
        try:
            assert journal.pending_wire(reopened2) == pending
            result = push(reopened2, receiver)
            assert (result.rejected if transient else result.applied) == 1
            assert len(receiver.sent) == 1 and receiver.slot['a'] == '1'
            assert row(reopened2, original) == rejected
            if transient:
                remapped = rk.receipt_for_task(reopened2, tid)
                assert remapped['parent_op_id'] != receipt['parent_op_id']
                assert dict(remapped, parent_op_id=receipt['parent_op_id']) == receipt
                assert row(reopened2, remapped['parent_op_id'])['op'] == 'import'
                receiver.import_fault = False
                with kbc.write_txn(reopened2):
                    reopened2.execute('UPDATE kanban_sync_outbox SET retry_after=0 WHERE op_id=?', (remapped['parent_op_id'],))
                assert push(reopened2, receiver).applied == 1
                assert receiver.sent[-1]['replay']['sequence'] == '2'
                assert receiver.sent[-1]['client_op_id'] == remapped['parent_op_id']
            else:
                assert rk.receipt_for_task(reopened2, tid) == receipt
            assert receiver.service.board('default')['rows']['tasks'][tid]['idempotency_key'] == reserved.GOOD
            assert row(reopened2, replacement['op_id']) is None
        finally:
            reopened2.close()
    finally:
        reopened.close()


def test_transient_refusal_reopen_remaps_only_after_final_and_other_work_flows(live):
    tid = kb.create_task(live, title='transient remap', assignee='worker')
    receiver = helpers.Receiver()
    receiver.lose_reply = True
    with pytest.raises(OSError, match='reply lost'):
        push(live, receiver)
    original = receiver.sent[0]['client_op_id']
    frozen = journal.pending_wire(live)
    with kbc.write_txn(live):
        live.execute('INSERT INTO kanban_sync_omissions (original_op_id,table_name,row_uid,field,compat_op_id) '
                     'VALUES (?,?,?,?,?)', (original, 'tasks', tid, 'title', original))
    other = kb.create_task(live, title='independent intent', assignee='worker')
    assert row(live, original)['state'] == 'pending' and journal.pending_wire(live) == frozen
    verdict = json.loads(receiver.slot['last_verdict'])
    verdict.update(status='rejected', reason='storage_error', detail='temporary storage fault')
    receiver.slot['last_verdict'] = json.dumps(verdict)
    reopened = helpers.reopen(live)
    receiver.lose_reply = False
    try:
        assert push(reopened, receiver).rejected == 1
        assert receiver.slot['a'] == '1' and len(receiver.sent) == 1
        replacement = reopened.execute("SELECT * FROM kanban_sync_outbox WHERE row_uid=? AND state='pending'", (tid,)).fetchone()
        assert replacement is not None, 'durable refusal must enter bounded authorized remap'
        assert replacement['op_id'] != original and replacement['wire_json'] is None
        assert replacement['attempts'] == 1 and replacement['retry_after'] > 0
        assert row(reopened, original) is None
        omission = reopened.execute('SELECT original_op_id,compat_op_id FROM kanban_sync_omissions').fetchone()
        assert tuple(omission) == (original, replacement['op_id'])
        push(reopened, receiver, batches=1)
        assert receiver.sent[-1]['uid'] == other and receiver.sent[-1]['replay']['sequence'] == '2'
        with kbc.write_txn(reopened):
            reopened.execute('UPDATE kanban_sync_outbox SET retry_after=0 WHERE op_id=?', (replacement['op_id'],))
        push(reopened, receiver, batches=1)
        assert receiver.sent[-1]['client_op_id'] == replacement['op_id']
        assert receiver.sent[-1]['replay']['sequence'] == '3'
        assert sum(op['client_op_id'] == original for op in receiver.sent) == 1
    finally:
        reopened.close()
