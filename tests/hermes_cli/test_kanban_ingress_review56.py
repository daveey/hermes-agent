"""Structured review refusals and phrase-scoped text policy."""
import pytest

from hermes_cli import kanban_db as kb
from tests.hermes_cli import test_kanban_managed_pin_policy as support

board = support.board


@pytest.mark.parametrize('metadata', [
    {'review_model': 'class:deep', 'review_provider': 'anthropic'},
    {'review_model': ['class:deep']},
    {'review_model': {}},
    {'review_model': False},
    {'review_model': 'class:deep', 'review_provider': []},
])
def test_review_refusal_is_structured(board, metadata):
    conn, _ = board
    tid = kb.create_task(conn, title='Review contract', assignee='worker')
    kb.claim_task(conn, tid)
    before = kb.get_task(conn, tid)
    events = kb.list_events(conn, tid)
    ok, reason = kb.request_review(conn, tid, metadata=metadata, with_reason=True)
    assert not ok and 'model-neutral' in reason
    after = kb.get_task(conn, tid)
    assert (before.status, before.current_run_id, before.claim_lock) == (
        after.status, after.current_run_id, after.claim_lock)
    assert kb.list_events(conn, tid) == events
    assert conn.execute('SELECT outcome FROM task_runs WHERE id=?',
                        (before.current_run_id,)).fetchone()[0] is None


@pytest.mark.parametrize('body', [
    'Must use opus',
    'Do not pin providers, but must use opus',
    'Exclude: sonnet, but must use opus',
    'Never require sonnet but must use opus',
    'Reviewer must differ from sonnet but must use opus',
    'Excluded models: sonnet; must use opus',
    'Must not pin sonnet and mandatory opus',
])
def test_active_obligation_refused(board, body):
    conn, _ = board
    before = conn.execute('SELECT count(*) FROM tasks').fetchone()[0]
    with pytest.raises(ValueError, match='model-neutral'):
        kb.create_task(conn, title='Review implementation', body=body, assignee='worker')
    assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == before


@pytest.mark.parametrize('body', [
    'Do not pin providers', 'Exclude: sonnet', 'Never require opus',
    'Reviewer must differ from sonnet', 'Must not pin opus only',
    'Historical run instruction (superseded and not active): "Must use opus"',
])
def test_negative_and_historical_controls_create_unchanged(board, body):
    conn, _ = board
    tid = kb.create_task(conn, title='Review implementation', body=body, assignee='worker')
    assert kb.get_task(conn, tid).body == body
