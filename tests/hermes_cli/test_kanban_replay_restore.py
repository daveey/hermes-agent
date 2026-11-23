"""Real omission restoration is normalized without retiring physical custody early."""
import json

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_sync as sync
from hermes_cli import kanban_replay_journal as journal, kanban_sync_client as client
from tests.hermes_cli import test_kanban_replay_sender as helpers
from tests.hermes_cli import test_kanban_replay_primary as primary

live = helpers.live


@pytest.mark.parametrize('transient', [False, True])
def test_restore_from_schema_omission_loss_reopen_and_delivery(live, transient):
    tid = kb.create_task(live, title='restore source', body='kept local body', assignee='worker')
    receiver = primary.Primary(live)
    receiver.lose_reply = True
    with pytest.raises(OSError):
        client.push(live, receiver, 'default', 'macbook', primary=True, max_batches=1)
    verdict = json.loads(receiver.slot['last_verdict'])
    verdict.update(status='rejected', reason='forbidden_field', detail='not writable on tasks: body')
    receiver.slot['last_verdict'] = json.dumps(verdict)
    receiver.lose_reply = False
    receiver.evidence = dict(board='default', table='tasks', uid=tid, state='live', head=20,
                             row=dict(uid=tid, status='todo', version=19))
    client.push(live, receiver, 'default', 'macbook', primary=True, max_batches=1)
    assert live.execute("SELECT 1 FROM kanban_sync_omissions WHERE field='body' AND state='held'").fetchone()
    receiver.status = 'applied'
    client.push(live, receiver, 'default', 'macbook', primary=False, max_batches=25)
    sequence = str(int(journal.snapshot(live)['settled']) + 1)
    sent_before = len(receiver.sent)
    change = client._row_change('tasks', dict(uid=tid, status='todo', body='old remote body', version=20))
    sync.apply_remote_changes(live, [change], new_cursor=0, board='default', local_pending_wins=True)
    restore = dict(live.execute("SELECT * FROM kanban_sync_outbox WHERE op='restore'").fetchone())
    assert json.loads(restore['fields']) == {'body': 'kept local body'}
    receiver.lose_reply = True
    try:
        with pytest.raises(OSError, match='reply lost'):
            client.push(live, receiver, 'default', 'macbook', primary=True, max_batches=1)
    except ValueError as exc:
        pytest.fail(f'commissioned restore cannot enter replay: {exc}')
    wire = receiver.sent[-1]
    assert wire['client_op_id'] == restore['op_id'] and wire['op'] == 'upsert'
    assert wire['replay']['sequence'] == sequence and journal.snapshot(live)['pending']['source_op'] == 'restore'
    if transient:
        verdict = json.loads(receiver.slot['last_verdict'])
        verdict.update(status='rejected', reason='storage_error')
        receiver.slot['last_verdict'] = json.dumps(verdict)
    reopened = helpers.reopen(live)
    receiver.conn = reopened
    receiver.lose_reply = False
    receiver.evidence['row'].update(body='kept local body', version=21)
    try:
        client.push(reopened, receiver, 'default', 'macbook', primary=True, max_batches=1)
        assert len(receiver.sent) == sent_before + 1 and receiver.slot['a'] == sequence
        receipt = reopened.execute("SELECT * FROM kanban_sync_omissions WHERE field='body'").fetchone()
        if transient:
            assert receipt['restore_op_id'] != restore['op_id'] and receipt['state'] == 'restoring'
            retry = reopened.execute('SELECT * FROM kanban_sync_outbox WHERE op_id=?', (receipt['restore_op_id'],)).fetchone()
            assert retry['op'] == 'restore' and retry['wire_json'] is None
            with kbc.write_txn(reopened):
                reopened.execute('UPDATE kanban_sync_outbox SET retry_after=0 WHERE op_id=?', (retry['op_id'],))
            client.push(reopened, receiver, 'default', 'macbook', primary=True, max_batches=1)
            assert receiver.sent[-1]['replay']['sequence'] == str(int(sequence) + 1)
        assert reopened.execute("SELECT state FROM kanban_sync_omissions WHERE field='body'").fetchone()[0] == 'delivered'
        assert journal.pending_wire(reopened) is None
    finally:
        reopened.close()
