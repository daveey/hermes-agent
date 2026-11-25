"""Actionability must consume the same verified renewal as execution readers."""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_actionability_sources import Reader, _private_reason, describe
from tests.hermes_cli import test_kanban_scope_renewal as support

board = support.board


@pytest.mark.parametrize('canonical_uid', [False, True])
def test_renewed_scope_is_not_a_private_hold(board, monkeypatch, canonical_uid):
    if canonical_uid:
        # Give new events transport identities distinct from local integer IDs.
        board.execute('ALTER TABLE task_events ADD COLUMN uid TEXT')
        board.execute("CREATE TRIGGER fixture_event_uid AFTER INSERT ON task_events "
                      "BEGIN UPDATE task_events SET uid='canonical-' || NEW.id WHERE id=NEW.id; END")
    tid, _, episode = support.stopped(board, monkeypatch)
    assert episode.startswith('canonical-') == canonical_uid
    support.link(board, tid)
    comment = support.mandate(board, tid)
    support.renew(board, tid, episode, comment)
    assert kb.promote_task(board, tid, actor='fixture', reason='renewed scope')[0]
    row = describe(board, tid)
    assert 'private_hold' not in row['violations']
    assert 'Private hold:' not in row['instruction']
    assert _private_reason(board, {'id': tid, 'status': 'running'}, Reader(board)) is None

    from hermes_cli import kanban_scope_renewal
    with monkeypatch.context() as patcher:
        def unavailable(*args):
            raise ValueError('mandate evidence unavailable')
        patcher.setattr(kanban_scope_renewal, 'covered', unavailable)
        assert _private_reason(board, {'id': tid, 'status': 'ready'}, Reader(board)) == 'mandate_unverifiable'

    # Coverage is tied to the frozen mandate bytes, not just a renewal's presence.
    board.execute('UPDATE task_comments SET body=? WHERE id=?', ('edited mandate', comment))
    assert _private_reason(board, {'id': tid, 'status': 'ready'}, Reader(board)) == 'mandate_unverifiable'


def test_unrenewed_scope_remains_a_private_hold(board):
    tid = kb.create_task(board, title='unrenewed scope', assignee='default')
    kb._append_event(board, tid, 'spend_budget_triaged', {'cause': 'scope_stopped'})
    # Even a stale runnable row cannot turn an unrenewed stop into permission.
    board.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    row = describe(board, tid)
    assert 'private_hold' in row['violations']
    assert 'Private hold: scope_stop_not_renewed' in row['instruction']
