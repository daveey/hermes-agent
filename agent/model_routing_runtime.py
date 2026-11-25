"""Class routing's credential boundary: no generic provider/billing fallback."""
from __future__ import annotations

import logging
import time

from agent.model_routing import ModelRoutingError, resolve_model_route
from contextlib import contextmanager
from contextvars import ContextVar

logger = logging.getLogger(__name__)

# [daveey] One re-check of an exact pin whose own credential was unselectable, after this pause.
PIN_RECHECK_DELAY_SECONDS = 1.0
_CREDENTIAL_MISSES = ("no available credential", "select() returned no credential")


class PinnedRouteUnavailable(ModelRoutingError):
    """[daveey] A session's exact pin lost its own route to a credential/provider check (not a quota wall).

    Before this, the allocator refused such a turn as "Exact pinned route cannot fail over", the same text a
    switch/record mismatch raises, so a transient credential miss (mini bb-aux 2026-11-05, self-heal
    agent-36e9ac084a) looked like a routing-store bug and nothing said why the pin was dropped."""

    def __init__(self, provider, model, reason, rechecked=False):
        self.provider, self.model, self.reason, self.rechecked = provider, model, reason, rechecked
        suffix = " (still missing after a pool reload)" if rechecked else ""
        super().__init__(f"Pinned route {provider}/{model} is unavailable: {reason}{suffix}")

_temporary_resolution = ContextVar("temporary_model_resolution", default=False)
_routing_session = ContextVar("model_routing_session", default=None)


@contextmanager
def routing_session(session_id):
    """[daveey] Name the session a pre-build lookup (``temporary_resolution``) resolves for.

    The pre-build lookup allocates nothing, but it must read that session's standing route: a
    router-picked pin walled for hours is rerouted by the agent build, so the lookup defers to it
    instead of refusing the turn (``gateway.session_worker_construct._route_runtime``)."""
    token = _routing_session.set(session_id or None)
    try:
        yield
    finally:
        _routing_session.reset(token)


def current_routing_session():
    return _routing_session.get()


def is_temporary_resolution():
    return _temporary_resolution.get()


@contextmanager
def temporary_resolution():
    """Validate/select normally, but never allocate a standing session or counter."""
    token = _temporary_resolution.set(True)
    try:
        yield
    finally:
        _temporary_resolution.reset(token)


def resolve_class_runtime(model, provider, *, config=None, session_id=None,
                          parent_session_id=None, requirements=None, excluded=(), state_path=None,
                          prepare_runtime=None, exact_pin=False, replace: bool | str = False,
                          pin_source=None, walled_from=None):
    from agent.credential_pool import load_pool
    from hermes_cli import runtime_provider as runtime

    if config is None:
        from hermes_cli.config_effective import load_user_config_effective
        config = load_user_config_effective()
    pools = {}
    unavailable = []
    churned = []  # [daveey] routes emptied by 401-benched rows: capacity unknown, never a quota reset
    available = []
    dropped = {}  # (provider, model) -> why that route left the candidate list
    if _temporary_resolution.get():
        state_path = ":memory:"

    def eligible(route):
        from agent.model_routing_exclusions import provider_allowed
        if not provider_allowed(route['provider']):
            return False
        from hermes_cli.config import is_provider_enabled
        block = config.get("providers", {}).get(route["provider"], {})
        if not is_provider_enabled(block):
            dropped[route["provider"], route["model"]] = "provider disabled"
            return False
        key = route["provider"], route["billing"]
        if key not in pools:
            pools[key] = load_pool(route["provider"], allowed_auth_types=
                                  {"oauth"} if route["billing"] == "subscription" else None)
        if pools[key].has_available(model=route["model"]):
            available.append(route)
            return True
        dropped[route["provider"], route["model"]] = "no available credential"
        if route["billing"] == "subscription":
            from agent.model_routing_unavailable import SubscriptionAuthUnavailable, auth_churned, quota_unavailable
            if error := quota_unavailable(pools[key], route["model"]):
                unavailable.append(error)
            elif auth_churned(pools[key], route["model"]):
                churned.append(SubscriptionAuthUnavailable(route["provider"], route["model"]))
        return False

    failed = list(excluded)
    resolved = {}

    class UnavailableCredential(Exception):
        pass

    def prepare(metadata):
        selected, selected_model = metadata["selected_provider"], metadata["selected_model"]
        from agent.model_routing_exclusions import provider_allowed
        if not provider_allowed(selected):
            raise ModelRoutingError('Safety exclusion refuses final provider allocation')
        runtime._raise_if_provider_disabled(selected)
        pool = pools[selected, metadata["billing_group"]]
        entry = pool.select(model=selected_model)
        if entry is None:
            # Roll back the unready selection before trying another same-class
            # candidate. Never enter the legacy environment-key ladder.
            failed.append((selected, selected_model))
            dropped[selected, selected_model] = "select() returned no credential"
            raise UnavailableCredential
        cfg = {"provider": selected, "default": selected_model}
        resolved.update(runtime._resolve_runtime_from_pool_entry(
            provider=selected, entry=entry, model_cfg=cfg, pool=pool,
            requested_provider=selected, target_model=selected_model))
        verify_class_runtime(resolved, metadata)
        resolved.update(model=selected_model, model_routing=metadata)
        if prepare_runtime is not None:
            prepare_runtime(resolved)

    rechecked = False
    while True:
        unavailable.clear()
        churned.clear()
        available.clear()
        try:
            resolve_model_route(model, provider, config=config, session_id=session_id,
                                parent_session_id=parent_session_id, requirements=requirements,
                                excluded=failed, eligible=eligible, state_path=state_path, prepare=prepare,
                                exact_pin=exact_pin, replace=replace, pin_source=pin_source,
                                walled_from=walled_from)
            return resolved
        except UnavailableCredential:
            continue
        except ModelRoutingError:
            # Only credential eligibility was lost: policy/capability refusals
            # never enter eligible(), and a prepared runtime's errors stay fatal.
            if churned and not available and not resolved and (rechecked or not exact_pin):
                raise churned[0] from None  # a 401-churned route may serve after re-read: no quota reset
            if unavailable and not churned and not available and not resolved:
                raise min(unavailable, key=lambda error: error.resets_at) from None
            if not exact_pin or available or resolved or not dropped:
                raise
            # [daveey] The pin's own route was dropped by a credential/provider check, not by policy or
            # the routing store. A credential miss is often momentary (a concurrent single-use-token
            # refresh fails silently and select() returns None): reload the pools from disk and re-check
            # once before refusing. A pin still never moves to another model.
            if not rechecked and any(why in _CREDENTIAL_MISSES for why in dropped.values()):
                rechecked = True
                logger.info("model routing: exact pin %s for session %s lost its credential (%s); "
                            "reloading the pool and re-checking once", _describe(dropped), session_id,
                            _describe(dropped, reasons=True))
                time.sleep(PIN_RECHECK_DELAY_SECONDS)
                pools.clear()
                dropped.clear()
                failed[:] = list(excluded)
                continue
            (provider_name, model_name), reason = sorted(dropped.items())[0]
            error = PinnedRouteUnavailable(provider_name, model_name, reason, rechecked=rechecked)
            logger.warning("model routing: session %s: %s", session_id, error)
            raise error from None


