"""Opt-in managed subscription policy, including concrete and persisted pins.

A concrete name is not spending authorization. Unknown pins fail closed rather
than acquiring a guessed class; approved native pins retain their exact tuple.
"""
from copy import deepcopy

from agent.model_routing import ModelRoutingError, eligible_routes, is_class_selector, validate_policy


def managed_config(config=None):
    if config is None:
        from hermes_cli.config_effective import load_user_config_effective
        config = load_user_config_effective()
    policy = config.get("model_routing", {})
    if not policy.get("enabled") or not policy.get("enforce_subscription_only"):
        return None
    validate_policy(policy)
    return config


def pin_policy(model, provider, config):
    """Only an unambiguous, eligible exact native tuple is an approved pin."""
    policy = config["model_routing"]
    matches = [(level, route) for level in policy.get("routes", {})
               for route in eligible_routes(policy, level)
               if route["billing"] == "subscription" and route["model"] == model
               and provider in (None, "", "auto", route["provider"])]
    if len(matches) != 1:
        raise ModelRoutingError("Managed subscription policy rejects unregistered or ambiguous concrete pin")
    level, route = matches[0]
    restricted = deepcopy(config)
    restricted["model_routing"]["routes"] = {level: [route]}
    restricted["model_routing"]["allow_metered"] = False
    return level, restricted


def resolve_managed_runtime(model, provider, *, config, session_id=None, requirements=None,
                            prepare_runtime=None, replace=False):
    from agent.model_routing_runtime import resolve_class_runtime
    from hermes_state_ids import new_session_id
    level, restricted = pin_policy(model, provider, config)
    runtime = resolve_class_runtime("class:" + level, "auto", config=restricted,
                                    session_id=session_id or new_session_id(), requirements=requirements,
                                    exact_pin=True, replace=replace, prepare_runtime=prepare_runtime)
    return runtime


def verify_selected_route(model, provider, metadata, config):
    """Validate a resolved tuple in its recorded class, not every registry class."""
    if not metadata or metadata.get("billing_group") != "subscription":
        raise ModelRoutingError("Managed request lacks verified subscription provenance; recreate runtime")
    if (metadata.get("selected_model"), metadata.get("selected_provider")) != (model, provider):
        raise ModelRoutingError("Managed request changed selected route identity")
    routes = eligible_routes(config["model_routing"], metadata.get("requested_class"))
    if not any(route["model"] == model and route["provider"] == provider
               and route["billing"] == "subscription" for route in routes):
        raise ModelRoutingError("Managed request route is no longer eligible in its recorded class")


def verify_managed_agent(agent, request):
    """Fence cached agents and stale overrides at each outbound attempt."""
    config = managed_config()
    if config is None:
        return
    model = request.get("model", agent.model)
    if is_class_selector(model) or model != agent.model:
        raise ModelRoutingError("Managed request model differs from the approved runtime")
    metadata = getattr(agent, "model_routing", None)
    verify_selected_route(model, agent.provider, metadata, config)
    from agent.model_routing_runtime import verify_class_runtime
    verify_class_runtime(agent_runtime(agent), metadata)


def agent_runtime(agent):
    native = agent.provider == "anthropic"
    return {
        "model": agent.model, "provider": agent.provider, "api_mode": agent.api_mode,
        "api_key": getattr(agent, "_anthropic_api_key", None) if native else agent.api_key,
        "base_url": getattr(agent, "_anthropic_base_url", None) if native else agent.base_url,
        "credential_pool": agent._credential_pool, "model_routing": agent.model_routing,
    }


def verify_main_sdk(agent, client, request):
    """Final native callback fence, after Relay and request-client checkout."""
    if managed_config() is None:
        return
    verify_managed_agent(agent, request)
    from agent.model_routing_auxiliary_guard import verify_sdk_request
    verify_sdk_request(client, request, agent_runtime(agent))


def prepare_anthropic_stream(agent, client, request):
    from agent.anthropic_adapter import sanitize_anthropic_kwargs
    verify_main_sdk(agent, client, request)
    result = dict(request)
    sanitize_anthropic_kwargs(result, log_prefix=getattr(agent, "log_prefix", ""))
    return result


def verify_managed_auxiliary(client, request, provider):
    config = managed_config()
    if config is None:
        return
    runtime = getattr(client, "_managed_subscription_runtime", None)
    if not isinstance(runtime, dict):
        raise ModelRoutingError("Managed auxiliary request lacks verified subscription provenance")
    model = request.get("model")
    metadata = runtime["model_routing"]
    verify_selected_route(model, provider, metadata, config)
    if (model != runtime["model"] or provider != runtime["provider"] or
            metadata["billing_group"] != "subscription"):
        raise ModelRoutingError("Managed auxiliary request changed subscription identity")
    from agent.model_routing_runtime import verify_class_runtime
    verify_class_runtime(runtime, metadata)
    from agent.model_routing_auxiliary_guard import verify_sdk_request
    verify_sdk_request(getattr(client, "_real_client", client), request, runtime)


def finish_managed_switch(agent, runtime):
    if runtime is None:
        return
    from agent.model_routing_agent import persist_class_metadata
    agent.model_routing = runtime["model_routing"]
    agent._credential_pool = runtime["credential_pool"]
    agent._model_routing_requirements = getattr(agent, "_model_routing_requirements", {})
    agent._fallback_chain = []
    agent._fallback_model = None
    persist_class_metadata(agent)
