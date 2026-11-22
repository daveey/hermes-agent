"""Import is not permission to replace an issued or rejected replay identity."""
import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_replay_journal as journal
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import test_kanban_replay_sender as helpers

live = helpers.live


def frozen_import_state(conn):
    return (list(map(tuple, conn.execute('SELECT * FROM kanban_sync_outbox ORDER BY op_id'))),
            list(map(tuple, conn.execute('SELECT * FROM kanban_replay_sender1'))))


@pytest.mark.parametrize('scoped', [False, True])
@pytest.mark.parametrize('status', ['pending', 'rejected'])
def test_import_cannot_replace_replay_custody(live, scoped, status):
    tid = kb.create_task(live, title='custody', assignee='worker')
    receiver = helpers.Receiver()
    if status == 'pending':
        receiver.lose_reply = True
        with pytest.raises(OSError, match='reply lost'):
            client.push(live, receiver, 'default', 'macbook', primary=False, max_batches=1)
    else:
        receiver.status = status
        client.push(live, receiver, 'default', 'macbook', primary=False, max_batches=1)
    before = frozen_import_state(live)
    with pytest.raises(ValueError, match='import overlaps replay custody'):
        client.import_board(live, tables=['tasks'], only={'tasks': {tid}} if scoped else None)
    assert frozen_import_state(live) == before


@pytest.mark.parametrize('status', ['pending', 'rejected'])
def test_unrelated_scoped_import_preserves_custody(live, status):
    tid = kb.create_task(live, title='custody', assignee='worker')
    receiver = helpers.Receiver()
    receiver.status = status if status == 'rejected' else 'applied'
    receiver.lose_reply = status == 'pending'
    if receiver.lose_reply:
        with pytest.raises(OSError):
            client.push(live, receiver, 'default', 'macbook', primary=False, max_batches=1)
    else:
        client.push(live, receiver, 'default', 'macbook', primary=False, max_batches=1)
    other = kb.create_task(live, title='unrelated', assignee='worker')
    before = frozen_import_state(live)
    counts = client.import_board(live, tables=iter(['tasks']), only={'tasks': {other}})
    after = frozen_import_state(live)
    assert counts == {'cleared_rejected': 0, 'tasks': 1}
    assert after[1] == before[1]
    assert [r for r in after[0] if r[2] == tid] == [r for r in before[0] if r[2] == tid]
    assert len(after[0]) == len(before[0]) + 1


def test_bound_import_does_not_clear_unrelated_rejections(live):
    kb.create_task(live, title='rejected', assignee='worker')
    receiver = helpers.Receiver()
    receiver.status = 'rejected'
    client.push(live, receiver, 'default', 'macbook', primary=False, max_batches=1)
    before = frozen_import_state(live)
    assert client.import_board(live, tables=['task_links']) == {'cleared_rejected': 0, 'task_links': 0}
    assert frozen_import_state(live) == before


def test_bound_import_validates_journal_anchor(live):
    with kbc.write_txn(live):
        live.execute('UPDATE kanban_sync_state SET journal_id=?', ('different',))
    before = frozen_import_state(live)
    with pytest.raises(ValueError, match='binding mismatch'):
        client.import_board(live, tables=['tasks'])
    assert frozen_import_state(live) == before


def test_empty_import_scope_keeps_pending_custody(live):
    kb.create_task(live, title='issued', assignee='worker')
    receiver = helpers.Receiver()
    receiver.lose_reply = True
    with pytest.raises(OSError):
        client.push(live, receiver, 'default', 'macbook', primary=False, max_batches=1)
    before = frozen_import_state(live)
    assert client.import_board(live, tables=['tasks'], only={'tasks': set()}) == {
        'cleared_rejected': 0, 'tasks': 0}
    assert frozen_import_state(live) == before


@pytest.mark.parametrize('scoped', [False, True])
def test_import_guard_uses_frozen_wire_even_without_materialized_source(live, scoped):
    # Synthetic or historically missing sources must still fence their logical import scope.
    tid = kb.create_task(live, title='would import first', assignee='worker')
    with kbc.write_txn(live):
        journal.issue(live, dict(client_op_id=client.sync.uuid7(), table='task_links',
                                uid=f'{tid}>missing', op='upsert', fields={}))
    before = frozen_import_state(live)
    with pytest.raises(ValueError, match='import overlaps replay custody'):
        client.import_board(live, tables=['tasks', 'task_links'],
                            only={'tasks': {tid}, 'task_links': {f'{tid}>missing'}} if scoped else None)
    assert frozen_import_state(live) == before


def test_corrupt_pending_wire_refuses_unrelated_import(live):
    with kbc.write_txn(live):
        journal.issue(live, dict(client_op_id=client.sync.uuid7(), table='tasks',
                                uid='missing', op='upsert', fields={}))
        live.execute('UPDATE kanban_replay_sender1 SET wire=?', (b'{}',))
    before = frozen_import_state(live)
    with pytest.raises(ValueError, match='invalid pending wire'):
        client.import_board(live, tables=['task_links'])
    assert frozen_import_state(live) == before


def test_unbound_import_keeps_legacy_clear_behavior(live):
    # Remove only the dormant test enrollment to exercise the unchanged legacy branch.
    with kbc.write_txn(live):
        live.execute('UPDATE kanban_sync_state SET journal_id=NULL')
        live.execute('DROP TABLE kanban_replay_sender1')
    kb.create_task(live, title='legacy', assignee='worker')
    with kbc.write_txn(live):
        live.execute("UPDATE kanban_sync_outbox SET state='rejected'")
    counts = client.import_board(live, tables=['tasks'])
    assert counts['cleared_rejected'] > 0 and counts['tasks'] == 1
    assert not live.execute("SELECT 1 FROM kanban_sync_outbox WHERE state='rejected'").fetchone()
