"""One-turn runtime ownership, separate from the standing session allocation."""
from copy import copy, deepcopy

_MISSING = object()


def _apply_snapshot(agent, snapshot):
    for key, value in snapshot.items():
        if value is _MISSING:
            agent.__dict__.pop(key, None)
        else:
            setattr(agent, key, value)


def snapshot_runtime(agent):
    from agent.agent_runtime_helpers import _SWITCH_SNAPSHOT_FIELDS
    fields = (*_SWITCH_SNAPSHOT_FIELDS, '_client_kwargs', 'model_routing',
              '_model_routing_requirements', '_model_routing_failed', '_primary_runtime',
              'reasoning_config', 'request_overrides', 'context_compressor',
              '_use_prompt_caching', '_use_native_cache_layout', '_custom_providers',
              '_fallback_chain', '_fallback_model', '_fallback_activated', '_fallback_index',
              '_provider_fallback_active', '_provider_fallback_route', '_credential_pool_revert_id',
              '_cached_system_prompt', '_transport_cache', '_rate_limited_until', '_consecutive_stale_streams',
              '_compression_feasibility_checked', '_compression_warning', '_last_feasibility_notice')
    # SDK clients, pools and compressor are owned handles, not deepcopy-able data.
    # Preserve absence too: a temporary probe can create a warning on first use.
    values = {key: getattr(agent, key, _MISSING) for key in fields}
    return {key: copy(value) if key == '_transport_cache' and value is not _MISSING else
            deepcopy(value) if isinstance(value, (dict, list, set)) else value
            for key, value in values.items()}


def standing_runtime(agent):
    """Persistence uses the standing identity even while a temporary turn is running."""
    snapshot = getattr(agent, '_one_turn_runtime', None)
    if not isinstance(snapshot, dict):
        return agent
    view = copy(agent)
    _apply_snapshot(view, snapshot)
    return view


def restore_runtime(agent, snapshot):
    _apply_snapshot(agent, snapshot)
    agent.__dict__.pop('_one_turn_runtime', None)
    # A compression rotation may have changed identity while the temporary route ran.
    compressor = getattr(agent, 'context_compressor', None)
    if (compressor is not None and hasattr(compressor, 'bind_session_state')
            and getattr(compressor, '_session_id', None) != agent.session_id):
        compressor.bind_session_state(getattr(agent, '_session_db', None), agent.session_id)
        # Compression is the sanctioned prompt rebuild. Do not resurrect the
        # pre-compression prompt snapshot when returning to the standing model.
        agent._cached_system_prompt = None
