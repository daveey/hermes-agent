"""Bounded review-role grammar, not a complete natural-language classifier."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_model_neutral as policy
from tests.hermes_cli import test_kanban_kernel_history as fixtures

conn = fixtures.conn
clock = fixtures.clock

ACTIVE = [
    'Now: run an independent exact-head review of abc123 with a pinned claude-opus-5-5 subagent, prompted neutrally.',
    'Arrange independent pinned Opus review through review card/request_review and exact-head CI before landing; do not self-review.',
    'Post plan first; commit/push DRAFT prefixes, exact-head independent Opus5.5 review and CI mandatory.',
    'Exclude: Opus, run an independent review using claude-opus-5-5.',
    'Do not pin Opus, arrange independent pinned claude-opus-5-5 review.',
    'Run an independent review using claude-opus-5-5.',
    'Independent pinned Opus review.',
    'Independent Opus review through review card.',
    'Opus 5.5 review required.',
    'Opus-5-5 review required.',
    'Exact-head independent claude-opus-5-5 review and CI mandatory.',
]
INERT = [
    'The old run used a pinned claude-opus-5-5 subagent.',
    'Prior reviewer ran on claude-opus-5-5 and returned request_changes.',
    'Run an independent review of the Opus pricing change.',
    'Run the Opus tokenizer tests.',
    'Independent Opus review through request_review returned approve.',
    'Completed: independent pinned Opus review.',
    'Independent pinned Opus review: completed.',
    'Independent pinned Opus review, completed successfully.',
    'Run an independent review without using Opus.',
    'Run an independent review of code written by Opus.',
    'Run review of the chat model selector using Opus as a test fixture.',
    'CI and independent Opus5.5 review (APPROVE) done.',
    'CI and independent Opus5.5 review (REQUEST_CHANGES) completed.',
    'Exact-head review and CI mandatory; prior review ran on Opus.',
    'Opus is not pinned; route dynamically.',
    'Must not be reviewed by Opus5.5.',
    'Exclude: Opus5.5.',
    '<!-- implementer-models: Opus5.5 -->',
    'Opus5.5 is mentioned in the changelog.',
    'Implement an application that uses Opus5.5 for summarization.',
]


@pytest.mark.parametrize('text', ACTIVE)
def test_create_edit_refuse_without_mutation(conn, text):
    task = kb.create_task(conn, title='Grammar fixture', body='Independent review.')
    before = conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]
    with pytest.raises(ValueError, match='model-neutral'):
        kb.create_task(conn, title='New grammar fixture', body=text)
    assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == before
    with pytest.raises(ValueError, match='model-neutral'):
        kb.edit_task(conn, task, body=text)
    assert kb.get_task(conn, task).body == 'Independent review.'


@pytest.mark.parametrize('text', INERT)
def test_inert_text_unchanged(text):
    policy.validate_text(text)
    assert policy.forwarded_text(text) == text
    assert policy.standing_allowed(text)


@pytest.mark.parametrize('text', ACTIVE)
def test_history_and_standing_consumers(text):
    forwarded = policy.forwarded_text(text)
    prefix = 'Historical run instruction (superseded and not active): '
    lines = forwarded.splitlines()
    assert json.loads(lines[0][len(prefix):]) == text
    neutral = policy.neutral_line(text)
    assert lines[1] == neutral
    policy.validate_text(neutral)
    assert policy.forwarded_text(forwarded) == forwarded
    assert not policy.standing_allowed(text)
    with pytest.raises(ValueError, match='model-neutral'):
        policy.validate_text(lines[0] + ' ' + text)


def test_neutralization_preserves_compound_gates_and_exclusions():
    text = 'Exclude: Opus5.5; exact-head independent Opus5.5 review and CI mandatory.'
    assert policy.neutral_line(text) == (
        'Exclude: Opus5.5; exact-head independent any qualified route review and CI mandatory.'
    )
