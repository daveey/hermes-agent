"""Restore persisted routing intent without granting new routing permissions."""
import json

from agent.model_routing import CLASSES, ModelRoutingError


def restore_route(model, config):
    """Return (model selector, provider), leaving policy validation to runtime.

    Class allocations may fail over; exact pins must retain both tuple members,
    including when the allocation database has been lost. Session metadata is
    intent, not authorization to bypass the current managed policy.
    """
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except (ValueError, TypeError):
            return None
    route = config.get("model_routing") if isinstance(config, dict) else None
    if not isinstance(route, dict) or route.get("selected_model") != model:
        return None
    if route.get("exact_pin"):
        provider = route.get("selected_provider")
        if not isinstance(model, str) or not model or not isinstance(provider, str) or not provider or provider == "auto":
            raise ModelRoutingError("Persisted exact pin lacks a concrete model/provider tuple")
        return model, provider
    level = route.get("requested_class")
    if level in CLASSES:
        return "class:" + level, "auto"
    return None
