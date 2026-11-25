"""Retiring a textual pin must not inactivate other obligations on the same line."""
import json

import pytest

from hermes_cli.kanban_model_neutral import _active_text, forwarded_text, validate_text
from hermes_cli.kanban_model_retirement import PREFIX, retired_body

NEUTRAL_ROUTE = 'any qualified route'

# The actual pre-retirement acceptance sentence of bb/t_01a1f03cd752752b (abridged
# to the clauses around its single refused clause), plus the ingress fixtures that
# already pair a pin with non-model gates.
MIXED = [
    ('Acceptance: named failing-before regressions. Mandatory exact-head Opus, source CI and normal '
     'card train gates on every shipping prefix. Never hand merge.',
     ('source CI and normal card train gates on every shipping prefix', 'Never hand merge',
      'Mandatory exact-head ' + NEUTRAL_ROUTE)),
    ('Do not bypass CI. Use only grok-4.6 / xai-oauth.', ('Do not bypass CI.',)),
    ('Get semantic RED. Mandatory exact-head anthropic/claude-opus-5-5 independent review; never hand merge.',
     ('Get semantic RED.', 'independent review; never hand merge.')),
    ('Independent primary Opus review, exact-head CI, normal merge queue.',
     ('exact-head CI, normal merge queue.',)),
    # Non-English obligations are kept verbatim; nothing guesses which prose is a gate.
    ('Fehler beheben. Must use claude-opus-5-5 für die Prüfung; CI grün halten.',
     ('Fehler beheben.', 'für die Prüfung; CI grün halten.')),
    ('Needs fresh independent review (claude-opus-5-5) before merge; keep CI green.',
     ('before merge; keep CI green.',)),
]


def _active(body):
    return '\n'.join(_active_text(line) for line in body.splitlines())


@pytest.mark.parametrize('line,kept', MIXED)
@pytest.mark.parametrize('ending', ['\n', '\r\n', ''])
def test_retired_body_keeps_same_line_gates_active(line, kept, ending):
    with pytest.raises(ValueError):
        validate_text(line)
    body = 'scope\n' + line + ending
    out = retired_body(body)
    validate_text(out)
    assert retired_body(out) == out
    lines = out.splitlines(keepends=True)
    assert lines[0] == 'scope\n'
    # History keeps the exact original line; the active copy follows it.
    assert lines[1] == PREFIX + json.dumps(line, ensure_ascii=False) + (ending or '\n')
    active = _active(out)
    for gate in kept:
        assert gate in active
    assert out.endswith(ending)


@pytest.mark.parametrize('line,kept', MIXED)
def test_forwarded_text_keeps_same_line_gates_active(line, kept):
    out = forwarded_text('lead\n' + line)
    validate_text(out)
    assert forwarded_text(out) == out
    assert PREFIX + json.dumps(line, ensure_ascii=False) in out.splitlines()
    active = _active(out)
    for gate in kept:
        assert gate in active


def test_only_obligation_identity_is_neutralized_not_exclusions_or_provenance():
    line = 'Author was gpt-6.1-sol; exclude: claude-opus-5-5. Use only grok-4.6; keep CI green.'
    with pytest.raises(ValueError):
        validate_text(line)
    out = retired_body(line)
    neutral = out.splitlines()[1]
    assert neutral == ('Author was gpt-6.1-sol; exclude: claude-opus-5-5. Use only '
                       + NEUTRAL_ROUTE + '; keep CI green.')


@pytest.mark.parametrize('identity', ['opus', '**opus**', '_opus_', '__opus__', '`opus`', '*opus*'])
@pytest.mark.parametrize('transform', [retired_body, forwarded_text])
def test_markdown_identity_keeps_active_gates(identity, transform):
    line = f'Must use {identity}; keep CI green. Never hand merge.'
    with pytest.raises(ValueError):
        validate_text(line)
    out = transform(line)
    validate_text(out)
    active = _active(out)
    assert 'keep CI green. Never hand merge.' in active
    assert identity.replace('opus', NEUTRAL_ROUTE) in out.splitlines()[1]
    assert transform(out) == out


def test_forwarding_refuses_unknown_neutralization_without_discarding_gates(monkeypatch):
    from hermes_cli import kanban_model_neutral as policy
    monkeypatch.setattr(policy, 'neutral_line', lambda line: None)
    with pytest.raises(ValueError):
        forwarded_text('Must use opus; keep CI green. Never hand merge.')
    line = 'Must use opus; keep CI green. Never hand merge.'
    assert retired_body(line) == line


def test_nameless_pure_directive_is_quoted_alone():
    assert retired_body('  require_model: true\n') == PREFIX + json.dumps('  require_model: true') + '\n'
    assert forwarded_text('require_model: true') == PREFIX + json.dumps('require_model: true')


def test_f03c_actual_line_restores_every_non_model_gate():
    line = ('Acceptance: named failing-before regressions for actual findings, green complete '
            'focused/integration tests and zero vacuous skips. Mandatory exact-head Opus, source CI and '
            'normal card train gates on every shipping prefix. Merge only when gates pass; no hand merge, '
            'express-lane bypass, or deployment claims without installed proof.')
    active = _active(retired_body(line))
    for gate in ('named failing-before regressions', 'zero vacuous skips', 'source CI and normal card train gates',
                 'no hand merge', 'deployment claims without installed proof'):
        assert gate in active
    assert 'Opus' not in active
