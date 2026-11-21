"""Profile-owned class routes must escape a held pool without changing pins. [daveey]"""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_llm_pacing_dispatch as pd
from hermes_cli import kanban_model_tier as tier
from tests.hermes_cli.test_kanban_pace_reroute import (
    _dispatch, _pools, _route, _route_receipt,
)
from tests.hermes_cli.test_kanban_pace_reroute import board as board, pool as pool  # noqa: PLC0414 -- pytest fixtures


@pytest.fixture
def coach(pool, monkeypatch):
    config, serving = pool
    config['model'] = {'default': 'class:standard', 'provider': 'auto'}
    monkeypatch.setattr('hermes_cli.models_detect.provider_has_credentials', lambda provider: provider in {'xai-oauth', 'anthropic', 'openai-codex'})
    config['model_routing']['routes']['standard'] = [
        _route('xai-oauth', 'grok-4.6'), _route('anthropic', 'claude-sonnet-5-5'),
        _route('openai-codex', 'gpt-6.1-sol')]
    monkeypatch.setattr(tier, 'load_tier_config', lambda: {
        'default_model': 'grok-4.6', 'default_provider': 'xai-oauth',
        'default_model_profiles': ['default']})
    # Dispatcher home has only Grok in standard, unlike the coach profile.
    monkeypatch.setattr('hermes_cli.config_effective.load_user_config_effective', lambda **_kw: {
        'model_routing': {'enabled': True, 'routes': {'standard': [_route('xai-oauth', 'grok-4.6')]}}})
    _pools(monkeypatch, {'xai-oauth': 99, 'anthropic': 5, 'openai-codex': 5})
    def read(provider, now):
        from hermes_cli import kanban_llm_pacing as lp
        return [lp.Reading(now, provider, lp.SEVEN_DAY, 99 if provider == 'xai-oauth' else 5,
                           now + 67 * 3600)]
    monkeypatch.setattr(pd, 'read_pool', read)
    return config, serving


@pytest.mark.parametrize('assignee', ['coach', 'alpha-follow'])
def test_coach_claims_next_tick_on_own_standard_peer(coach, assignee):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='routine work', assignee=assignee)
        seen, result = _dispatch(conn)
        assert seen == [(tid, 'claude-sonnet-5-5', 'anthropic')]
        assert tid not in result.clock_held
        assert tier.owner_model_pin(conn, kb.get_task(conn, tid)) == ('claude-sonnet-5-5', 'anthropic')
        receipt = _route_receipt(conn, tid)
        assert receipt['tier_reason'] == 'profile_out_of_scope+paced:xai-oauth'
        assert kb.get_task(conn, tid).model_override is None


def test_profile_candidates_are_not_dispatcher_home_candidates(coach):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='routine work', assignee='coach')
        assert set(pd.candidate_providers(conn, kb.get_task(conn, tid), 'ready')) == {
            'anthropic', 'openai-codex', 'xai-oauth'}


@pytest.mark.parametrize('pin', [{'model_override': 'grok-4.6', 'provider_override': 'xai-oauth'},
                                  {'model_override': 'grok-4.6'},
                                  {'model_override': 'class:standard', 'provider_override': 'xai-oauth'}])
def test_legacy_profile_pins_are_ignored_and_the_card_escapes_the_held_pool(coach, pin):
    """New concrete pins are refused at ingress; an old row carrying one routes like any default profile card."""
    with pytest.raises(ValueError):
        with kbc.connect() as conn:
            kb.create_task(conn, title='routine work', assignee='coach', model_override='grok-4.6',
                           provider_override='xai-oauth')
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='routine work', assignee='coach')
        conn.execute('UPDATE tasks SET model_override=?, provider_override=? WHERE id=?',
                     (pin['model_override'], pin.get('provider_override'), tid))
        conn.commit()
        seen, result = _dispatch(conn)
        assert seen == [(tid, 'claude-sonnet-5-5', 'anthropic')] and tid not in result.clock_held
        assert kb.get_task(conn, tid).model_override == pin['model_override']  # author provenance retained


def test_no_usable_profile_peer_keeps_hold(coach):
    _, serving = coach
    serving.intersection_update({'xai-oauth'})
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='routine work', assignee='coach')
        seen, result = _dispatch(conn)
        assert seen == [] and tid in result.clock_held
