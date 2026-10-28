"""[daveey] Refuse a bare vendor model id on a vendor/model-namespaced aggregator before any request.

OpenRouter and the Nous Portal only know ``vendor/model`` slugs: ``claude-opus-5-5`` there can only
come back as HTTP 400 "is not a valid model ID", an error that names neither the route nor why it was
taken. That happens when a route switches provider without switching the model (a Kanban pin with no
provider late-bound to an ``auto`` config, self-heal agent-1572c64e24). This check runs once the
client is built, so the error names the provider, base URL, model and how the route was chosen.

``agent.model_provider_guard: false`` in config.yaml turns it off.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Aggregators whose catalogues are vendor-namespaced end to end.
_NAMESPACED_AGGREGATORS = frozenset({"openrouter", "nous"})


class ModelProviderMismatchError(ValueError):
    """The resolved provider cannot serve this model id as written."""


def _guard_enabled() -> bool:
    try:
        from hermes_cli.config import load_config

        return (load_config().get("agent") or {}).get("model_provider_guard", True) is not False
    except Exception:
        logger.debug("model_provider_guard: config unreadable, guard stays on", exc_info=True)
        return True


def route_origin(agent: Any) -> str:
    """How the provider was chosen: ``fallback`` (init-time chain), ``auto`` (no provider named,
    or ``auto``), else ``pinned`` (named by the caller, config or a Kanban pin)."""
    if getattr(agent, "_fallback_activated", False):
        return "fallback"
    requested = str(getattr(agent, "requested_provider", "") or "").strip().lower()
    return "auto" if requested in {"", "auto"} else "pinned"


def mismatch_message(provider: str, base_url: str, model: str, route: str) -> str | None:
    """The refusal text when ``model`` is a bare id of a known vendor on a namespaced aggregator."""
    if provider not in _NAMESPACED_AGGREGATORS or not model or "/" in model:
        return None
    from hermes_cli.model_normalize import detect_vendor

    vendor = detect_vendor(model)
    if not vendor:
        return None  # unknown house ids pass: only a certainly-wrong id is refused
    # First line stays short: the CLI init-failure copy shows only the first ~120 chars of it.
    return (
        f"'{model}' on {provider} needs '{vendor}/{model}' (route: {route}); or use --provider {vendor}\n"
        f"Provider {provider} at {base_url or 'its default base URL'} only accepts vendor/model ids, "
        f"so this bare {vendor} id would fail with HTTP 400 \"is not a valid model ID\". "
        f"Turn this check off with `hermes config set agent.model_provider_guard false`."
    )


def check_model_matches_provider(agent: Any) -> None:
    """Raise :class:`ModelProviderMismatchError` (logged at WARNING) for a 400-bound model/provider pair."""
    provider = str(getattr(agent, "provider", "") or "").strip().lower()
    model = str(getattr(agent, "model", "") or "").strip()
    base_url = str(getattr(agent, "base_url", "") or "")
    client_kwargs = getattr(agent, "_client_kwargs", None)
    if isinstance(client_kwargs, dict) and client_kwargs.get("base_url"):
        base_url = str(client_kwargs["base_url"])
    message = mismatch_message(provider, base_url, model, route_origin(agent))
    if message is None or not _guard_enabled():
        return
    logger.warning("model/provider mismatch refused before any request: %s", message)
    raise ModelProviderMismatchError(message)
