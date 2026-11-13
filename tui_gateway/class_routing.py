"""Class selectors stay intact until AIAgent owns a durable session identity."""
from agent.model_routing import is_class_selector


def deferred_runtime(model, provider, config):
    if not is_class_selector(model):
        return None
    model_config = config.get("model") or {}
    configured = model_config.get("provider") if isinstance(model_config, dict) else None
    # AIAgent validates the selector and provider and fences credentials atomically.
    requested = provider or configured or "auto"
    return {"provider": requested, "requested_provider": requested}


def walled_pin_deferral(wall, model, provider, session_id):
    """A runtime that leaves the route to the agent build, or None when the quota *wall* must stand.

    The agent build owns the session's durable route and moves a router-picked pin off a far wall within its
    class (``resolve_managed_runtime``); David's own pins and short walls still refuse
    (``model_routing_walls.gateway_deferral`` decides, from the session's standing route). The one chat line
    naming the reroute rides ``_fallback_notice``, which ``_make_agent`` already shows once."""
    if not session_id:
        return None
    from agent.model_routing_runtime import routing_session
    from agent.model_routing_walls import gateway_deferral
    with routing_session(session_id):
        deferred = gateway_deferral(wall, model, provider, None)
    if deferred is None:
        return None
    notice = deferred.pop("walled_notice", None)
    deferred.pop("class_deferred", None)
    if notice:
        deferred["_fallback_notice"] = notice
    return deferred


def child_class_kwargs(agent):
    """Independent side jobs inherit the workload class, not a paid-capable concrete pin."""
    route = getattr(agent, "model_routing", None)
    if not isinstance(route, dict):
        return {}
    return {"model": f"class:{route['requested_class']}", "provider": "auto", "requested_provider": "auto",
            "base_url": None, "api_key": None, "api_mode": None, "credential_pool": None,
            "request_overrides": None, "fallback_model": [], "acp_command": None, "acp_args": None}


def probe_class_runtime(model, provider, config):
    """Check real native credentials without consuming a durable allocation slot."""
    from agent.model_routing_runtime import resolve_class_runtime
    requirements: dict[str, bool | int] = {"tools": True, "streaming": True}
    model_config = config.get("model") or {}
    if isinstance(model_config, dict) and model_config.get("context_length"):
        requirements["min_context_tokens"] = int(model_config["context_length"])
    runtime = resolve_class_runtime(model, provider, config=config, session_id="readiness",
                                    requirements=requirements, state_path=":memory:")
    return runtime["model"], runtime


def restore_class_overrides(model, config, overrides, session_id=None):
    from agent.model_routing_restore import restore_route, stale_managed_pin
    restored = restore_route(model, config, session_id=session_id)
    if restored is not None:
        model, provider = restored
        overrides["model_override"] = {"model": model, "provider": provider}
        overrides["provider_override"] = provider
    target = overrides.get("model_override") or {}
    if stale_managed_pin(target.get("model"), overrides.get("provider_override") or target.get("provider")):
        # History, not a request: resume on the profile default instead of failing closed at agent build.
        overrides.pop("model_override", None)
        overrides.pop("provider_override", None)
    return overrides


def runtime_model_config(agent, existing, runtime_view, logger):
    """Persist concrete display identity and class policy, clearing stale explicit-switch metadata."""
    config = dict(existing or {})
    from agent.agent_runtime_once import standing_runtime
    agent = standing_runtime(agent)
    route = getattr(agent, "model_routing", None)
    agent = runtime_view(agent)
    attr = lambda k: str(getattr(agent, k, "") or "").strip()
    model, base_url = attr("model"), attr("base_url")
    from hermes_cli.runtime_provider_custom import session_provider_identity
    provider = session_provider_identity(attr("provider"), attr("requested_provider"), base_url, model)
    reasoning_config = getattr(agent, "reasoning_config", None)
    live = {
        "model": model, "provider": provider, "base_url": base_url, "api_mode": attr("api_mode"),
        "reasoning_config": reasoning_config if isinstance(reasoning_config, dict) else None,
        "service_tier": getattr(agent, "service_tier", None),
        "model_routing": route,
    }
    for key, value in live.items():
        if value or isinstance(value, dict):
            config[key] = value
        else:
            config.pop(key, None)
    return config
