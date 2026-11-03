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


def _allocated_route(route, session_id, state_path=None):
    """The allocation store's current route for this chat's lineage and class, else the cached ``route``.

    ``model_config.model_routing`` is a copy written after a turn; the allocation store
    (``model-routing.sqlite3``) is what the allocator checks. They diverge when an explicit switch is honoured
    by a build whose turn never rewrote the copy: MacBook chat 20261026_205117_f67f0d (11-02) had the store on
    gpt-6-astra/openai-codex (``explicit-switch``) and the copy still on claude-opus-5-5/anthropic, so resume
    restored the copy as an exact pin and every build (close-out wake, notice wake, replay) was refused with
    "Exact pinned route cannot fail over", leaving the chat on an unanswered notice. [daveey]
    """
    level = route.get("requested_class")
    if not session_id or level not in CLASSES:
        return route
    try:
        from agent.model_routing_state import RoutingState
        if state_path is None:
            from hermes_constants import get_hermes_home
            state_path = get_hermes_home() / "model-routing.sqlite3"
        current = RoutingState(state_path).peek(session_id, level)
    except Exception:
        logging.getLogger(__name__).debug("resume: allocation store unreadable for %s", session_id, exc_info=True)
        return route
    if isinstance(current, dict) and isinstance(current.get("selected_model"), str) and current["selected_model"]:
        return current
    return route


def restore_route(model, config, policy_config=None, *, session_id=None, state_path=None):
    """Return (model selector, provider), leaving policy validation to runtime.

    Class allocations may fail over; exact pins must retain both tuple members,
    including when the allocation database has been lost. Session metadata is
    intent, not authorization to bypass the current managed policy.

    A row whose ``model`` disagrees with its recorded route resumes on the recorded route: the recorded
    allocation is the only state that carries an explicit switch (``/model`` replaces it, reason
    ``explicit-switch``). Replaying the row's tuple as a NEW exact pin collides with the recorded pin and is
    refused as "Exact pinned route cannot fail over" on every build, so the chat can never reopen. A recorded
    pin that policy no longer approves is not restored; the allocator keeps refusing it. [daveey]

    With ``session_id`` the record is the allocation store's row for that lineage and class when it has one
    (``_allocated_route``): the copy in ``model_config`` can lag an honoured switch. [daveey]
    """
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except (ValueError, TypeError):
            return None
    route = config.get("model_routing") if isinstance(config, dict) else None
    if not isinstance(route, dict):
        return None
    route = _allocated_route(route, session_id, state_path)
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
