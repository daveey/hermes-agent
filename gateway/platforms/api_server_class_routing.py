"""Defer class credentials until the API agent has its durable session identity."""
from agent.model_routing import is_class_selector


def deferred_runtime(model, provider=None):
    if not is_class_selector(model):
        return None
    return {"provider": provider or "auto", "requested_provider": provider or "auto"}


def restore_session_class(adapter, session_id, session_model, confirmed_runtime_lock):
    """A concrete persisted selection is not an explicit pin when it carries class provenance."""
    if not session_id or confirmed_runtime_lock:
        return session_model
    db = adapter._ensure_session_db()
    row = db.get_session(session_id) if db is not None else None
    if not isinstance(row, dict):
        return session_model
    config = adapter._parse_session_model_config(row.get("model_config"))
    metadata = config.get("model_routing") or {}
    if (metadata.get("selected_model") == row.get("model")
            and metadata.get("requested_class") in {"deep", "standard", "fast"}
            and (not session_model or session_model == row.get("model"))):
        return "class:" + metadata["requested_class"]
    return session_model


def initial_runtime(adapter, *, default_model, requested_model,
                    route, session_model, confirmed_runtime_lock, session_key):
    """Avoid touching default credentials before a class request overrides them.

    The ordinary API precedence resolver remains the owner of route selection. This
    preflight only skips unnecessary default authentication, never allocates a route.
    """
    from gateway.run import _resolve_runtime_agent_kwargs
    override = None if confirmed_runtime_lock else adapter._session_model_override_for(session_key)
    candidate = (override or {}).get("model") or (session_model if not confirmed_runtime_lock else None)
    candidate = candidate or ((route.get("model") or default_model) if route is not None
                              else requested_model or default_model)
    if is_class_selector(candidate) or is_class_selector(default_model):
        from hermes_cli.runtime_provider import _get_model_config
        provider = (_get_model_config().get("provider") or "auto") if not (
            override or session_model or requested_model or route) else "auto"
        return {"provider": provider, "requested_provider": provider}
    return _resolve_runtime_agent_kwargs()


def prepare_runtime(adapter, session_id, session_model, confirmed_runtime_lock,
                    requested_model, route, gateway_session_key):
    from gateway.run import _resolve_gateway_model
    from gateway.platforms.api_server import _ProviderAuthResolutionError
    session_model = restore_session_class(adapter, session_id, session_model, confirmed_runtime_lock)
    model = _resolve_gateway_model()
    try:
        runtime = initial_runtime(
            adapter, default_model=model, requested_model=requested_model, route=route,
            session_model=session_model, confirmed_runtime_lock=confirmed_runtime_lock,
            session_key=gateway_session_key or session_id)
    except RuntimeError as exc:
        raise _ProviderAuthResolutionError(str(exc)) from exc
    model = runtime.pop("model", None) or model
    runtime.pop("_fallback_notice", None)
    return model, runtime, session_model
