"""Common AIAgent class-selection boundary, before any native client is built."""
from agent.model_routing import is_class_selector


def prepare_agent_route(agent, model, provider, session_id, api_key, base_url,
                        api_mode, credential_pool, fallback_model):
    """Resolve only class selectors; concrete user pins retain legacy semantics.

    Parent sessions here can be delegated jobs, not compression continuations, so
    they must not automatically share allocations. Compression publication links
    rotated IDs explicitly before exposing its child in SessionDB.
    """
    agent.model_routing = None
    from agent.model_routing_managed import managed_config
    managed = managed_config()
    if not is_class_selector(model) and managed is None:
        return (model, provider, session_id, api_key, base_url, api_mode,
                credential_pool, fallback_model)
    from hermes_state_ids import new_session_id
    from hermes_cli.config_effective import load_user_config_effective
    from hermes_cli.runtime_provider import resolve_runtime_provider

    config = managed or load_user_config_effective()
    model_config = config.get("model", {})
    requirements = {"tools": True, "streaming": True}
    if isinstance(model_config, dict) and model_config.get("context_length"):
        requirements["min_context_tokens"] = model_config["context_length"]
    session_id = session_id or new_session_id()
    runtime = resolve_runtime_provider(
        requested=provider or "auto", target_model=model,
        explicit_api_key=None if managed else api_key, explicit_base_url=None if managed else base_url,
        routing_session_id=session_id, routing_config=config,
        routing_requirements=requirements)
    agent.model_routing = runtime["model_routing"]
    agent._model_routing_requirements = requirements
    return (runtime["model"], runtime["provider"], session_id,
            runtime["api_key"], runtime["base_url"], runtime["api_mode"],
            runtime["credential_pool"], [])


def begin_recovery_episode(agent):
    """Reset turn-local attempts, without reallocating a sticky class route.

    Return true when the primary is already active and needs no restoration.
    Pool cooldowns remain authoritative even though previous attempts are reset.
    """
    if getattr(agent, "model_routing", None):
        agent._model_routing_failed = set()
    if agent._fallback_activated:
        return False
    from agent.agent_runtime_helpers import _revert_credential_rotation
    # Even failed fallback walks advance the legacy index; reset at the next turn.
    agent._fallback_index = 0
    _revert_credential_rotation(agent)
    return True


