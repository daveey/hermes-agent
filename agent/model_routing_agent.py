"""Common AIAgent class-selection boundary, before any native client is built."""
from agent.model_routing import is_class_selector


def class_requirements(config):
    """Construction-time capability floor every AIAgent class allocation applies.

    Shared with non-allocating probes (Kanban quota reroutes) so a probe never offers a
    route the worker's own allocator would refuse."""
    model_config = (config or {}).get("model", {})
    requirements = {"tools": True, "streaming": True}
    if isinstance(model_config, dict) and model_config.get("context_length"):
        requirements["min_context_tokens"] = model_config["context_length"]
    return requirements


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
    requirements = class_requirements(config)
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
        from contextlib import nullcontext
        from agent.model_routing_runtime import temporary_resolution
        scope = temporary_resolution() if isinstance(getattr(agent, '_one_turn_runtime', None), dict) else nullcontext()
        with scope:
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
    # The failover is committed in memory and in the routing allocation store; the session-row mirror is
    # best-effort here. A failed mirror write must not turn a recoverable route failure (402, 429) into a
    # dead turn: the retry continues on the new route and the next persist re-syncs the row.
    try:
        persist_class_metadata(agent)
    except Exception:
        import logging
        logging.getLogger(__name__).warning(
            "Class failover to %s/%s: session row mirror failed; continuing on the new route",
            agent.provider, agent.model, exc_info=True)
    return True


def adopt_managed_policy(agent):
    """Bring an agent built before managed enforcement was switched on under the policy.

    ``enforce_subscription_only`` is read live, but subscription provenance is only minted when
    an agent is constructed, so every agent already alive when an operator enables the policy
    (gateway caches, Desktop sessions, running Kanban workers) would fail closed at its next
    request with "lacks verified subscription provenance". Before a request is shaped, re-resolve
    the agent's own concrete tuple as a managed exact pin and rebuild its client from the fenced
    runtime. Unapproved tuples (paid keys, aggregators, unknown models) still raise before any
    send, and a paid credential the legacy agent held is replaced by the subscription pool.
    Returns True only when provenance was adopted by this call.
    """
    if getattr(agent, "model_routing", None):
        return False
    if isinstance(getattr(agent, "_one_turn_runtime", None), dict):
        return False  # a temporary switch owns the runtime; its own guards apply
    from agent.model_routing_managed import managed_config, resolve_managed_runtime
    config = managed_config()
    if config is None:
        return False
    from agent.agent_init import _build_client, _snapshot_primary_runtime
    from agent.agent_runtime_helpers import sync_credential_pool_entry_id

    requirements = dict(getattr(agent, "_model_routing_requirements", None) or {"tools": True, "streaming": True})
    old_client = getattr(agent, "client", None)
    old_anthropic_client = getattr(agent, "_anthropic_client", None)
    previous = vars(agent).copy()

    def prepare(runtime):
        agent.model, agent.provider = runtime["model"], runtime["provider"]
        agent.base_url, agent.api_mode = runtime["base_url"], runtime["api_mode"]
        agent._credential_pool = runtime["credential_pool"]
        _build_client(agent, runtime["api_key"], runtime["base_url"], [])

    try:
        runtime = resolve_managed_runtime(agent.model, agent.provider, config=config,
                                          session_id=agent.session_id, requirements=requirements,
                                          prepare_runtime=prepare)
    except BaseException:
        vars(agent).clear()
        vars(agent).update(previous)
        raise
    agent.model_routing = runtime["model_routing"]
    agent._model_routing_requirements = requirements
    agent._fallback_chain = []
    agent._fallback_model = None
    if old_client is not None and old_client is not agent.client:
        agent._retire_shared_openai_client(old_client, reason="managed_policy_adoption")
    if old_anthropic_client is not None and old_anthropic_client is not getattr(agent, "_anthropic_client", None):
        import logging
        try:
            old_anthropic_client.close()
        except Exception:
            logging.getLogger(__name__).debug("Retired legacy client close failed", exc_info=True)
    sync_credential_pool_entry_id(agent)
    if hasattr(agent, "_transport_cache"):
        agent._transport_cache.clear()
    _snapshot_primary_runtime(agent)
    persist_class_metadata(agent)
    import logging
    logging.getLogger(__name__).info(
        "Adopted managed subscription policy for live agent session=%s route=%s/%s",
        agent.session_id, agent.provider, agent.model)
    return True


def link_compression_route(agent, parent_session_id, child_session_id):
    """Only compression publication/adoption may join main-agent allocations."""
    from agent.agent_runtime_once import standing_runtime
    if not getattr(standing_runtime(agent), "model_routing", None):
        return
    from agent.model_routing_state import RoutingState
    from hermes_constants import get_hermes_home

    RoutingState(get_hermes_home() / "model-routing.sqlite3").link_continuation(
        child_session_id, parent_session_id)


def persist_class_metadata(agent):
    """Keep the ordinary session row in sync with the durable allocation store."""
    if isinstance(getattr(agent, '_one_turn_runtime', None), dict):
        return
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
    requested_class = (getattr(agent, "model_routing", None) or {}).get("requested_class")
    if requested_class:
        runtime["requested_class"] = requested_class
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
