"""Reviewer probe 3 (not part of the PR): create_task with text that names a route/plugin but carries no card-route mandate.

Strings are copied from live board rows or origin/main commit subjects. Controls are real mandates.
"""
from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb
from tests.hermes_cli import test_kanban_containment_lifecycle as chelpers

board = chelpers.board

NOT_MANDATES = [
    # origin/main commit subjects (ship copies subjects into review-card bodies via range-diff)
    ("subject_gemini_live", "feat(plugin-catalog): pin gemini-live to the latest commit"),
    ("subject_directsdk", "chore(catalog): pin claude-subscription-directsdk to 0.3.4"),
    # tool / plugin names, not models
    ("claude_code_skill", "Use the claude-code skill to delegate the refactor."),
    # provenance copied from live cards
    ("followup_from_review", "Non-blocking follow-up from independent Opus review of state PR3204 exact 89c9a9fa8c."),
    ("previous_review", "Previous exact-head Opus review t_01a1f850b1834855 approved, ran 39 tests, but ship refused it."),
    ("review_done", "exact-head validate and independent Opus review (APPROVE) done."),
    ("canary_evidence", "The canary card t_01a1e31e8805e893 (pinned gpt-6-astra/openai-codex by `canary_pin`) was never claimed."),
    ("run_receipt", "Actual session local-962f pinned anthropic/claude-opus-5-5 exact_pin=true fallback_active=false."),
    # implementation spec for code under change, not for the card's route
    ("code_spec", "If the config names no fast model, use claude-haiku-5-5 and record it."),
]
MANDATES = [
    ("mandate_must", "This review MUST run claude-opus-5-5/anthropic, mandatory pin."),
    ("mandate_required", "Independent primary Opus review required"),
    ("mandate_directive", "require_model: true"),
]


@pytest.mark.parametrize("text", [t for _n, t in NOT_MANDATES], ids=[n for n, _t in NOT_MANDATES])
def test_non_mandate_body_creates(board, text):
    tid = kb.create_task(board, title="Follow-up card", body=f"Context for the worker.\n{text}\nMore context.", assignee="default")
    assert board.execute("SELECT body FROM tasks WHERE id=?", (tid,)).fetchone()[0].count(text) == 1


@pytest.mark.parametrize("text", [t for _n, t in MANDATES], ids=[n for n, _t in MANDATES])
def test_mandate_refused_no_write(board, text):
    before = board.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    with pytest.raises(ValueError):
        kb.create_task(board, title="Review card", body=text, assignee="default")
    assert board.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == before


@pytest.mark.parametrize('subject', [text for _name, text in NOT_MANDATES[:2]])
def test_ship_range_diff_creates_review_with_literal_subject(board, subject):
    body = (
        'Independent exact-head review. Keep required CI and normal landing gates.\n'
        'Canonical clean rebase range-diff:\n```\n'
        f'1: abcdef12 = 1: 123456ab {subject}\n```\n'
        'Read the complete diff and return an evidence-backed verdict.'
    )
    tid = kb.create_task(board, title='Independent exact-head review', body=body)
    stored = kb.get_task(board, tid)
    assert stored.body == body
    assert stored.model_override is None and stored.provider_override is None


@pytest.mark.parametrize('text', [text for _name, text in MANDATES])
def test_refusal_identifies_exact_line(board, text):
    with pytest.raises(ValueError) as error:
        kb.create_task(board, title='Review card', body='Context\n' + text)
    assert 'refused line: ' + text in str(error.value)


@pytest.mark.parametrize('text', [
    'Use claude-code to inspect the implementation.',
    'Use gemini-live for the speech integration.',
    'Pin claude-opus-5-5-plugin to a commit.',
    'Use gpt-6-luna-helper to format the report.',
    'Use claude-opus-5-5.extra as the fixture string.',
    'Independent Opus review (APPROVE) done.',
    'Independent Opus review t_fixture approved; retain that audit receipt.',
    'If the config has no fast model, use claude-haiku-5-5 and record it; use the class registry for this card.',
])
def test_non_route_context_not_mandatory(board, text):
    tid = kb.create_task(board, title='Implementation context', body=text)
    assert kb.get_task(board, tid).body == text


@pytest.mark.parametrize('text', [
    'Use claude-opus-5-5.',
    'Use claude-opus-5-5/anthropic.',
    'Use anthropic/claude-opus-5-5.',
    'Use grok-4.6.',
    'Use gpt-6-luna.',
    'Use only claude-opus-5-5; keep required CI.',
])
def test_catalog_model_obligation_is_not_tool_name(board, text):
    before = board.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]
    with pytest.raises(ValueError, match='refused line:'):
        kb.create_task(board, title='Review instruction', body=text)
    assert board.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == before
