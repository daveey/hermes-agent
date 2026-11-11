"""Non-allocating gateway context windows, separated from the runner facade."""
from contextlib import suppress
import logging

logger = logging.getLogger(__name__)


def resolve_gateway_model_context(model=None, route=None):
    from gateway.run import (
        _resolve_gateway_model, _GatewayModelContext, _load_gateway_config,
        _resolve_runtime_agent_kwargs, _best_effort,
    )
    from gateway.run_class_routing import class_context_length
    from agent.context_pin import is_fallback_context_length
    from agent.model_metadata import get_model_context_length

    resolved_model = model or _resolve_gateway_model()
    class_window = class_context_length(resolved_model, provider=(route or {}).get("provider", "auto"))
    if class_window is not None:
        return _GatewayModelContext(resolved_model, "auto", "", class_window, "class-registry")
    config_context_length = provider = base_url = api_key = custom_providers = None
    configured_model = configured_provider = configured_base_url = None

    def read_config():
        nonlocal config_context_length, provider, base_url, custom_providers
        nonlocal configured_model, configured_provider, configured_base_url
        data = _load_gateway_config()
        if not data:
            return
        model_cfg = data.get("model", {})
        if isinstance(model_cfg, dict):
            configured_model = model_cfg.get("default") or model_cfg.get("model")
            raw_ctx = model_cfg.get("context_length")
            if raw_ctx is not None:
                with suppress(TypeError, ValueError):
                    config_context_length = int(raw_ctx)
            configured_provider = provider = model_cfg.get("provider") or None
            configured_base_url = base_url = model_cfg.get("base_url") or None
        try:
            from hermes_cli.config import get_compatible_custom_providers
            custom_providers = get_compatible_custom_providers(data)
        except Exception:
            logger.debug("Gateway context configuration probe failed", exc_info=True)
            custom_providers = data.get("custom_providers")

    def read_runtime():
        nonlocal provider, base_url, api_key
        if route and (route.get("base_url") or route.get("_runtime_resolved")):
            # A session route with its own endpoint (a /model switch), or provider-specific runtime
            # already resolved by the caller, replaces the default runtime read. A persisted route
            # without either still resolves default credentials so a custom endpoint and its pin survive.
            provider = route.get("provider") or provider
            base_url = route.get("base_url") or ""
            api_key = route.get("api_key")
            return
        runtime = _resolve_runtime_agent_kwargs()
        provider = runtime.get("provider") or provider
        base_url = runtime.get("base_url") or base_url
        api_key = runtime.get("api_key")

    def pin_still_applies():
        from hermes_cli.route_identity import should_clear_context_pin
        return not should_clear_context_pin(
            configured_model, resolved_model, configured_base_url, base_url, configured_provider, provider)

    def custom_ctx():
        from hermes_cli.config import get_custom_provider_context_length
        return get_custom_provider_context_length(
            model=resolved_model, base_url=base_url, custom_providers=custom_providers)

    _best_effort(read_config)
    _best_effort(read_runtime)
    if config_context_length is not None and not _best_effort(pin_still_applies):
        config_context_length = None
    if config_context_length is None and custom_providers and base_url:
        config_context_length = _best_effort(custom_ctx) or None
    context_length = get_model_context_length(
        resolved_model, base_url=base_url or "", api_key=api_key or "",
        config_context_length=config_context_length, provider=provider or "",
        custom_providers=custom_providers)
    fell_through = is_fallback_context_length(
        context_length, model=str(resolved_model), provider=provider or "", base_url=base_url or "")
    context_source = ("config" if config_context_length is not None
                      else "default" if fell_through else "detected")
    return _GatewayModelContext(
        model=resolved_model, provider=provider or "", base_url=base_url or "",
        context_length=context_length, context_source=context_source)


async def inbound_model_context_length(runner, source, session_key):
    from gateway.run import _load_gateway_config
    from gateway.run_class_routing import class_context_length
    from agent.model_metadata import get_model_context_length_async

    config_context = None
    config = None
    model_config = {}
    custom_providers = []
    with suppress(Exception):
        config = _load_gateway_config()
        model_config = config.get("model", {})
        if isinstance(model_config, dict):
            raw = model_config.get("context_length")
            if raw is not None:
                config_context = int(raw)
        try:
            from hermes_cli.config import get_compatible_custom_providers
            custom_providers = get_compatible_custom_providers(config)
        except Exception:
            logger.debug("Gateway context configuration probe failed", exc_info=True)
            custom_providers = config.get("custom_providers") or []
    model, runtime = runner._resolve_session_agent_runtime(
        source=source, session_key=session_key, user_config=config)
    class_window = class_context_length(model, config=config, runner=runner,
                                        session_key=session_key, provider=runtime.get("provider"))
    if class_window is not None:
        return class_window
    base_url = runtime.get("base_url") or ""
    configured_model = model_config.get("default") or model_config.get("model") if isinstance(model_config, dict) else model_config
    if model != configured_model:
        config_context = None
    if config_context is not None:
        try:
            from hermes_cli.route_identity import should_clear_context_pin_async
            if await should_clear_context_pin_async(
                None, None, model_config.get("base_url"), base_url,
                model_config.get("provider"), runtime.get("provider")):
                config_context = None
        except Exception:
            logger.debug("Gateway context configuration probe failed", exc_info=True)
            config_context = None
    if custom_providers and base_url:
        with suppress(Exception):
            from hermes_cli.config import get_custom_provider_context_length
            config_context = get_custom_provider_context_length(
                model=model, base_url=base_url, custom_providers=custom_providers) or config_context
    return await get_model_context_length_async(
        model, base_url=base_url, api_key=runtime.get("api_key") or "",
        config_context_length=config_context, provider=runtime.get("provider") or "",
        custom_providers=custom_providers)
