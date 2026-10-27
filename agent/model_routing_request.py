"""Validate physical class attempts without allocating or changing cached context."""
import json

from agent.model_routing import ModelRoutingError, _capable, validate_policy


_IDENTITY_FIELDS = frozenset({
    "model", "messages", "input", "instructions", "system", "tools", "stream",
    "max_tokens", "max_completion_tokens", "max_output_tokens", "response_format", "text",
})


def _has_image(value):
    if isinstance(value, dict):
        return value.get("type") in {"image", "image_url", "input_image"} or any(
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
    reservation = next((request[key] for key in (
        "max_tokens", "max_completion_tokens", "max_output_tokens") if request.get(key)), None)
    context += reservation or getattr(agent, "max_tokens", None) or 4096
    requirements["min_context_tokens"] = max(floor, context)
    requirements.update(tools=bool(request.get("tools")), vision=_has_image(payload),
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
    allowed = [route for route in routes
               if route["provider"] == agent.provider and route["model"] == agent.model
               and route.get("eligible") and route.get("funding_verified")
               and route["billing"] == metadata["billing_group"]
               and (route["billing"] == "subscription" or policy.get("allow_metered", False))
               and _capable(route, requirements)]
    if not policy.get("enabled") or not allowed:
        detail = ""
        same = [route for route in routes
                if route["provider"] == agent.provider and route["model"] == agent.model]
        failing = _failing_requirements(same[0], requirements) if same else []
        if failing:
            detail = "; failing capability: " + ", ".join(failing)
        raise ModelRoutingError(
            "Selected class route is ineligible for this request; no bypass permitted" + detail)
    agent._model_routing_requirements = requirements
