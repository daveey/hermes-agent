"""Opt-in class-preserving, subscription-first routing before credential resolution.

Concrete model pins never enter the allocator. Eligibility/funding attestations are
operator-provided, host-specific facts, not inferred from the existence of a key.
Runtime credential validation is a separate gate, before any request is sent.
"""
from __future__ import annotations

from agent.model_routing_state import RoutingState

CLASSES = frozenset({"deep", "standard", "fast"})
BILLING = frozenset({"subscription", "credit-funded", "metered"})
SUBSCRIPTION_PROVIDERS = frozenset({"anthropic", "openai-codex", "xai-oauth"})


class ModelRoutingError(ValueError):
    """Fail closed; deliberately not AuthError (legacy paid fallbacks catch it)."""


class RouteContextOverflow(ModelRoutingError):
    """An otherwise eligible route only needs a smaller request; never authorize failover."""


def is_class_selector(model):
    return isinstance(model, str) and model.startswith("class:")


def exact_class_selector(model):
    """Policy membership only; callers own normalization, never this predicate."""
    return isinstance(model, str) and model in ('class:fast', 'class:standard', 'class:deep')


def _validate_route(route):
    if not isinstance(route, dict):
        raise ModelRoutingError("Route must be an exact provider/model/billing/weight/capabilities mapping")
    for key in ("provider", "model"):
        if not isinstance(route.get(key), str) or not route[key] or route[key].strip() != route[key]:
            raise ModelRoutingError(f"Route {key} must be a nonempty exact identifier")
    if route["provider"] == "auto" or is_class_selector(route["model"]):
        raise ModelRoutingError("Registry routes must be concrete, never auto or class selectors")
    if route.get("billing") not in BILLING:
        raise ModelRoutingError("Unknown route billing classification")
    if route["billing"] == "subscription" and route["provider"] not in SUBSCRIPTION_PROVIDERS:
        raise ModelRoutingError("Unverified subscription provider: only native subscription transports are supported")
    weight = route.get("weight")
    if type(weight) is not int or not 1 <= weight <= 100:
        raise ModelRoutingError("Route weight must be an integer from 1 to 100")
    if not isinstance(route.get("capabilities"), dict):
        raise ModelRoutingError("Route capabilities must be a mapping")
    for key in ("eligible", "funding_verified"):
        if key in route and type(route[key]) is not bool:
            raise ModelRoutingError(f"Route {key} must be boolean")
    caps = route["capabilities"]
    for key, value in caps.items():
        if key in ("context_tokens", "max_latency_ms"):
            if type(value) is not int or value <= 0:
                raise ModelRoutingError(f"Capability {key} must be a positive integer")
        elif type(value) is not bool:
            raise ModelRoutingError(f"Capability {key} must be boolean")


def validate_policy(policy):
    if not isinstance(policy, dict):
        raise ModelRoutingError("model_routing must be a mapping")
    if type(policy.get("enabled", False)) is not bool or type(policy.get("allow_metered", False)) is not bool:
        raise ModelRoutingError("model_routing enabled/allow_metered must be boolean")
    if type(policy.get("enforce_subscription_only", False)) is not bool:
        raise ModelRoutingError("model_routing enforce_subscription_only must be boolean")
    if type(policy.get("policy_version", 1)) is not int or policy.get("policy_version", 1) != 1:
        raise ModelRoutingError("Unsupported model_routing policy_version")
    routes = policy.get("routes", {})
    if not isinstance(routes, dict) or set(routes) - CLASSES:
        raise ModelRoutingError("Registry must contain only deep/standard/fast route lists")
    for level, entries in routes.items():
        if not isinstance(entries, list):
            raise ModelRoutingError(f"{level} routes must be a list")
        identities, weights = set(), {}
        for route in entries:
            _validate_route(route)
            identity = route["provider"], route["model"]
            if identity in identities:
                raise ModelRoutingError("Duplicate route identity")
            identities.add(identity)
            provider = route["provider"]
            if provider in weights and weights[provider] != route["weight"]:
                raise ModelRoutingError("All routes for a provider must use the same provider weight")
            weights[provider] = route["weight"]


def _capable(route, requirements):
    caps = route["capabilities"]
    for key, value in requirements.items():
        if key == "min_context_tokens":
            if caps.get("context_tokens", 0) < value:
                return False
        elif key == "max_latency_ms":
            if caps.get(key, float("inf")) > value:
                return False
        elif value and caps.get(key) is not True:
            return False
    return True


def eligible_routes(policy, level, requirements=None, excluded=(), eligible=None):
    """Shared billing/capability filter for allocation and non-allocating probes."""
    return [r for r in policy.get("routes", {}).get(level, [])
            if r.get("eligible") is True and r.get("funding_verified") is True
            and (r["provider"], r["model"]) not in excluded
            and _capable(r, requirements or {})
            and (r["billing"] == "subscription" or
                 (policy.get("allow_metered", False) and not policy.get("enforce_subscription_only", False)))
            and (eligible is None or eligible(r))]


def resolve_model_route(model, provider="auto", *, config=None, session_id=None,
                        parent_session_id=None, requirements=None, excluded=(), state_path=None,
                        eligible=None, prepare=None, exact_pin=False, replace: bool | str = False,
                        pin_source=None, walled_from=None):
    """Return concrete model/provider and durable metadata, or unchanged exact pin.

    ``excluded`` is a sequence of failed (provider, model) pairs for a new attempt,
    never a reason to rebalance a healthy running turn. No paid legacy fallback is
    permissible after ModelRoutingError. Caller must validate actual credentials.
    """
    if not is_class_selector(model):
        from agent.model_routing_managed import managed_config, pin_policy
        managed = managed_config(config)
        if managed is not None:
            pin_policy(model, provider, managed)
        return {"model": model, "provider": provider, "metadata": None}
    level = model.removeprefix("class:")
    if level not in CLASSES:
        raise ModelRoutingError(f"Unknown model class: {level}")
    if provider not in (None, "", "auto"):
        raise ModelRoutingError("Class selectors require provider auto; use a concrete model to pin a provider")
    if config is None:
        from hermes_cli.config_effective import load_user_config_effective
        config = load_user_config_effective()
    policy = config.get("model_routing", {})
    validate_policy(policy)
    if not policy.get("enabled", False):
        raise ModelRoutingError("Model class routing is disabled")
    if not isinstance(session_id, str) or not session_id:
        raise ModelRoutingError("Class routing requires a durable session/job identity")
    excluded = set(tuple(pair) for pair in excluded)
    routes = eligible_routes(policy, level, requirements, excluded, eligible)
    # Keep the full policy-approved set for sticky lookup. New allocations prefer
    # subscriptions; returning to a healthy paid session must not silently switch it.
    if state_path is None:
        from hermes_constants import get_hermes_home
        state_path = get_hermes_home() / "model-routing.sqlite3"
    metadata = RoutingState(state_path).allocate(session_id, parent_session_id, level,
                                                routes, policy.get("policy_version", 1), excluded, prepare,
                                                exact_pin=exact_pin, replace=replace,
                                                pin_source=pin_source, walled_from=walled_from)
    return {"model": metadata["selected_model"], "provider": metadata["selected_provider"], "metadata": metadata}
