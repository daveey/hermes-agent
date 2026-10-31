"""Restore persisted routing intent without granting new routing permissions."""
import json
import logging

from agent.model_routing import CLASSES, ModelRoutingError, is_class_selector


def stale_managed_pin(model, provider, config=None):
    """Whether the managed subscription policy would refuse this stored concrete tuple as a pin.

    A session row's model is history, not a new request. Replaying a retired, unregistered or paid tuple
    fails closed at agent build on every attempt, so the chat could never be reopened; resume adapters
    drop such a tuple and the session runs on the configured default. Off-policy, class selectors and
    approved tuples are never stale, and an explicit request for the same model still refuses.
    """
    if not isinstance(model, str) or not model.strip() or is_class_selector(model.strip()):
        return False
    from agent.model_routing_managed import managed_config, pin_policy
    try:
        config = managed_config(config)
    except ModelRoutingError:
        return False  # an invalid policy is the runtime's error to report, not a reason to drop history
    if config is None:
        return False
    try:
        pin_policy(model.strip(), provider or None, config)
    except ModelRoutingError:
        logging.getLogger(__name__).info(
            "resume: stored model %r (provider %r) is not an approved subscription route; using the default",
            model, provider)
        return True
    return False


def stale_session_pin(row):
    """``stale_managed_pin`` for a stored session row, read through the canonical runtime reader."""
    if not isinstance(row, dict):
        return False
    from hermes_state import SessionDB
    return stale_managed_pin(row.get("model"), SessionDB.session_gateway_runtime(row).get("provider"))


def restore_route(model, config, policy_config=None):
    """Return (model selector, provider), leaving policy validation to runtime.

    Class allocations may fail over; exact pins must retain both tuple members,
    including when the allocation database has been lost. Session metadata is
    intent, not authorization to bypass the current managed policy.

    A row whose ``model`` disagrees with its recorded route resumes on the recorded route: the recorded
    allocation is the only state that carries an explicit switch (``/model`` replaces it, reason
    ``explicit-switch``). Replaying the row's tuple as a NEW exact pin collides with the recorded pin and is
    refused as "Exact pinned route cannot fail over" on every build, so the chat can never reopen. A recorded
    pin that policy no longer approves is not restored; the allocator keeps refusing it. [daveey]
    """
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except (ValueError, TypeError):
            return None
    route = config.get("model_routing") if isinstance(config, dict) else None
    if not isinstance(route, dict):
        return None
    if route.get("selected_model") != model:
        model = route.get("selected_model")  # row and record disagree: the record wins
        if not isinstance(model, str) or not model:
            return None
    if route.get("exact_pin"):
        provider = route.get("selected_provider")
        if not isinstance(model, str) or not model or not isinstance(provider, str) or not provider or provider == "auto":
            raise ModelRoutingError("Persisted exact pin lacks a concrete model/provider tuple")
        return None if stale_managed_pin(model, provider, policy_config) else (model, provider)
    level = route.get("requested_class")
    if level in CLASSES:
        return "class:" + level, "auto"
    return None
