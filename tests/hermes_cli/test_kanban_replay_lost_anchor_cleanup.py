"""Lost anchors are not permission to destroy surviving replay custody."""
import pytest

from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_replay_journal as journal
from hermes_cli import kanban_sync_client as client, kanban_sync_envelope as envelopes
from hermes_cli.kanban_containment_outbox import strip_lifecycle
from hermes_cli.kanban_replay_admission import mark_rebuilt
from tests.hermes_cli.test_kanban_replay_parent_custody import issued_parent
from tests.hermes_cli.test_kanban_replay_feed_custody import echo
from tests.hermes_cli import test_kanban_replay_sender as sender

live = sender.live


def custody(conn):
    tables = ('kanban_sync_outbox', 'kanban_sync_parent_envelopes', 'kanban_replay_sender1',
              'kanban_replay_recovery_loss1', 'kanban_sync_state', 'tasks')
    present = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return {table: list(map(tuple, conn.execute(f'SELECT * FROM {table}'))) for table in tables if table in present}


def consume(conn, tid, op_id, consumer):
    if consumer == 'import':
        client.import_board(conn, tables=['tasks'])
    elif consumer == 'echo':
        sync.apply_remote_changes(conn, [echo(conn, tid, op_id)], new_cursor=8, observers=False)
    else:
        with kbc.write_txn(conn):
            actions = {'strip': lambda: strip_lifecycle(conn, tid),
                       'prune': lambda: envelopes.prune_parents(conn),
                       'retire': lambda: envelopes.retire_parent(conn, tid)}
            actions[consumer]()


@pytest.mark.parametrize('damage', ['null', 'missing'])
@pytest.mark.parametrize('evidence', ['journal', 'loss', 'both'])
@pytest.mark.parametrize('consumer', ['strip', 'prune', 'retire', 'echo', 'import'])
def test_lost_anchor_refuses_destructive_consumers(live, damage, evidence, consumer):
    tid, _, _ = issued_parent(live)
    op_id = live.execute('SELECT op_id FROM kanban_sync_outbox LIMIT 1').fetchone()[0]
    with kbc.write_txn(live):
        live.execute('UPDATE kanban_sync_state SET journal_id=NULL' if damage == 'null' else 'DELETE FROM kanban_sync_state')
        if evidence != 'journal':
            mark_rebuilt(live, {'kanban_sync_state': {'known_lost': 1}})
        if evidence == 'loss':
            live.execute('DROP TABLE kanban_replay_sender1')
        live.execute('UPDATE tasks SET sync_version=9 WHERE id=?', (tid,))
    before = custody(live)
    refusal = (ValueError, RuntimeError, client.SyncUnavailable)
    with pytest.raises(refusal, match='custody LOSS or invalid anchor|sync is not enabled|does not sync'):
        consume(live, tid, op_id, consumer)
    # Missing sync rows refuse before public feed/import dispatch. Also exercise
    # their custody helpers under the writer, rather than bypassing that preflight.
    if damage == 'missing' and consumer in ('echo', 'import'):
        with pytest.raises(ValueError, match='custody LOSS or invalid anchor'):
            with kbc.write_txn(live):
                if consumer == 'echo':
                    journal.issued_operation(live)
                else:
                    journal.guard_import(live, ('tasks',), None)
    assert custody(live) == before


@pytest.mark.parametrize('consumer', ['strip', 'prune', 'retire', 'echo', 'import'])
def test_never_enrolled_legacy_consumer_still_progresses(live, consumer):
    tid, _, _ = issued_parent(live)
    op_id = live.execute('SELECT op_id FROM kanban_sync_outbox LIMIT 1').fetchone()[0]
    with kbc.write_txn(live):
        live.execute('DROP TABLE kanban_replay_sender1')
        live.execute('UPDATE kanban_sync_state SET journal_id=NULL')
        live.execute('UPDATE tasks SET sync_version=9 WHERE id=?', (tid,))
    consume(live, tid, op_id, consumer)
    if consumer in ('prune', 'retire'):
        assert not live.execute('SELECT 1 FROM kanban_sync_parent_envelopes').fetchone()
    elif consumer == 'import':
        assert live.execute("SELECT 1 FROM kanban_sync_outbox WHERE table_name='tasks'").fetchone()
    elif consumer == 'echo':
        assert not live.execute('SELECT 1 FROM kanban_sync_outbox WHERE op_id=?', (op_id,)).fetchone()
