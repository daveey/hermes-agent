"""Real inventory shapes: requirements versus evidence and exclusions."""
import pytest
from hermes_cli.kanban_model_neutral import validate_text


@pytest.mark.parametrize('text', [
    'Independent primary Opus review, exact-head CI, normal merge queue.',
    'Get semantic RED. Mandatory exact-head anthropic/claude-opus-5-5 independent review; never hand merge.',
    'Primary independentOpus exacthead review + BB mergequeue.',
    'Each slice needs a fresh independent review (claude-opus-5-5/anthropic; park until quota resets).',
    'Do not bypass CI. Use only grok-4.6 / xai-oauth.',
    'Exclude Opus from authors. Required grok-4.6 / xai-oauth, mandatory pin.',
    'require_model: true',
])
def test_active_named_obligations_are_rejected(text):
    with pytest.raises(ValueError, match='model-neutral'):
        validate_text(text)


@pytest.mark.parametrize('text', [
    'Prior reviewer ran on claude-opus-5-5 and returned request_changes.',
    'Implementer models/forbidden overlaps: claude-opus-5-5, gpt-6.1-sol.',
    '<!-- implementer-models: claude-opus-5-5,gpt-6.1-sol -->',
    'Never pin to Opus. Route dynamically among eligible independent models.',
    'Find the Desktop lease loss after successful Opus response.',
    'Preserve all reserve/pacing and model-independence boundaries.',
    'Independent primary class:deep review with actual route evidence.',
    'The reviewer must differ from gpt-6.1-sol.',
    'Must not be reviewed by claude-opus-5-5.',
])
def test_historical_evidence_and_author_exclusions_are_retained(text):
    validate_text(text)