def _describe(dropped, reasons=False):
    if reasons:
        return ", ".join(f"{p}/{m}: {why}" for (p, m), why in sorted(dropped.items()))
    return ", ".join(f"{p}/{m}" for p, m in sorted(dropped))


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


def refresh_class_responses_token(agent):
    """Refresh only the active OAuth pool entry; never consult a singleton fallback."""
    from hermes_cli.runtime_provider import _resolve_runtime_from_pool_entry
    pool = agent._credential_pool
    entry = pool.try_refresh_current() if pool else None
    if entry is None:
        return False
    runtime = _resolve_runtime_from_pool_entry(
        provider=agent.provider, entry=entry, model_cfg={"default": agent.model, "provider": agent.provider},
        pool=pool, requested_provider=agent.provider, target_model=agent.model)
    verify_class_runtime(runtime, agent.model_routing)
    if runtime['api_key'] == agent.api_key:
        return False
    return agent._adopt_openai_credentials(runtime['api_key'], runtime['base_url'],
                                            reason='class_oauth_refresh')


def reselect_class_credential(agent):
    """[daveey] Re-adopt a subscription pool entry a rotation left unselected, before the next attempt.

    A 429 that benches the last row for this model leaves ``pool.current()`` empty and the turn waits
    (up to 600 s). A sibling row whose cooldown lapsed meanwhile is room, yet nothing reselected it, so
    the pre-send check refused the retry as "no selected pool entry" (hub 2026-11-14 05:50Z)."""
    metadata = getattr(agent, "model_routing", None)
    pool = getattr(agent, "_credential_pool", None)
    if not metadata or metadata.get("billing_group") != "subscription" or pool is None or pool.current():
        return
    entry = pool.select(model=agent.model)
    if entry is not None and agent._swap_credential(entry) is not False:
        logger.info("Class route re-selected pool entry %s for %s after a rotation left none",
                    entry.label or entry.id[:8], agent.model)


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
    if entry is None:
        from agent.model_routing_unavailable import SubscriptionAuthUnavailable, auth_churned, quota_unavailable
        model = metadata["selected_model"]
        if pool is not None and any(
            candidate.auth_type == "oauth" and candidate.runtime_api_key == runtime["api_key"]
            for candidate in pool.entries()
        ):
            # [daveey] Same ladder as resolution: a rotated-off pool's own state types the refusal.
            if unavailable := quota_unavailable(pool, model):
                raise unavailable
            if auth_churned(pool, model):
                raise SubscriptionAuthUnavailable(metadata["selected_provider"], model)
        raise ModelRoutingError("Subscription OAuth credential is unavailable; no selected pool entry")
    if entry.auth_type != "oauth":
        raise ModelRoutingError("Subscription routing requires an OAuth credential, never a paid API key")
    from hermes_cli.runtime_provider import _pool_entry_api_key
    if runtime["api_key"] != _pool_entry_api_key(entry):
        raise ModelRoutingError("Subscription credential differs from the selected OAuth token in the pool entry")
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
