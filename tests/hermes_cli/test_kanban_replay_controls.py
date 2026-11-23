"""Production replay of existing /ops controls, not canonical command authority."""
import json

import pytest

from hermes_cli import kanban_db_connect as kbc, kanban_db_sync as sync
from hermes_cli import kanban_replay_journal as journal, kanban_sync_client as client
from hermes_cli import kanban_actionability_transport as act, kanban_retain_transport as retain
from hermes_cli import kanban_failure_release as release
from tests.hermes_cli import test_kanban_replay_sender as sender
from tests.hermes_cli import test_kanban_failure_transport as failure
from tests.hermes_cli import test_kanban_retain_transport as retained
from tests.hermes_cli import test_kanban_actionability_transport as diagnostic

live = sender.live
KINDS = ['retain', 'actionability_record', 'release']


class Controls(sender.Receiver):
    def decision_retain_capability(self, board):
        return dict(decision_retain=1, frozen_replay=1, hlc='0000000000001-0000-server')

    def actionability_capability(self, board):
        return dict(actionability_record=1, frozen_replay=1, diagnostic_only=True, hlc='0000000000001-0000-server')

    def row_evidence(self, board, table, uid):
        assert self.sent[-1]['op'] == 'release', 'frozen semantic command must bypass row projection'
        return dict(board=board, table=table, uid=uid, state='live', head=10,
                    row=dict(uid=uid, status='running', version=5))


def reserve(conn, kind):
    sync.set_mode(conn, 'primary')
    if kind == act.KIND:
        tid, payload = diagnostic.contract(conn)
        with kbc.write_txn(conn):
            conn.execute("UPDATE tasks SET created_on='macbook' WHERE id=?", (tid,))
        owned = diagnostic.reserve(conn, tid, payload)
    else:
        tid, rid = failure._running(conn)
        if kind == 'retain':
            owned = retained.reserve(conn, tid, rid)
        else:
            with kbc.write_txn(conn):
                release.queue(conn, tid, rid, outcome='crashed', error='controlled exit')
            owned = dict(conn.execute('SELECT * FROM kanban_failure_commands').fetchone())
    row = dict(conn.execute('SELECT * FROM kanban_sync_outbox WHERE op_id=?', (owned['op_id'],)).fetchone())
    return tid, owned, row


def command(conn, kind, owned):
    return act.command(conn, owned['op_id']) if kind == act.KIND else retain.command(conn, owned['run_uid'])


@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('primary', [False, True])
def test_control_lost_committed_reply_reopen_no_resend(live, kind, primary):
    tid, owned, _row = reserve(live, kind)
    receiver = Controls()
    receiver.lose_reply = True
    try:
        with pytest.raises(OSError, match='reply lost'):
            client.push(live, receiver, 'default', 'macbook', primary=primary, max_batches=1)
    except (ValueError, KeyError) as exc:
        pytest.fail(f'commissioned control cannot enter replay: {kind}: {exc}')
    frozen = journal.pending_wire(live)
    wire = json.loads(frozen)
    assert wire['op'] == kind and wire['task'] == tid
    if kind != 'release':
        assert {k: v for k, v in wire.items() if k != 'replay'} == json.loads(owned['request_json'])
    reopened = sender.reopen(live)
    receiver.lose_reply = False
    try:
        result = client.push(reopened, receiver, 'default', 'macbook', primary=primary, max_batches=1)
        assert result.applied == 1 and len(receiver.sent) == 1 and receiver.slot['a'] == '1'
        assert journal.pending_wire(reopened) is None
        assert not reopened.execute('SELECT 1 FROM kanban_sync_outbox WHERE op_id=?', (owned['op_id'],)).fetchone()
        current = command(reopened, kind, owned)
        assert current['op_id'] == owned['op_id']
        if kind != 'release':
            assert current['request_json'] == owned['request_json']
            assert current['delivery_state'] == 'acknowledged' and current['canonical_receipt_json'] is None
        else:
            assert reopened.execute('SELECT sync_version FROM tasks WHERE id=?', (tid,)).fetchone()[0] == 5
        client.push(reopened, receiver, 'default', 'macbook', primary=primary, max_batches=0)
        assert len(receiver.sent) == 1
    finally:
        reopened.close()


@pytest.mark.parametrize('kind', ['retain', act.KIND])
@pytest.mark.parametrize('reason', ['storage_error', 'depends_on_rejected', 'forbidden'])
def test_retained_final_rejection_is_not_unknown_retry(live, kind, reason):
    _, owned, _ = reserve(live, kind)
    receiver = Controls()
    receiver.lose_reply = True
    with pytest.raises(OSError):
        client.push(live, receiver, 'default', 'macbook', primary=True, max_batches=1)
    verdict = json.loads(receiver.slot['last_verdict'])
    verdict.update(status='rejected', reason=reason)
    receiver.slot['last_verdict'] = json.dumps(verdict)
    receiver.lose_reply = False
    reopened = sender.reopen(live)
    try:
        result = client.push(reopened, receiver, 'default', 'macbook', primary=True, max_batches=1)
        current = command(reopened, kind, owned)
        assert result.rejected == 1 and current['delivery_state'] == 'rejected'
        assert current['request_json'] == owned['request_json'] and current['canonical_receipt_json'] is None
        row = reopened.execute('SELECT * FROM kanban_sync_outbox WHERE op_id=?', (owned['op_id'],)).fetchone()
        assert row['state'] == 'rejected' and reason in row['last_error']
        assert journal.pending_wire(reopened) is None and receiver.slot['a'] == '1'
        client.push(reopened, receiver, 'default', 'macbook', primary=True, max_batches=1)
        assert len(receiver.sent) == 1
    finally:
        reopened.close()


@pytest.mark.parametrize('kind', ['retain', act.KIND])
def test_command_custody_change_blocks_recovery(live, kind):
    _, owned, _ = reserve(live, kind)
    receiver = Controls()
    receiver.lose_reply = True
    with pytest.raises(OSError):
        client.push(live, receiver, 'default', 'macbook', primary=True, max_batches=1)
    frozen = journal.pending_wire(live)
    table = 'kanban_actionability_commands' if kind == act.KIND else 'kanban_failure_commands'
    with kbc.write_txn(live):
        live.execute(f"UPDATE {table} SET request_sha256='changed' WHERE op_id=?", (owned['op_id'],))
    receiver.lose_reply = False
    with pytest.raises(ValueError, match='custody'):
        client.push(live, receiver, 'default', 'macbook', primary=True, max_batches=1)
    assert journal.pending_wire(live) == frozen and receiver.slot['a'] == '0' and len(receiver.sent) == 1
