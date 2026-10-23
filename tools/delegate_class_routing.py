"""Defer delegated class allocation until the child owns a durable session ID."""
from agent.model_routing import ModelRoutingError, is_class_selector


def child_selector(model, provider, parent, *, base_url=None, api_key=None, api_mode=None, command=None):
    if not model and provider in (None, "", "auto") and not any((base_url, api_key, api_mode, command)):
        metadata = getattr(parent, "model_routing", None)
        if isinstance(metadata, dict) and metadata.get("requested_class"):
            model = "class:" + metadata["requested_class"]
    if not is_class_selector(model):
        return None
    if provider not in (None, "", "auto") or any((base_url, api_key, api_mode, command)):
        raise ModelRoutingError("Delegated classes require provider auto and no transport/credential overrides")
    return model


def child_runtime(selector, parent, config):
    # A child is a new job, not a compression continuation. Inherit the class,
    # not the parent's allocation, credential lease, endpoint or paid fallback.
    from hermes_constants import parse_reasoning_effort
    reasoning = getattr(parent, "reasoning_config", None)
    effort = config.get("reasoning_effort")
    if effort or effort is False:
        parsed = parse_reasoning_effort(effort)
        if parsed is not None:
            reasoning = parsed
    result = dict(model=selector, provider="auto", requested_provider="auto",
                  base_url=None, api_key=None, api_mode=None, acp_command=None,
                  acp_args=[], capabilities=None, fallback_model=[], reasoning_config=reasoning)
    max_tokens = getattr(parent, "max_tokens", None)
    if isinstance(max_tokens, int):
        result["max_tokens"] = max_tokens
    return result


def attach_child_pool(child, runtime, parent):
    # Class construction already selected a billing-fenced native pool. Replacing
    # it with the parent's unrestricted pool would undo the OAuth-only fence.
    if getattr(child, "model_routing", None):
        return
    from tools.delegate_tool import _resolve_child_credential_pool
    pool = _resolve_child_credential_pool(runtime["provider"], parent, runtime["base_url"],
                                          effective_requested_provider=runtime.get("requested_provider"))
    if pool is not None:
        child._credential_pool = pool
