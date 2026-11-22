"""Retiring policy must preserve, not broaden, conservative author diagnostics."""
import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_review_model as review
from tests.hermes_cli import test_kanban_model_retirement as fixtures

board = fixtures.board


@pytest.mark.parametrize('boundary,models,expected', [
    (False, set(), {'grok-4.6'}),
    (False, {'actual-author'}, {'actual-author', 'grok-4.6'}),
    (True, {'new-author'}, {'new-author'}),
    (True, set(), {'grok-4.6'}),
])
def test_retirement_preserves_existing_epoch_rules(board, monkeypatch, boundary, models, expected):
    monkeypatch.setattr(review, '_current_authors', lambda *_: (set(models), boundary))
    with kbc.connect() as conn:
        tid = fixtures.seed(conn)
        before = review.implementer_models(conn, kb.get_task(conn, tid))
        assert before == expected
        fixtures.sweep(conn)
        assert kb.get_task(conn, tid).model_override is None
        assert review.implementer_models(conn, kb.get_task(conn, tid)) == before


@pytest.mark.parametrize('pin', ['class:deep', None])
def test_nonconcrete_retirement_adds_no_author(board, pin):
    with kbc.connect() as conn:
        tid = fixtures.seed(conn, model=pin)
        fixtures.sweep(conn)
        assert review.implementer_models(conn, kb.get_task(conn, tid)) == set()


def test_downstream_pin_never_becomes_implementer(board):
    with kbc.connect() as conn:
        tid = fixtures.seed(conn)
        conn.execute("UPDATE tasks SET title='Independent exact-head review fixture' WHERE id=?", (tid,))
        fixtures.sweep(conn)
        assert review.implementer_models(conn, kb.get_task(conn, tid)) == set()


@pytest.mark.parametrize('payload', [
    {}, {'reason': 'other', 'previous': {'model_override': 'grok-4.6'}},
    {'reason': 'model_policy_no_pins', 'previous': []},
    {'reason': 'model_policy_no_pins', 'previous': {'model_override': 12}},
    {'reason': 'model_policy_no_pins', 'previous': {'model_override': 'class:deep'}},
])
def test_unrelated_or_malformed_clear_is_not_retired_pin(board, payload):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='ordinary', assignee='default')
        kb._append_event(conn, tid, 'model_override_cleared', payload)
        assert review.implementer_models(conn, kb.get_task(conn, tid)) == set()


def test_new_class_policy_supersedes_conservative_pin(board):
    with kbc.connect() as conn:
        tid = fixtures.seed(conn)
        fixtures.sweep(conn)
        kb.set_model_override(conn, tid, 'class:deep', 'auto')
        assert review.implementer_models(conn, kb.get_task(conn, tid)) == set()


def test_latest_retirement_does_not_accumulate_old_pins(board):
    with kbc.connect() as conn:
        tid = fixtures.seed(conn)
        fixtures.sweep(conn)
        conn.execute("UPDATE tasks SET model_override='  newer-author  ' WHERE id=?", (tid,))
        fixtures.sweep(conn)
        assert review.implementer_models(conn, kb.get_task(conn, tid)) == {'newer-author'}
