"""Regression coverage for the three independently reproduced home-review defects."""
from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as dbc
from hermes_cli import kanban_home_owner as owner, kanban_home_transfer as transfer
from hermes_cli import kanban_wake_check as wake
from hermes_cli.kanban_sync_client import Hub, Identity
from tests.hermes_cli import test_home_transfer_capture as support

board = support.board


@pytest.mark.parametrize('purpose,status', [('wake', 'scheduled'), ('ask_resolved', 'blocked'), ('wake', 'todo')])
def test_prepared_probe_does_not_starve_due_neighbour(board, monkeypatch, purpose, status):
    task = kb.create_task(board, title='frozen probe')
    other = kb.create_task(board, title='independent probe')
    with dbc.write_txn(board):
        board.execute('UPDATE tasks SET status=?,created_on=\'worker-a3\',sync_version=7 WHERE id=?', (status, task))
        board.execute("UPDATE tasks SET status='scheduled' WHERE id=?", (other,))
        wake.set_check(board, task, 'exit 1', 120, now=1)
        board.execute('UPDATE task_wake_checks SET purpose=? WHERE task_id=?', (purpose, task))
        wake.set_check(board, other, 'exit 1', 120, now=2)
        board.execute("UPDATE kanban_sync_state SET gateway_id='worker-a3'")
        board.execute('DELETE FROM kanban_sync_outbox')
        transfer.prepare(board, task, source='worker-a3', destination='hub', nonce='review-probe-00001', expected_version=7)
    before = tuple(board.execute('SELECT * FROM task_wake_checks WHERE task_id=?', (task,)).fetchone())
    ran = []
    def probe(*args):
        ran.append(args[0][1])
        return 1, 'not yet'
    monkeypatch.setattr(wake, '_run_probe', probe)
    try:
        result = wake.run_due_wake_checks(board, now=100, wait=True)
        assert result['submitted'] == [other]
        assert ran == [other]
        assert tuple(board.execute('SELECT * FROM task_wake_checks WHERE task_id=?', (task,)).fetchone()) == before
    finally:
        wake._shutdown_for_tests()


@pytest.mark.parametrize('source,destination', [('macbook', 'hub'), ('worker-a3', 'hermes-hub'),
                                               ('worker-', 'hub'), ('worker-' + 'a' * 25, 'hub')])
def test_owner_rejects_unsupported_identity_before_fencing_or_network(board, monkeypatch, source, destination):
    task = kb.create_task(board, title='invalid protocol identity')
    with dbc.write_txn(board):
        board.execute('UPDATE tasks SET created_on=?,sync_version=7 WHERE id=?', (source, task))
        board.execute("UPDATE kanban_sync_state SET gateway_id=?,board_id='default'", (source,))
        board.execute('DELETE FROM kanban_sync_outbox')
    hub = Hub(Identity('http://127.0.0.1:1', 'fixture-key', source))
    monkeypatch.setattr(hub, 'home_transfer', lambda *a: pytest.fail('invalid identity reached network'))
    before = list(board.iterdump())
    with pytest.raises(ValueError, match='invalid home preparation identity'):
        owner.source_step(board, hub, 'default', dict(task=task, source=source, destination=destination,
                          nonce='review-invalid-00001', expected_version=7))
    assert list(board.iterdump()) == before
    assert not transfer.frozen(board, task)
