"""Native pull owns bounded replay; admin/repair calls never consume it."""
from dataclasses import asdict

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as dbc, kanban_db_sync as sync
from hermes_cli import kanban_home_feed as feed, kanban_home_transfer as transfer
from hermes_cli import kanban_sync_client as client, kanban_sync_apply_budget as budget
from tests.hermes_cli import test_kanban_home_source_stop as support
from tests.hermes_cli.test_home_feed_custody import handoff
from tests.hermes_cli.test_review_home_feed_isolation import change

board = support.board


def retained(conn):
    tid, record = support.prepared(conn)
    cursor = sync.sync_state(conn)['cursor']
    sync.apply_remote_changes(conn, [handoff(conn, tid, record, cursor+1)], new_cursor=cursor+1, observers=False)
    with dbc.write_txn(conn):
        transfer.finalize_source(conn, record)
    wire = conn.execute('SELECT wire FROM kanban_home_feed1').fetchone()[0]
    return tid, record, cursor+1, wire


def pull(conn, monkeypatch, rows, cursor):
    hub = client.Hub(client.Identity('http://127.0.0.1:1', 'fixture-key', 'worker-a3'))
    page = dict(changes=[asdict(ch) for ch in rows], next=cursor, more=False)
    monkeypatch.setattr(client.acw, 'changes_page', lambda *a: page)
    monkeypatch.setattr(client, 'discard_replica_writes', lambda *a: None)
    return client.pull(conn, hub, 'default', observers=False)


@pytest.mark.parametrize('fault', ['sql', 'budget'])
def test_poison_or_slow_replay_cannot_abort_independent_incoming_page(board, monkeypatch, fault):
    tid, _, cursor, wire = retained(board)
    peer = kb.create_task(board, title='healthy')
    with dbc.write_txn(board):
        board.execute('DELETE FROM kanban_sync_outbox')
        if fault == 'sql':
            board.execute("CREATE TRIGGER poisoned_replay BEFORE UPDATE ON tasks WHEN NEW.id='" + tid +
                          "' BEGIN SELECT RAISE(ABORT,'poisoned replay'); END")
    if fault == 'budget':
        apply = sync.apply_remote_changes
        def slow(conn, changes, **kwargs):
            if kwargs.get('home_replay_id') is not None:
                raise budget.ApplyBudgetExceeded('retained one-row reservation exceeded')
            return apply(conn, changes, **kwargs)
        monkeypatch.setattr(sync, 'apply_remote_changes', slow)
    assert pull(board, monkeypatch, [change(board, peer, 4, cursor+1, title='progress')], cursor+1) == 1
    assert kb.get_task(board, peer).title == 'progress'
    assert sync.sync_state(board)['cursor'] == cursor+1
    row = board.execute('SELECT wire,attempts,last_error FROM kanban_home_feed1').fetchone()
    assert row[0] == wire and row[1] == 1
    assert ('IntegrityError' if fault == 'sql' else 'ApplyBudgetExceeded') in row[2]
    assert board.execute('SELECT sync_version FROM tasks WHERE id=?', (tid,)).fetchone()[0] == 8


def test_admin_repair_and_keep_filtered_apply_never_replay_unrelated_custody(board, monkeypatch):
    tid, _, cursor, wire = retained(board)
    row = board.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone()
    sync.apply_remote_changes(board, [], new_cursor=cursor, observers=False, local_pending_wins=True)
    peer = kb.create_task(board, title='admin-selected')
    sync.apply_remote_changes(board, [change(board, peer, 4, cursor+1)], new_cursor=cursor+1,
                              observers=False, keep=lambda conn, ch: False)
    assert tuple(board.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone()) == tuple(row)
    assert board.execute('SELECT wire,attempts FROM kanban_home_feed1').fetchone()[:] == (wire, 0)
    pull(board, monkeypatch, [], cursor+1)
    assert not feed.pending(board)


def test_real_budget_wrapper_defers_certified_prepared_containment_before_any_marker_write(board):
    from hermes_cli import kanban_containment_replica as replica
    from tests.hermes_cli import test_kanban_containment_replica as custody
    target, _, stop, _, hub = custody.scenario(board)
    sync.set_identity(board, 'fixture', 'worker-a3')
    with dbc.write_txn(board):
        board.execute("UPDATE tasks SET created_on='worker-a3',sync_version=7 WHERE id=?", (target,))
        board.execute('DELETE FROM kanban_sync_outbox')
        record = support.record(board, target)
        transfer.prepare(board, target, source='worker-a3', destination='hub', nonce=record['nonce'], expected_version=7)
    before = tuple(board.execute('SELECT * FROM tasks WHERE id=?', (target,)).fetchone())
    peer = kb.create_task(board, title='healthy')
    rows = [custody.event(stop, hub, 1), change(board, peer, 4, 2, title='progress')]
    replica.certify_page(board, hub, 'fixture', rows)
    result = budget.apply_page(board, rows, new_cursor=2, board='fixture', observers=False)
    assert result.deferred == 1 and kb.get_task(board, peer).title == 'progress'
    assert tuple(board.execute('SELECT * FROM tasks WHERE id=?', (target,)).fetchone()) == before
    assert feed.pending(board)
