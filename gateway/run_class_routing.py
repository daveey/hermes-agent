"""Class-aware messaging preflight; allocation belongs to the common agent boundary."""
from agent.model_routing import is_class_selector


def class_session_runtime(runner, model, override, source, user_config, session_key=None):
    """Honor session/channel precedence before authenticating the global default.

    Return None for the unchanged legacy path. A class marker stays unresolved until
    AIAgent has the durable conversation ID; readiness/hygiene probes never allocate.
    """
    from gateway.run import _resolve_runtime_agent_kwargs_for_provider
    from hermes_cli.runtime_provider import _get_model_config

    channel = channel_override(runner, source)
    if not override:
        restored = stored_class(runner, session_key)
        if restored:
            return restored, {"provider": "auto", "requested_provider": "auto"}
    candidate = (override or {}).get("model") or (channel.model if channel else None) or model
    if not (is_class_selector(candidate) or is_class_selector(model)):
        return None
    if override:
        provider = override.get("provider") or "auto"
    elif channel and (channel.model or channel.provider):
        provider = channel.provider or "auto"
    else:
        model_cfg = user_config.get("model", {}) if user_config is not None else _get_model_config()
        provider = model_cfg.get("provider") or "auto" if isinstance(model_cfg, dict) else "auto"
    if is_class_selector(candidate):
        return candidate, {"provider": provider, "requested_provider": provider}
    # An explicit concrete selection over a class default must not authenticate
    # that default or silently switch to its fallback when the pin is unavailable.
    runtime = _resolve_runtime_agent_kwargs_for_provider(provider, target_model=candidate)
    runtime.pop("model", None)
    return candidate, runtime


def stored_class(runner, session_key):
    """Resume class provenance, not a new global/channel default, after restart."""
    import json
    store = getattr(runner, "session_store", None)
    entry = getattr(store, "_entries", {}).get(session_key) if session_key else None
    db = getattr(runner, "_session_db", None)
    db = getattr(db, "_db", db)
    if entry is None or db is None:
        return None
    row = db.get_session(entry.session_id)
    if not isinstance(row, dict):
        return None
    config = row.get("model_config") or {}
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except (ValueError, TypeError):
            return None
    metadata = config.get("model_routing") if isinstance(config, dict) else None
    if (isinstance(metadata, dict) and metadata.get("requested_class") in {"deep", "standard", "fast"}
            and metadata.get("selected_model") == row.get("model")):
        return "class:" + metadata["requested_class"]
    return None


def class_context_length(model, *, config=None, runner=None, session_key=None, provider="auto"):
    """Read the sticky window, or the conservative eligible window before allocation.

    Registry limits are transport-specific (Codex can be smaller than the generic
    model catalog). Never authenticate or advance allocation from a context probe.
    """
    from agent.model_routing import CLASSES, ModelRoutingError, eligible_routes, validate_policy
    from agent.model_routing_state import RoutingState
    from hermes_constants import get_hermes_home
    from hermes_cli.config_effective import load_user_config_effective

    if not is_class_selector(model):
        return None
    level = model.removeprefix("class:")
    config = config if config is not None else load_user_config_effective()
    policy = config.get("model_routing", {})
    validate_policy(policy)
    if level not in CLASSES or not policy.get("enabled") or provider not in (None, "", "auto"):
        raise ModelRoutingError("Invalid or disabled class context request")
    requirements = {"tools": True, "streaming": True, "min_context_tokens": 1}
    model_config = config.get("model", {})
    if isinstance(model_config, dict) and model_config.get("context_length"):
        requirements["min_context_tokens"] = int(model_config["context_length"])
    routes = eligible_routes(policy, level, requirements)
    entry = getattr(getattr(runner, "session_store", None), "_entries", {}).get(session_key)
    metadata = RoutingState(get_hermes_home() / "model-routing.sqlite3").peek(entry.session_id, level) if entry else None
    if metadata:
        routes = [r for r in routes if (r["provider"], r["model"], r["billing"]) == (
            metadata["selected_provider"], metadata["selected_model"], metadata["billing_group"])]
    else:
        routes = [r for r in routes if r["billing"] == "subscription"] or routes
    if not routes:
        raise ModelRoutingError("No eligible class context window; refusing generic catalog fallback")
    return min(r["capabilities"]["context_tokens"] for r in routes)


def channel_override(runner, source):
    from gateway.run import _get_channel_override
    cfg = getattr(runner, "config", None)
    if not cfg or source is None:
        return None
    return _get_channel_override(
        cfg, source.platform, str(source.chat_id) if source.chat_id else "",
        thread_id=str(source.thread_id) if getattr(source, "thread_id", None) else None,
        parent_id=str(source.parent_chat_id) if getattr(source, "parent_chat_id", None) else None)


def apply_channel_runtime(runner, source, model, runtime):
    from gateway.run import _resolve_runtime_agent_kwargs_for_provider
    channel = channel_override(runner, source)
    if channel:
        model = channel.model or model
        if channel.provider:
            runtime = _resolve_runtime_agent_kwargs_for_provider(channel.provider, target_model=model or None)
            bundled_model = runtime.pop("model", None)
            if bundled_model and not channel.model:
                model = bundled_model
    return model, runtime
