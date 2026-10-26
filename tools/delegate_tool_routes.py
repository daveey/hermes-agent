"""Offline, conservative model/provider preflight for delegated batches."""
from typing import Any, Optional

from hermes_cli.model_normalize import (
    _VENDOR_PREFIXES,
    detect_vendor,
    model_conflicts_with_provider,
    normalize_model_for_provider,
)
from hermes_cli.models_validate import static_model_provider_conflict
from utils import base_url_hostname


# Only stock single-vendor endpoints are evidence of an impossible route. A
# provider label on a custom relay says nothing about the models it can serve.
_STOCK_ROUTE_HOSTS = {
    "openai-codex": "chatgpt.com",
    "anthropic": "api.anthropic.com",
    "gemini": "generativelanguage.googleapis.com",
    "openai": "api.openai.com",
    "xai": "api.x.ai",
}


def task_route_conflict(creds: dict[str, Any], parent_agent) -> Optional[str]:
    """Reject certain mismatches, never unknown models, endpoints or providers.

    Credential resolution leaves inherited fields as None; inspect the effective
    route without mutating the parent or filling those sentinels in the bundle.
    This checks compatibility, not availability, entitlement or rate limits.
    """
    model = creds.get("model")
    # A pure inherited route already runs on the parent; this preflight is for
    # newly selected models, not revalidation of a running parent's state.
    provider = creds.get("provider") or getattr(parent_agent, "provider", None)
    base_url = (creds.get("base_url") if creds.get("provider")
                else getattr(parent_agent, "base_url", None))
    if not isinstance(model, str) or not isinstance(provider, str):
        return None
    host = _STOCK_ROUTE_HOSTS.get(provider)
    if not host or not isinstance(base_url, str) or base_url_hostname(base_url) != host:
        return None
    # The static OAuth catalog gate rejects even arbitrary unknown slugs. That
    # is too strict for delegation: only a recognized vendor is evidence here.
    lookup = normalize_model_for_provider(model, provider)
    if detect_vendor(lookup) not in _VENDOR_PREFIXES.values():
        return None
    if not (model_conflicts_with_provider(lookup, provider)
            or static_model_provider_conflict(lookup, provider)):
        return None
    return (
        f"Model '{model}' is incompatible with provider '{provider}'. "
        "Supply provider together with model in this task, or choose a compatible model. "
        "No children were started; providers are never switched automatically."
    )
