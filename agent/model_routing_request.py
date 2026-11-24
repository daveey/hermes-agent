"""Validate physical class attempts without allocating or changing cached context."""
import json

from agent.model_routing import ModelRoutingError, _capable, eligible_routes, validate_policy


_IDENTITY_FIELDS = frozenset({
    "model", "messages", "input", "instructions", "system", "tools", "stream",
    "max_tokens", "max_completion_tokens", "max_output_tokens", "response_format", "text",
})


_IMAGE_PART_TYPES = frozenset({"image", "image_url", "input_image"})
# Payload keys that can carry user/tool image parts. ``tools`` holds JSON schemas, never images:
# a schema property NAMED "type" makes ``dict.get("type")`` a dict (unhashable) and an
# ``{"type": "image"}`` default/example would falsely demand vision.
_CONTENT_KEYS = ("messages", "input", "system", "instructions")


def _has_image(value):
    if isinstance(value, dict):
        kind = value.get("type")
        return (isinstance(kind, str) and kind in _IMAGE_PART_TYPES) or any(
            _has_image(item) for item in value.values())
    return isinstance(value, list) and any(_has_image(item) for item in value)


def _estimate_context_tokens(request, payload):
    """Token estimate of the current request, in the same units compression uses."""
    from agent.model_metadata import (
        estimate_messages_tokens_rough, estimate_request_tokens_rough, estimate_tokens_rough)
    total = estimate_request_tokens_rough(
        [m for m in request.get("messages") or [] if isinstance(m, dict)],
        tools=request.get("tools") or None)
    for key in ("input", "system", "instructions"):
        value = payload.get(key)
        if isinstance(value, list) and all(isinstance(m, dict) for m in value):
            total += estimate_messages_tokens_rough(value)
        elif value:
            total += estimate_tokens_rough(
                value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str))
    return total


def _failing_requirements(route, requirements):
    """Requirement names (and token counts, never content) that one route fails."""
    failing = []
    for key, value in requirements.items():
        if not _capable(route, {key: value}):
            if key == "min_context_tokens":
                failing.append(f"min_context_tokens(required={value}, "
                               f"attested={route['capabilities'].get('context_tokens', 0)})")
            else:
                failing.append(key)
    return failing


def output_reservation(agent, request):
    """One output runway calculation for route admission and proactive compaction."""
    reservation = next((request[key] for key in (
        "max_tokens", "max_completion_tokens", "max_output_tokens") if request.get(key)), None)
    if reservation is not None:
        return reservation
    reservation = getattr(agent, "_ephemeral_max_output_tokens", None)
    if reservation is None:
        reservation = getattr(agent, "max_tokens", None)
    if getattr(agent, "api_mode", None) == "anthropic_messages":
        from agent.anthropic_adapter import _resolve_anthropic_messages_max_tokens
        reservation = _resolve_anthropic_messages_max_tokens(reservation, agent.model)
        window = agent.context_compressor.context_length
        return max(window - 1, 1) if window and reservation > window else reservation
    return reservation or 4096


def route_preflight_pressure(agent, request, pressure):
    """Honor the selected transport's attestation even when usage/catalog estimates disagree.

    This does not authorize a request: final validation still checks the current
    policy, identity and every capability. Recompute the cap per request so a new
    output reservation or route never retains the previous route's budget.
    """
    import logging
    from hermes_cli.config_effective import load_user_config_effective

    agent._route_context_pressure = False
    metadata = getattr(agent, "model_routing", None)
    compressor = getattr(agent, "context_compressor", None)
    if compressor is None:
        return pressure
    compressor._route_context_ceiling = None
    if not metadata:
        return pressure
    policy = load_user_config_effective().get("model_routing", {})
    routes = policy.get("routes", {}).get(metadata["requested_class"], [])
    windows = [r["capabilities"].get("context_tokens", 0) for r in routes
               if (r["provider"], r["model"], r["billing"]) == (
                   agent.provider, agent.model, metadata["billing_group"])]
    if not windows or windows[0] <= 0:
        return pressure
    # No amount of history compression can create output runway in this case.
    cap = windows[0] * 98 // 100 - output_reservation(agent, request)
    if cap <= 0:
        return pressure
    previous = compressor.threshold_tokens
    compressor._route_context_ceiling = cap
    if cap < previous and not getattr(agent, "_route_context_cap_logged", False):
        logging.getLogger(__name__).info(
            "Route context cap: model=%s provider=%s attested=%s prompt_trigger=%s output_reservation=%s",
            agent.model, agent.provider, windows[0], cap, output_reservation(agent, request))
        agent._route_context_cap_logged = True
    estimate = _estimate_context_tokens(request, request)
    agent._route_context_pressure = estimate >= compressor.threshold_tokens
    return max(pressure, estimate)


