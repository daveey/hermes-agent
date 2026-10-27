"""Provider catalog routes used to validate initialization context pins."""

from contextlib import suppress


def provider_default_routes(provider: str) -> set[str]:
    """Return known exact default routes for a canonical provider id."""
    # Keep the initialization module's normalization seam patchable at call time.
    from agent.agent_init import normalize_route_base_url

    routes: set[str] = set()

    def add(value):
        route = normalize_route_base_url(value)
        if route:
            routes.add(route)

    with suppress(Exception):
        from hermes_cli.providers import HERMES_OVERLAYS, get_provider
        overlay = HERMES_OVERLAYS.get(provider)
        provider_def = get_provider(provider, allow_network=False)
        add(getattr(overlay, "base_url_override", ""))
        add(getattr(provider_def, "base_url", ""))

    with suppress(Exception):
        from providers import get_provider_profile
        add(getattr(get_provider_profile(provider), "base_url", ""))

    with suppress(Exception):
        from hermes_cli.auth import PROVIDER_REGISTRY
        from hermes_cli.models import normalize_provider as normalize_model_provider
        from hermes_cli.providers import normalize_provider as normalize_registry_provider
        for provider_id, config in PROVIDER_REGISTRY.items():
            if normalize_registry_provider(normalize_model_provider(provider_id)) == provider:
                add(getattr(config, "inference_base_url", ""))

    if provider == "gemini":
        routes.update(f"{route.rstrip('/')}/openai" for route in list(routes))
    return routes
