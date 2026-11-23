"""Unknown/deleted current images preserve transport and independent custody. [daveey]"""
import json

import pytest

from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_replay_deferred as deferred
from hermes_cli import kanban_replay_journal as journal
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import test_kanban_replay_sender as helpers
from tests.hermes_cli import test_kanban_replay_synthetic_deferred as synthetic
from tests.hermes_cli import test_kanban_replay_current_row as current

live = helpers.live


def push(conn, receiver, primary=False):
    return client.push(conn, receiver, 'default', 'macbook', primary=primary, max_batches=1)


def hold(conn, receiver, primary=False):
    try:
        push(conn, receiver, primary)
    except (ValueError, OSError, client.SyncUnavailable):
        pass


@pytest.mark.parametrize('state', ['unknown', 'deleted', 'unavailable', 'local_delete', 'missing_local'])
@pytest.mark.parametrize('primary', [False, True])
def test_deferred_reference_survives_current_read_hold_and_restart(live, state, primary):
    tid, receiver = synthetic.issue(live)
    frozen = journal.pending_wire(live)
    wire = json.loads(frozen)
    if state == 'local_delete':
        with kbc.write_txn(live):
            live.execute("INSERT INTO kanban_sync_tombstones VALUES ('tasks',?,1)", (tid,))
    elif state == 'missing_local':
        with kbc.write_txn(live):
            live.execute('UPDATE kanban_sync_state SET applying=1')
            live.execute('DELETE FROM tasks WHERE id=?', (tid,))
            live.execute('UPDATE kanban_sync_state SET applying=0')
        receiver.evidence.update(state='unknown', row=None)
    elif state == 'unavailable':
        def unavailable(*args):
            raise OSError('current image offline')
        receiver.row_evidence = unavailable
    else:
        receiver.evidence.update(state=state, row=None)
    restarted = helpers.reopen(live)
    receiver.conn = restarted
    try:
        hold(restarted, receiver, primary)
        assert deferred.pending(restarted) == [dict(board_incarnation=receiver.slot['board_incarnation'],
            task=tid, origin=wire['status_origin'], status='done', hlc=wire['hlc'])], 'current read erased semantic custody'
        assert journal.pending_wire(restarted) == frozen
        assert receiver.slot['a'] == journal.snapshot(restarted)['settled'] == '0'
        assert restarted.execute('SELECT 1 FROM kanban_sync_parent_envelopes WHERE task_id=?', (tid,)).fetchone()
        assert len(receiver.sent) == 1
        calls = list(receiver.calls)
        hold(restarted, receiver, primary)
        assert receiver.calls == calls, 'persistent hold retried network automatically'
        assert len(receiver.sent) == 1
    finally:
        restarted.close()


@pytest.mark.parametrize('state', ['unknown', 'deleted'])
def test_versionless_parent_hold_is_visible_bounded_and_requires_exact_owner_resume(live, state, caplog):
    tid, receiver = current.issued_parent(live)
    receiver.evidence.update(state=state, row=None)
    frozen = journal.pending_wire(live)
    hold(live, receiver)
    report = client.status(live)
    fault = report.get('replay_fault')
    assert fault is not None, 'stuck transport has no visible actionable fault'
    assert fault['op_id'] == json.loads(frozen)['client_op_id']
    assert fault['sequence'] == '1' and fault['journal_id'] == receiver.slot['journal_id']
    assert fault['task'] == tid and fault['reason']
    assert 'replay-resume' in fault['recovery']
    assert sum('Replay current-row hold' in r.message for r in caplog.records) == 1
    calls = list(receiver.calls)
    reopened = helpers.reopen(live)
    receiver.conn = reopened
    try:
        for _ in range(9):
            hold(reopened, receiver)
        assert receiver.calls == calls and journal.pending_wire(reopened) == frozen
        assert client.status(reopened)['replay_fault'] == fault
        from hermes_cli import kanban_replay_existence as existence
        with pytest.raises(ValueError):
            existence.resume(reopened, 'wrong-operation', 'rechecked current state')
        with pytest.raises(ValueError):
            existence.resume(reopened, fault['op_id'], '')
        assert client.status(reopened)['replay_fault'] == fault
        existence.resume(reopened, fault['op_id'], 'owner checked serving Board; retry exact current image')
        receiver.evidence.update(state='live', row=dict(uid=tid, id=tid, title='restored current',
                                                       status='todo', version=23))
        assert push(reopened, receiver).applied == 1
        assert len(receiver.sent) == 1 and receiver.slot['a'] == '1'
        assert journal.pending_wire(reopened) is None
        assert client.status(reopened)['replay_fault'] is None
        receiver._call = helpers.Receiver._call.__get__(receiver)
        assert push(reopened, receiver).applied == 1
        assert receiver.sent[-1]['table'] == 'task_comments'
    finally:
        reopened.close()


@pytest.mark.parametrize('primary', [False, True])
@pytest.mark.parametrize('state', ['unknown', 'deleted'])
def test_plausible_row_bytes_do_not_override_unknown_or_deleted_evidence(live, primary, state):
    from tests.hermes_cli.test_kanban_replay_primary import lost_primary
    _tid, receiver = lost_primary(live) if primary else current.issued_parent(live)
    receiver.evidence['state'] = state
    frozen = journal.pending_wire(live)
    hold(live, receiver, primary)
    assert journal.pending_wire(live) == frozen, 'untrusted current row released transport'
    assert receiver.slot['a'] == '0' and len(receiver.sent) == 1
    assert client.status(live)['replay_fault']['paused'] == 1


def test_fault_write_failure_does_not_release_transport_or_ack(live, monkeypatch):
    from hermes_cli import kanban_replay_existence as existence
    _tid, receiver = current.issued_parent(live)
    receiver.evidence.update(state='unknown', row=None)
    frozen = journal.pending_wire(live)
    def fail(*args):
        raise OSError('fault storage unavailable')
    monkeypatch.setattr(existence, 'pause', fail)
    with pytest.raises(OSError, match='fault storage unavailable'):
        push(live, receiver)
    assert journal.pending_wire(live) == frozen and receiver.slot['a'] == '0'
