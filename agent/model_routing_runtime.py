"""Class routing's credential boundary: no generic provider/billing fallback."""
from __future__ import annotations

from agent.model_routing import ModelRoutingError, resolve_model_route


def resolve_class_runtime(model, provider, *, config=None, session_id=None,
                          parent_session_id=None, requirements=None, excluded=()):
    from agent.credential_pool import load_pool
    from hermes_cli import runtime_provider as runtime

    if config is None:
        from hermes_cli.config_effective import load_user_config_effective
        config = load_user_config_effective()
    pools = {}

    def eligible(route):
        from hermes_cli.config import is_provider_enabled
        block = config.get("providers", {}).get(route["provider"], {})
        if not is_provider_enabled(block):
            return False
        key = route["provider"], route["billing"]
        if key not in pools:
            pools[key] = load_pool(route["provider"], allowed_auth_types=
                                  {"oauth"} if route["billing"] == "subscription" else None)
        return pools[key].has_available(model=route["model"])

    failed = list(excluded)
    while True:
        chosen = resolve_model_route(model, provider, config=config, session_id=session_id,
                                     parent_session_id=parent_session_id, requirements=requirements,
                                     excluded=failed, eligible=eligible)
        metadata = chosen["metadata"]
        selected = chosen["provider"]
        runtime._raise_if_provider_disabled(selected)
        pool = pools[selected, metadata["billing_group"]]
        entry = pool.select(model=chosen["model"])
        if entry is None:
            # Refresh/quota can change after the availability probe. Never enter
            # the legacy ladder (it can consume an environment API key).
            failed.append((selected, chosen["model"]))
            continue
        cfg = {"provider": selected, "default": chosen["model"]}
        resolved = runtime._resolve_runtime_from_pool_entry(
            provider=selected, entry=entry, model_cfg=cfg, pool=pool,
            requested_provider=selected, target_model=chosen["model"])
        verify_class_runtime(resolved, metadata)
        resolved.update(model=chosen["model"], model_routing=metadata)
        return resolved


def verify_class_runtime(runtime, metadata):
    """Check the actual credential/transport, not only manifest labels."""
    from hermes_cli.auth import DEFAULT_CODEX_BASE_URL, DEFAULT_XAI_OAUTH_BASE_URL
    from utils import base_url_hostname

    if runtime.get("provider") != metadata["selected_provider"] or not runtime.get("api_key"):
        raise ModelRoutingError("Class route resolved to a different provider or an empty credential")
    if metadata["billing_group"] != "subscription":
        return
    pool = runtime.get("credential_pool")
    entry = pool.current() if pool else None
    if entry is None or entry.auth_type != "oauth":
        raise ModelRoutingError("Subscription routing requires an OAuth credential, never a paid API key")
    expected = {
        "anthropic": ("api.anthropic.com", "anthropic_messages"),
        "openai-codex": (base_url_hostname(DEFAULT_CODEX_BASE_URL), "codex_responses"),
        "xai-oauth": (base_url_hostname(DEFAULT_XAI_OAUTH_BASE_URL), "codex_responses"),
    }
    host, mode = expected[metadata["selected_provider"]]
    if base_url_hostname(runtime.get("base_url", "")) != host or runtime.get("api_mode") != mode:
        raise ModelRoutingError("Subscription route requires its native provider endpoint and protocol")
    if metadata["selected_provider"] == "anthropic" and not runtime["api_key"].startswith("sk-ant-oat"):
        raise ModelRoutingError("Anthropic subscription route requires its native OAuth token")