def try_class_fallback(agent):
    """Called only at the existing failed-request recovery boundary, never per tool.

    No generic fallback is permitted for a class request, including exhaustion.
    A replacement is the session's new sticky primary, not a temporary override.
    """
    from agent.model_routing import ModelRoutingError
    from agent.model_routing_runtime import resolve_class_runtime
    from agent.agent_init import _build_client, _snapshot_primary_runtime
    from agent.agent_runtime_helpers import sync_credential_pool_entry_id
    from agent.chat_completion_helpers import (
        _reset_stale_streak, _update_fallback_context_compressor,
        _reresolve_fallback_reasoning_config, _rescope_fallback_extra_body,
    )

    if agent.model_routing.get("exact_pin"):
        return False
    failed = getattr(agent, "_model_routing_failed", set())
    failed.add((agent.provider, agent.model))
    agent._model_routing_failed = failed
    requirements = dict(agent._model_routing_requirements)
    context = getattr(getattr(agent, "context_compressor", None), "context_length", 0)
    if isinstance(context, int) and context > 0:
        requirements["min_context_tokens"] = max(requirements.get("min_context_tokens", 0), context)
    old_model, old_provider, old_url = agent.model, agent.provider, agent.base_url
    old_client = agent.client
    old_anthropic_client = getattr(agent, "_anthropic_client", None)
    previous = vars(agent).copy()
    building_client = False

    def prepare(runtime):
        nonlocal building_client
        building_client = True
        agent.model, agent.provider = runtime["model"], runtime["provider"]
        agent.base_url, agent.api_mode = runtime["base_url"], runtime["api_mode"]
        agent.requested_provider = "auto"
        agent._credential_pool = runtime["credential_pool"]
        _build_client(agent, runtime["api_key"], runtime["base_url"], [])

    try:
        runtime = resolve_class_runtime(
            "class:" + agent.model_routing["requested_class"], "auto",
            session_id=agent.session_id, requirements=requirements, excluded=failed,
            prepare_runtime=prepare)
    except Exception as exc:
        vars(agent).clear()
        vars(agent).update(previous)
        if isinstance(exc, ModelRoutingError) and not building_client:
            return False
        raise ModelRoutingError("Class failover client construction failed; no legacy fallback permitted") from exc
    agent.model_routing = runtime["model_routing"]
    if old_client is not None and old_client is not agent.client:
        agent._retire_shared_openai_client(old_client, reason="class_failover")
    if old_anthropic_client is not None and old_anthropic_client is not agent._anthropic_client:
        # Native streams own separate request clients; this idle construction
        # client is no longer reachable after the new runtime is committed.
        import logging
        try:
            old_anthropic_client.close()
        except Exception:
            logging.getLogger(__name__).debug("Retired class client close failed", exc_info=True)
    sync_credential_pool_entry_id(agent)
    if hasattr(agent, "_transport_cache"):
        agent._transport_cache.clear()
    agent._use_prompt_caching, agent._use_native_cache_layout = agent._anthropic_prompt_cache_policy(
        provider=agent.provider, base_url=agent.base_url, api_mode=agent.api_mode, model=agent.model)
    _update_fallback_context_compressor(agent)
    _reresolve_fallback_reasoning_config(agent)
    _rescope_fallback_extra_body(agent, old_model, old_provider, old_url)
    _reset_stale_streak(agent)
    agent._fallback_activated = False
    _snapshot_primary_runtime(agent)
    persist_class_metadata(agent)
    return True


def link_compression_route(agent, parent_session_id, child_session_id):
    """Only compression publication/adoption may join main-agent allocations."""
    if not getattr(agent, "model_routing", None):
        return
    from agent.model_routing_state import RoutingState
    from hermes_constants import get_hermes_home

    RoutingState(get_hermes_home() / "model-routing.sqlite3").link_continuation(
        child_session_id, parent_session_id)


def persist_class_metadata(agent):
    """Keep the ordinary session row in sync with the durable allocation store."""
    metadata = getattr(agent, "model_routing", None)
    config = getattr(agent, "_session_init_model_config", None)
    if isinstance(config, dict):
        config["model_routing"] = metadata
    db = getattr(agent, "_session_db", None)
    if db and getattr(agent, "_session_db_created", False):
        db.update_session_model(agent.session_id, agent.model)
        db.patch_session_model_config(agent.session_id, {"model_routing": metadata})


def clear_class_route(agent):
    if not getattr(agent, "model_routing", None):
        return
    agent.model_routing = None
    persist_class_metadata(agent)


def current_main_runtime(agent):
    runtime = {key: getattr(agent, key, "") or "" for key in (
        "model", "provider", "base_url", "api_key", "api_mode", "auth_mode", "session_id")}
    runtime["requested_class"] = (getattr(agent, "model_routing", None) or {}).get("requested_class", "")
    return runtime


def routing_model_config(agent):
    metadata = getattr(agent, "model_routing", None)
    return {"model_routing": metadata} if metadata else {}


def publish_session_id(session_id):
    """Publish context-local identity without leaking delegated IDs process-wide."""
    import os
    import logging
    logger = logging.getLogger(__name__)
    try:
        from gateway.session_context import set_current_session_id
        set_current_session_id(session_id)
    except Exception:
        logger.debug("Context-local session publication failed", exc_info=True)
        try:
            from agent.delegation_context import is_delegated_child_context
            delegated_child = is_delegated_child_context()
        except Exception:
            logger.debug("Delegation context lookup failed", exc_info=True)
            delegated_child = False
        if not delegated_child:
            os.environ["HERMES_SESSION_ID"] = session_id
