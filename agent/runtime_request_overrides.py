"""Caller-owned request body settings, distinct from model-scoped provider defaults."""
from copy import deepcopy


def capture_caller_body(agent):
    """Capture before custom-provider defaults merge; never infer ownership afterward."""
    agent._caller_extra_body = deepcopy((agent.request_overrides or {}).get("extra_body") or {})
    agent._caller_extra_body_route = (agent.provider, agent.base_url.rstrip("/"))


def switched_extra_body(agent, provider_defaults):
    """Caller settings survive model switches only within their original provider/endpoint."""
    body = deepcopy(provider_defaults or {})
    route = (agent.provider, agent.base_url.rstrip("/"))
    if route == getattr(agent, "_caller_extra_body_route", None):
        body.update(deepcopy(getattr(agent, "_caller_extra_body", {})))
    return body
