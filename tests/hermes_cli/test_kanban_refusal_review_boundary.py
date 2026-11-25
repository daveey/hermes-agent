"""Review selection and outbound no-peer-check safety clauses; no provider calls."""
import json
from types import SimpleNamespace

import pytest

from agent import model_routing_managed as managed
from agent.model_routing import ModelRoutingError
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_model_tier as tier
from hermes_cli import kanban_refusal_fallback as refusal
from hermes_cli import kanban_review_model as review
from tests.hermes_cli import test_kanban_refusal_boundary as fixtures

board = fixtures.board
pools = fixtures.pools
claimed = fixtures.claimed


def test_apply_review_selects_unrefused_qualified_candidate(claimed):
    conn, task, _ = claimed
    kb.request_review(conn, task.id)
    task = kb.claim_review_task(conn, task.id, claimer=kb._claimer_id())
    tier.apply_model_tier(conn, task, lane='review')
    assert (task.model_override, task.provider_override) == ('gpt-6-astra', 'openai-codex')
    assert refusal.refused_providers(conn, task.id) == ['anthropic']
    assert kb.get_task(conn, task.id).consecutive_failures == 0


def test_apply_review_all_candidates_refused_fails_closed(claimed):
    conn, task, _ = claimed
    with kb.write_txn(conn):
        for provider in ('openai-codex', 'xai-oauth'):
            conn.execute("INSERT INTO task_runs(task_id,status,started_at,ended_at,outcome,metadata) "
                         "VALUES (?, 'safety_refused', 1, 2, 'safety_refused', ?)",
                         (task.id, json.dumps({'refused_provider': provider})))
    kb.request_review(conn, task.id)
    task = kb.claim_review_task(conn, task.id, claimer=kb._claimer_id())
    with pytest.raises(review.ReviewModelsExhausted, match='No eligible'):
        tier.apply_model_tier(conn, task, lane='review')
    assert not conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='model_tier'", (task.id,)).fetchone()


@pytest.mark.parametrize('provider', ['anthropic', '', 'auto', None])
def test_terminal_request_checks_actual_not_admitted_provider(claimed, monkeypatch, provider):
    conn, task, context = claimed
    kb.complete_task(conn, task.id, summary='Fixture completed without provider calls')
    agent = SimpleNamespace(model='gpt-test', provider=provider,
                            _kanban_refusal_policy=fixtures.policy_for(context))
    monkeypatch.setattr(managed, 'managed_config', lambda: None)
    with pytest.raises(ModelRoutingError, match='refuses execution on'):
        managed.verify_managed_agent(agent, {'model': 'gpt-test'})
    agent.provider = 'openai-codex'
    managed.verify_managed_agent(agent, {'model': 'gpt-test'})


@pytest.mark.parametrize('model,provider', [('class:standard', 'openai-codex'), ('gpt-test', 'auto'),
                                           ('gpt-test', ''), ('gpt-test', None)])
@pytest.mark.parametrize('path', ['request', 'review'])
def test_no_peer_check_still_requires_concrete_tuple(claimed, monkeypatch, model, provider, path):
    from hermes_cli.kanban_refusal_boundary import require_route
    conn, task, context = claimed
    if path == 'request':
        monkeypatch.setattr(managed, 'managed_config', lambda: None)
        agent = SimpleNamespace(model=model, provider=provider,
                                _kanban_refusal_policy=fixtures.policy_for(context))
        with pytest.raises(ModelRoutingError, match='concrete execution route'):
            managed.verify_managed_agent(agent, {'model': model})
    else:
        with pytest.raises(ModelRoutingError, match='concrete execution route'):
            require_route(conn, task, model, provider, lane='review')
