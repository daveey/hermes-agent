"""One-turn runtime ownership, separate from the standing session allocation."""
from copy import copy, deepcopy


def snapshot_runtime(agent):
    from agent.agent_runtime_helpers import _SWITCH_SNAPSHOT_FIELDS
    fields = (*_SWITCH_SNAPSHOT_FIELDS, '_client_kwargs', 'model_routing',
              '_model_routing_requirements', '_model_routing_failed', '_primary_runtime',
              'reasoning_config', 'request_overrides', 'context_compressor',
              '_use_prompt_caching', '_use_native_cache_layout', '_custom_providers',
              '_fallback_chain', '_fallback_model', '_fallback_activated', '_fallback_index',
              '_provider_fallback_active', '_provider_fallback_route', '_credential_pool_revert_id',
              '_cached_system_prompt', '_transport_cache', '_rate_limited_until')
    # SDK clients, pools and compressor are owned handles, not deepcopy-able data.
    values = {key: getattr(agent, key) for key in fields if hasattr(agent, key)}
    return {key: copy(value) if key == '_transport_cache' else
            deepcopy(value) if isinstance(value, (dict, list, set)) else value
            for key, value in values.items()}


def standing_runtime(agent):
    """Persistence uses the standing identity even while a temporary turn is running."""
    snapshot = getattr(agent, '_one_turn_runtime', None)
    if not isinstance(snapshot, dict):
        return agent
    view = copy(agent)
    for key, value in snapshot.items():
        setattr(view, key, value)
    return view


def restore_runtime(agent, snapshot):
    for key, value in snapshot.items():
        setattr(agent, key, value)
    agent.__dict__.pop('_one_turn_runtime', None)
    # A compression rotation may have changed identity while the temporary route ran.
    compressor = getattr(agent, 'context_compressor', None)
    if (compressor is not None and hasattr(compressor, 'bind_session_state')
            and getattr(compressor, '_session_id', None) != agent.session_id):
        compressor.bind_session_state(getattr(agent, '_session_db', None), agent.session_id)
        # Compression is the sanctioned prompt rebuild. Do not resurrect the
        # pre-compression prompt snapshot when returning to the standing model.
        agent._cached_system_prompt = None
