"""Bot Chat policy following composed with durable exact-pin allocation and managed workers."""
import json
from pathlib import Path

import pytest

from tests.gateway.test_model_switch_worker_composed import (
    home, OPUS, ASTRA, _owner, _noop, _worker_build, _allocation, _pinned_opus,
)
from tests.gateway.test_bot_chat_follows_profile import _open, _legacy


@pytest.fixture(autouse=True)
def _state_db_in_profile(home, monkeypatch):
    """The owner's state.db lives in its profile home, as in production: ``profile_config`` reads the
    profile's config.yaml beside it. The root conftest re-points ``hermes_state.DEFAULT_DB_PATH`` to its
    own sandbox once ``hermes_state`` is imported, so without this only a worker's first test followed."""
    import hermes_state
    monkeypatch.setattr(hermes_state, 'DEFAULT_DB_PATH', home / 'state.db')


def _profile_pick(home, model, provider):
    from hermes_cli.config import atomic_config_write
    from hermes_cli.config import read_user_config_raw
    config = read_user_config_raw(home / 'config.yaml')
    config['model'].update(default=model, provider=provider)
    atomic_config_write(home / 'config.yaml', config)


@pytest.mark.asyncio
@pytest.mark.parametrize('entry', ['resume', 'adoption'])
@pytest.mark.parametrize('held', [False, True], ids=['follow', 'composer-hold'])
async def test_managed_bot_follows_profile_across_exact_pins(home, monkeypatch, entry, held):
    from gateway.session_local_migration import resolve_local_target
    from gateway.session_policy import policy_for_source
    from run_agent import AIAgent
    store, db, authority, owner, ref = _owner(home, monkeypatch, _noop)
    assert Path(db.db_path).resolve().parent == home.resolve()
    authority.runner._peek_session_state = lambda key: None
    try:
        if entry == 'resume':
            _pinned_opus(home, authority, db, ref, monkeypatch)
            db.set_session_title(ref.session_id, 'Bot Chat')
            if held:
                db.patch_session_model_config(ref.session_id, {
                    'composer_override_profile': {'model': ASTRA, 'provider': 'openai-codex'}})
        else:
            sid = 'legacy-bot'
            saved = {'provider': 'anthropic'}
            if held:
                saved['composer_override_profile'] = {'model': ASTRA, 'provider': 'openai-codex'}
            _legacy(db, home, sid, source='gui', model=OPUS, title='Bot Chat', model_config=saved)
            AIAgent(model=OPUS, provider='anthropic', session_id=sid, session_db=db,
                    quiet_mode=True, skip_memory=True, skip_context_files=True)
            assert _allocation(home, sid)['exact_pin']
        _profile_pick(home, ASTRA, 'openai-codex')
        if entry == 'adoption':
            authority._native_legacy_transports = {owner.actor.transport_id: owner.actor}
            ref = resolve_local_target(authority, owner.actor, sid)
        await _open(authority, ref)
        policy = policy_for_source(authority.runner, authority.sessions[ref.session_id].source)
        assert policy.follows_profile
        assert policy.effective(authority)[1] == (OPUS if held else ASTRA)
        agent = _worker_build(authority, db, ref, monkeypatch, 'follow-first')
        assert (agent.model, agent.provider) == ((OPUS, 'anthropic') if held else (ASTRA, 'openai-codex'))
        # No further resume: the already-following policy must take later profile changes too.
        _profile_pick(home, OPUS, 'anthropic')
        agent = _worker_build(authority, db, ref, monkeypatch, 'follow-second')
        assert (agent.model, agent.provider) == (OPUS, 'anthropic')
        _profile_pick(home, ASTRA, 'openai-codex')
        agent = _worker_build(authority, db, ref, monkeypatch, 'follow-third')
        assert (agent.model, agent.provider) == (ASTRA, 'openai-codex')
        assert _allocation(home, ref.session_id)['selected_model'] == ASTRA
        # The held pick's end is durable: the receipt no longer records it, so a restarted owner agrees.
        assert 'selection_profile' not in _receipt_model(db, ref)
    finally:
        await owner.close()
        store.close_all_db_handles()


def _receipt_model(db, ref):
    from hermes_state_local import local_receipt
    return json.loads(local_receipt(db, ref.session_id)['policy']['config_json']).get('model') or {}


@pytest.mark.asyncio
async def test_new_pick_after_ended_hold_holds_until_next_profile_edit(home, monkeypatch):
    """An ended composer hold does not swallow a later explicit pick: the pick holds against the profile
    target it was made on and ends, durably, on the next profile edit."""
    from tests.gateway.test_model_switch_worker_composed import _mutate
    store, db, authority, owner, ref = _owner(home, monkeypatch, _noop)
    authority.runner._peek_session_state = lambda key: None
    try:
        _pinned_opus(home, authority, db, ref, monkeypatch)
        db.set_session_title(ref.session_id, 'Bot Chat')
        db.patch_session_model_config(ref.session_id, {
            'composer_override_profile': {'model': ASTRA, 'provider': 'openai-codex'}})
        _profile_pick(home, ASTRA, 'openai-codex')
        await _open(authority, ref)
        assert _worker_build(authority, db, ref, monkeypatch, 'held').model == OPUS
        _profile_pick(home, OPUS, 'anthropic')
        assert _worker_build(authority, db, ref, monkeypatch, 'ended').model == OPUS
        assert 'selection_profile' not in _receipt_model(db, ref)
        reply = await _mutate(owner, db, ref, 'repick', {'model': ASTRA, 'provider': 'openai-codex'})
        assert (reply['result']['model'], reply['result'].get('status')) == (ASTRA, None), reply
        assert _receipt_model(db, ref)['selection_profile'] == [OPUS, 'anthropic']
        agent = _worker_build(authority, db, ref, monkeypatch, 'repicked')
        assert (agent.model, agent.provider) == (ASTRA, 'openai-codex')
        _profile_pick(home, OPUS, 'anthropic')  # a rewrite that keeps the target keeps the pick
        assert _worker_build(authority, db, ref, monkeypatch, 'still-held').model == ASTRA
        assert _receipt_model(db, ref)['selection_profile'] == [OPUS, 'anthropic']
    finally:
        await owner.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_ordinary_exact_pin_does_not_follow_profile(home, monkeypatch):
    from agent.model_routing import ModelRoutingError
    from run_agent import AIAgent
    store, db, authority, owner, ref = _owner(home, monkeypatch, _noop)
    try:
        before = _pinned_opus(home, authority, db, ref, monkeypatch)
        _profile_pick(home, ASTRA, 'openai-codex')
        assert _worker_build(authority, db, ref, monkeypatch, 'ordinary').model == OPUS
        with pytest.raises(ModelRoutingError, match='cannot fail over'):
            AIAgent(model=ASTRA, provider='openai-codex', session_id=ref.session_id, session_db=db,
                    quiet_mode=True, skip_memory=True, skip_context_files=True)
        assert _allocation(home, ref.session_id) == before
    finally:
        await owner.close()
        store.close_all_db_handles()
