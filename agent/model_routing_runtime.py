"""Class routing's credential boundary: no generic provider/billing fallback."""
from __future__ import annotations

from agent.model_routing import ModelRoutingError, resolve_model_route


def resolve_class_runtime(model, provider, *, config=None, session_id=None,
                          parent_session_id=None, requirements=None, excluded=(), state_path=None,
                          prepare_runtime=None):
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
    resolved = {}

    class UnavailableCredential(Exception):
        pass

    def prepare(metadata):
        selected, selected_model = metadata["selected_provider"], metadata["selected_model"]
        runtime._raise_if_provider_disabled(selected)
        pool = pools[selected, metadata["billing_group"]]
        entry = pool.select(model=selected_model)
        if entry is None:
            # Roll back the unready selection before trying another same-class
            # candidate. Never enter the legacy environment-key ladder.
            failed.append((selected, selected_model))
            raise UnavailableCredential
        cfg = {"provider": selected, "default": selected_model}
        resolved.update(runtime._resolve_runtime_from_pool_entry(
            provider=selected, entry=entry, model_cfg=cfg, pool=pool,
            requested_provider=selected, target_model=selected_model))
        verify_class_runtime(resolved, metadata)
        resolved.update(model=selected_model, model_routing=metadata)
        if prepare_runtime is not None:
            prepare_runtime(resolved)

    while True:
        try:
            resolve_model_route(model, provider, config=config, session_id=session_id,
                                parent_session_id=parent_session_id, requirements=requirements,
                                excluded=failed, eligible=eligible, state_path=state_path, prepare=prepare)
            return resolved
        except UnavailableCredential:
            continue


def refresh_class_anthropic_token(agent):
    """Refresh within the selected billing pool, never the legacy env-key ladder.

    This resolves credentials only: it neither allocates nor changes the sticky
    provider/model. Native pool resolution retains refresh-grant ownership.
    """
    from agent.credential_pool import load_pool
    from hermes_cli.runtime_provider import _resolve_runtime_from_pool_entry

    metadata = agent.model_routing
    pool = load_pool("anthropic", allowed_auth_types=
                     {"oauth"} if metadata["billing_group"] == "subscription" else None)
    entry = pool.select(model=agent.model)
    if entry is None:
        return None
    runtime = _resolve_runtime_from_pool_entry(
        provider="anthropic", entry=entry,
        model_cfg={"provider": "anthropic", "default": agent.model},
        pool=pool, requested_provider="anthropic", target_model=agent.model)
    verify_class_runtime(runtime, metadata)
    agent._credential_pool = pool
    return runtime["api_key"]


def verify_agent_class_credentials(agent):
    """Recheck the actual native credential immediately before client checkout."""
    metadata = getattr(agent, "model_routing", None)
    if metadata:
        verify_class_runtime({
            "provider": agent.provider, "api_mode": agent.api_mode,
            "api_key": agent._anthropic_api_key,
            "base_url": agent._anthropic_base_url,
            "credential_pool": agent._credential_pool,
        }, metadata)


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
