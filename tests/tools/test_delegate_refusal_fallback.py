"""A subagent refused by its provider is re-run unchanged on the class's other providers, then fails. [daveey]"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent import kanban_delegation_policy as policy
from hermes_cli import kanban_refusal_fallback as fb
from tools import delegate_tool, delegate_tool_refusal as refusal
from tools.delegate_tool_dispatch import _Batch

REFUSAL = {'status': 'failed', 'failure_reason': 'content_policy_blocked', 'summary': '', 'task_index': 0}


def entry(**kw):
    return {'status': 'completed', 'summary': 'done', 'task_index': 0, **kw}


@pytest.fixture
def rig(monkeypatch):
    """Class peers anthropic/openai-codex/xai-oauth; each child is tagged with its provider; calls recorded."""
    calls = []
    behaviour = {}  # provider -> entry returned by a child on it

    monkeypatch.setattr(policy, 'origin_for', lambda parent: None)
    monkeypatch.setattr(fb, 'class_peers', lambda task, model: [
        ('claude-opus-5-5', 'anthropic'), ('gpt-6-astra', 'openai-codex'), ('grok-4.7', 'xai-oauth')])
    monkeypatch.setattr('tools.delegate_tool_routes.route_for_config',
                        lambda cfg, parent: SimpleNamespace(**cfg))

    def build(task_list, schemas, routes, **kw):
        route = routes[0]
        child = SimpleNamespace(model=route.model, provider=route.provider)
        calls.append(('build', route.provider, dict(task_list[0])))
        return [(0, task_list[0], child)], None

    monkeypatch.setattr(delegate_tool, '_build_children', build)

    def run_once(self, i, task, child):
        calls.append(('run', child.provider, dict(task)))
        return dict(behaviour.get(child.provider, entry()))

    parent = SimpleNamespace(_profile_name=None)
    batch = SimpleNamespace(parent_agent=parent, top_role='leaf')
    batch._run_once = lambda i, task, child: run_once(batch, i, task, child)
    return SimpleNamespace(batch=batch, calls=calls, behaviour=behaviour)


def run(rig, task, provider='anthropic'):
    child = SimpleNamespace(model='claude-opus-5-5', provider=provider)
    return _Batch.run_child(rig.batch, 0, task, child)


def test_subagent_retries_unchanged_on_another_provider(rig):
    task = {'goal': 'Refactor X.', 'context': 'Exact context \u00e9\n'}
    rig.behaviour['anthropic'] = dict(REFUSAL)
    out = run(rig, task)
    assert out['status'] == 'completed' and out['refusal_fallback'] == {
        'refused_providers': ['anthropic'], 'provider': 'openai-codex'}
    runs = [c for c in rig.calls if c[0] == 'run']
    assert [c[1] for c in runs] == ['anthropic', 'openai-codex']  # first peer after the refuser, not the same provider
    assert runs[0][2] == runs[1][2] == task  # byte-identical goal/context, no note about the refusal
    assert str(task) == "{'goal': 'Refactor X.', 'context': 'Exact context \u00e9\\n'}"


def test_subagent_tries_each_other_provider_once_then_returns_the_failure(rig):
    rig.behaviour.update({p: dict(REFUSAL) for p in ('anthropic', 'openai-codex', 'xai-oauth')})
    out = run(rig, {'goal': 'g'})
    assert out['failure_reason'] == 'content_policy_blocked' and out['status'] == 'failed'
    assert out['refused_providers'] == ['anthropic', 'openai-codex', 'xai-oauth']
    assert [c[1] for c in rig.calls if c[0] == 'run'] == ['anthropic', 'openai-codex', 'xai-oauth']


def test_second_provider_refusal_falls_through_to_third(rig):
    rig.behaviour.update({'anthropic': dict(REFUSAL), 'openai-codex': dict(REFUSAL)})
    out = run(rig, {'goal': 'g'})
    assert out['status'] == 'completed'
    assert out['refusal_fallback'] == {'refused_providers': ['anthropic', 'openai-codex'], 'provider': 'xai-oauth'}


def test_other_failures_and_success_are_not_retried(rig):
    rig.behaviour['anthropic'] = {'status': 'failed', 'failure_reason': 'rate_limit', 'task_index': 0}
    assert run(rig, {'goal': 'g'})['failure_reason'] == 'rate_limit'
    rig.behaviour['anthropic'] = entry()
    assert run(rig, {'goal': 'g'})['status'] == 'completed'
    assert [c[1] for c in rig.calls if c[0] == 'run'] == ['anthropic', 'anthropic']


def test_kanban_origin_parent_is_left_to_the_card_path(rig, monkeypatch):
    monkeypatch.setattr(policy, 'origin_for', lambda parent: object())
    rig.behaviour['anthropic'] = dict(REFUSAL)
    assert run(rig, {'goal': 'g'})['failure_reason'] == 'content_policy_blocked'
    assert [c[1] for c in rig.calls if c[0] == 'run'] == ['anthropic']
