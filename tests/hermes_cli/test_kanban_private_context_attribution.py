"""Context uncertainty must not resurrect a resolved private stop. [daveey]"""
from types import SimpleNamespace

import pytest

from hermes_cli.kanban_actionability_private import held
from hermes_cli.kanban_actionability_restrictions import details


@pytest.mark.parametrize('context', [
    {'kind': 'scope_renewed', 'payload': {'covered_restriction_uids': ['scope']}},
    {'kind': 'scope_renewed', 'payload': {}},
    {'kind': 'scope_renewed', 'payload': 'not-json'},
    {'kind': 'stop_released', 'payload': 'not-json'},
    {'kind': 'stop_released', 'payload': {'cause': 'safety_refusal', 'episode': 'other'}},
])
def test_unrelated_context_does_not_resurrect_resolved_budget(context):
    events = [
        {'uid': 'budget', 'kind': 'spend_budget_triaged', 'ord': 1, 'payload': {}},
        {'uid': 'promotion', 'kind': 'promoted_manual', 'ord': 2, 'payload': {}},
        {'uid': 'scope', 'kind': 'spend_budget_triaged', 'ord': 3,
         'payload': {'cause': 'scope_stopped'}},
        {'uid': 'context', 'ord': 4, **context},
    ]
    reader = SimpleNamespace(synced=False, events=lambda _tid: [])
    assert held('ready', events[:2]) is None
    assert held('ready', events) is not None  # Aggregate uncertainty stays fail-closed.
    rows = details({'id': 'fixture'}, reader, events, frozenset(), frozenset())
    assert [row['source_uid'] for row in rows] == ['scope']
    assert rows[0]['reason'] == (
        'mandate_unverifiable' if context['payload'] == {'covered_restriction_uids': ['scope']}
        else 'scope_stop_not_renewed')


@pytest.mark.parametrize('covered', [['scope'], ['scope', 'other']])
@pytest.mark.parametrize('verified,expected', [(frozenset(), ['scope']), (frozenset({'scope'}), [])])
def test_exact_renewal_keeps_verified_mandate_and_decision_requirements(verified, expected, covered):
    events = [
        {'uid': 'scope', 'kind': 'spend_budget_triaged', 'ord': 1,
         'payload': {'cause': 'scope_stopped'}},
        {'uid': 'renewal', 'kind': 'scope_renewed', 'ord': 2,
         'payload': {'covered_restriction_uids': covered}},
        {'uid': 'promotion', 'kind': 'promoted_manual', 'ord': 3, 'payload': {}},
    ]
    reader = SimpleNamespace(synced=False, events=lambda _tid: [])
    rows = details({'id': 'fixture'}, reader, events, verified, frozenset())
    assert [row['source_uid'] for row in rows] == expected
    assert [row['source_uid'] for row in details(
        {'id': 'fixture'}, reader, events[:2], frozenset({'scope'}), frozenset())] == ['scope']


@pytest.mark.parametrize('covered', [['scope', None], ['scope', ''], ['scope', 1], 'scope', None, []])
def test_malformed_targeted_renewal_is_never_valid_resolution(covered):
    events = [
        {'uid': 'scope', 'kind': 'spend_budget_triaged', 'ord': 1,
         'payload': {'cause': 'scope_stopped'}},
        {'uid': 'renewal', 'kind': 'scope_renewed', 'ord': 2,
         'payload': {'covered_restriction_uids': covered}},
        {'uid': 'promotion', 'kind': 'promoted_manual', 'ord': 3, 'payload': {}},
    ]
    reader = SimpleNamespace(synced=False, events=lambda _tid: [])
    row, = details({'id': 'fixture'}, reader, events, frozenset({'scope'}), frozenset())
    assert row['source_uid'] == 'scope'
    assert row['reason'] == ('renewal_unverifiable' if isinstance(covered, list) and 'scope' in covered
                             else 'scope_stop_not_renewed')
    if row['reason'] == 'renewal_unverifiable':
        assert row['release']['authority'] == 'unavailable'


@pytest.mark.parametrize('kind', ['promoted_manual', 'archived'])
def test_decisions_remain_resolution_context(kind):
    from hermes_cli.kanban_actionability_restrictions import _context

    decision = {'uid': 'decision', 'kind': kind, 'ord': 1, 'payload': {}}
    unrelated = {'uid': 'unrelated', 'kind': 'created', 'ord': 2,
                 'payload': {'episode': 'scope', 'covered_restriction_uids': ['scope']}}
    assert list(_context([decision, unrelated], 'scope')) == [decision]


@pytest.mark.parametrize('episode,expected', [('capability', []), ('other', ['capability'])])
def test_only_exact_authenticated_release_resolves_capability(episode, expected):
    events = [
        {'uid': 'capability', 'kind': 'spend_budget_triaged', 'ord': 1,
         'payload': {'cause': 'capability_unavailable'}},
        {'uid': 'release', 'kind': 'stop_released', 'ord': 2,
         'payload': {'version': 1, 'episode': episode}},
        {'uid': 'promotion', 'kind': 'promoted_manual', 'ord': 3, 'payload': {}},
    ]
    reader = SimpleNamespace(synced=False, events=lambda _tid: [])
    rows = details({'id': 'fixture'}, reader, events, frozenset(), frozenset({'release'}))
    assert [row['source_uid'] for row in rows] == expected
    assert [row['source_uid'] for row in details(
        {'id': 'fixture'}, reader, events, frozenset(), frozenset())] == ['capability']