def _requirements(agent, request, streaming):
    requirements = dict(agent._model_routing_requirements)
    # The construction-time minimum (e.g. compression's context floor) is remembered once;
    # the per-request bound is recomputed from the CURRENT request so it shrinks again
    # after compression instead of ratcheting forever.
    floor = getattr(agent, "_model_routing_context_floor", None)
    if floor is None:
        floor = agent._model_routing_context_floor = requirements.get("min_context_tokens", 0)
    payload = {key: request[key] for key in ("messages", "input", "system", "instructions", "tools")
               if key in request}
    # Same rough token estimator as compression/preflight (UTF-8 bytes overstate tokens ~3-4x
    # against token-denominated route caps). Includes tool schemas, system prompt and the
    # output reservation, not just user text.
    context = _estimate_context_tokens(request, payload)
    context += output_reservation(agent, request)
    requirements["min_context_tokens"] = max(floor, context)
    requirements.update(tools=bool(request.get("tools")),
                        vision=_has_image([payload[key] for key in _CONTENT_KEYS if key in payload]),
                        streaming=streaming, json=bool(request.get("response_format") or
                        (request.get("text") or {}).get("format")))
    return requirements


def validate_class_request(agent, request, *, streaming):
    """Final middleware boundary: reject an incompatible sticky route, never rebalance.

    Capability failures are explicit rather than discarding images or changing the
    model halfway through a tool round. Failure recovery retains these requirements.
    """
    from agent.model_routing_managed import verify_managed_agent
    verify_managed_agent(agent, request)
    metadata = getattr(agent, "model_routing", None)
    if not metadata:
        return
    if (request.get("model") != metadata["selected_model"] or
            agent.model != metadata["selected_model"] or
            agent.provider != metadata["selected_provider"]):
        raise ModelRoutingError("Class request must preserve the selected provider/model")
    extra = request.get("extra_body") or {}
    if _IDENTITY_FIELDS.intersection(extra):
        raise ModelRoutingError("Class extra_body cannot override request identity or capabilities")
    headers = request.get("extra_headers") or {}
    if {key.lower() for key in headers} & {"authorization", "x-api-key", "api-key", "host"}:
        raise ModelRoutingError("Class headers cannot override authentication or destination")
    from hermes_cli.config_effective import load_user_config_effective
    policy = load_user_config_effective().get("model_routing", {})
    validate_policy(policy)
    requirements = _requirements(agent, request, streaming)
    routes = policy.get("routes", {}).get(metadata["requested_class"], [])
    candidates = eligible_routes(policy, metadata["requested_class"])
    allowed = [route for route in candidates
               if route["provider"] == agent.provider and route["model"] == agent.model
               and route.get("eligible") and route.get("funding_verified")
               and route["billing"] == metadata["billing_group"]
               and _capable(route, requirements)]
    if not policy.get("enabled") or not allowed:
        detail = ""
        same = [route for route in routes
                if route["provider"] == agent.provider and route["model"] == agent.model]
        failing = _failing_requirements(same[0], requirements) if same else []
        if failing:
            detail = "; failing capability: " + ", ".join(failing)
        # Compress only a request-size failure. A withdrawn route, funding/billing
        # fence, mixed capability failure or impossible construction floor is not
        # made eligible by deleting history.
        from agent.model_routing import RouteContextOverflow
        context_free = {key: value for key, value in requirements.items() if key != "min_context_tokens"}
        recoverable = policy.get("enabled") and any(
            route.get("eligible") and route.get("funding_verified")
            and route["billing"] == metadata["billing_group"]
            and route in candidates
            and _capable(route, context_free)
            and agent._model_routing_context_floor <= route["capabilities"].get("context_tokens", 0)
            and output_reservation(agent, request) < route["capabilities"].get("context_tokens", 0)
            for route in same)
        error_type = RouteContextOverflow if recoverable else ModelRoutingError
        raise error_type(
            "Selected class route is ineligible for this request; no bypass permitted" + detail)
    agent._model_routing_requirements = requirements
