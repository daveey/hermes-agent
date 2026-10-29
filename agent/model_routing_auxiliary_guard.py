"""Fail-closed managed checks on the native auxiliary SDK about to transmit."""
from agent.model_routing import ModelRoutingError


_AUTH_HEADERS = {"authorization", "x-api-key", "api-key", "host"}
_BODY_IDENTITY = {"model", "messages", "tools", "stream", "max_tokens", "max_completion_tokens"}


def verify_sdk_request(client, request, runtime):
    """Compare live endpoint/auth/headers with the already OAuth-fenced runtime."""
    from agent.model_routing_runtime import verify_class_runtime

    if not isinstance(runtime, dict):
        raise ModelRoutingError("Managed auxiliary SDK lacks subscription provenance")
    verify_class_runtime(runtime, runtime["model_routing"])
    if request.get("model") != runtime["model"]:
        raise ModelRoutingError("Managed auxiliary SDK model differs from approved exact route")
    if set(request.get("extra_body") or {}) & _BODY_IDENTITY:
        raise ModelRoutingError("Managed auxiliary body cannot override request identity")
    if {str(k).lower() for k in (request.get("extra_headers") or {})} & _AUTH_HEADERS:
        raise ModelRoutingError("Managed auxiliary request cannot override authentication")
    if str(client.base_url).rstrip("/") != str(runtime["base_url"]).rstrip("/"):
        raise ModelRoutingError("Managed auxiliary SDK endpoint changed")
    expected = runtime["api_key"]
    if runtime["provider"] == "anthropic":
        if client.auth_token != expected or client.api_key:
            raise ModelRoutingError("Managed Anthropic SDK requires the selected OAuth token only")
    elif client.api_key != expected:
        raise ModelRoutingError("Managed auxiliary SDK credential changed")
    headers = {str(k).lower(): v for k, v in client.default_headers.items()}
    # openai>=2.5x sends the key from ``auth_headers`` at request time instead of folding it into
    # ``default_headers``; a custom Authorization header still lands in (and wins from) the latter.
    auth = headers.get("authorization")
    if auth is None:
        auth = {str(k).lower(): v for k, v in (getattr(client, "auth_headers", None) or {}).items()}.get("authorization")
    if auth != "Bearer " + expected:
        raise ModelRoutingError("Managed auxiliary SDK authorization header changed")
    if any(headers.get(k) for k in ("x-api-key", "api-key", "host")):
        raise ModelRoutingError("Managed auxiliary SDK has conflicting authentication headers")


def verify_adapter_request(client, request):
    """Run inside native adapters, including retained and async-to-thread clients."""
    from agent.model_routing_managed import managed_config, verify_selected_route

    config = managed_config()
    if config is None:
        return
    runtime = getattr(client, "_managed_subscription_runtime", None)
    verify_sdk_request(client, request, runtime)
    verify_selected_route(request["model"], runtime["provider"], runtime["model_routing"], config)
