"""Opt-in managed subscription policy, including concrete and persisted pins.

A concrete name is not spending authorization. Unknown pins fail closed rather
than acquiring a guessed class; approved native pins retain their exact tuple.
"""
from copy import deepcopy

from agent.model_routing import ModelRoutingError, eligible_routes, is_class_selector, validate_policy


class HostRoutingConfigError(ModelRoutingError):
    """The host's ``model_routing`` registry could not be read or validated (``managed_config``). [daveey]

    Raised only where the host config is read and validated, so it names the host's registry, never
    a card's pin: ``hermes_cli.kanban_host_refusal`` reads it to keep a Kanban start failure off the
    card. Still a ``ModelRoutingError``: every caller that fails closed on one keeps doing so."""


def managed_config(config=None):
    if config is None:
        from hermes_cli.config_effective import load_user_config_effective
        config = load_user_config_effective()
    policy = config.get("model_routing", {})
    if not isinstance(policy, dict):
        raise HostRoutingConfigError("model_routing must be a mapping")
    if not policy.get("enabled") or not policy.get("enforce_subscription_only"):
        return None
    try:
        validate_policy(policy)
    except ModelRoutingError as exc:
        raise HostRoutingConfigError(str(exc)) from exc
    return config


def overrides_endpoint(provider, base_url, config):
    """True when *base_url* asks for anything but *provider*'s own endpoint. [daveey]

    Adoption freezes a Desktop chat's saved endpoint into its launch request, so a native URL reaches
    the resolver as "explicit" while overriding nothing. ``auto`` and bare ``custom`` have no endpoint
    of their own, and an unknown host proves nothing, so those stay overrides.
    """
    if not base_url:
        return False
    from hermes_cli.providers import normalize_provider
    from hermes_cli.route_identity import provider_owns_route
    canonical = normalize_provider(str(provider or "").strip().lower())
    return (canonical in ("", "auto", "custom", "local")
            or provider_owns_route(canonical, base_url, config) is not True)


class ManagedPinRejectedError(ModelRoutingError):
    """A concrete pin the registry cannot serve. Config, not a transient: a retry never heals it. [daveey]

    ``registry_empty``: the registry served no eligible subscription pin at all, so no pin of any
    card could pass. That is the host's registry, not this pin (a config outage that emptied the
    routes); ``hermes_cli.kanban_host_refusal`` reads it to keep such a start failure off the card."""

    def __init__(self, model, provider, allowed):
        self.model, self.provider, self.allowed = model, provider, list(allowed)
        self.registry_empty = not self.allowed
        # Every chat, gateway.log and alert shows only str(exc): name the pin and the way out.
        on = f" on provider {provider!r}" if provider else ""
        allowed_text = ", ".join(self.allowed) or "none (the registry serves no subscription pin)"
        super().__init__(
            f"Managed subscription policy rejects unregistered or ambiguous concrete pin {model!r}{on}. "
            f"Allowed: {allowed_text}. Pick one with /model, or register it in this host's model_routing.routes.")


def allowed_pins(config):
    """Sorted model names an exact pin may name under ``config``'s managed policy."""
    policy = config["model_routing"]
    return sorted({route["model"] for level in policy.get("routes", {})
                   for route in eligible_routes(policy, level)
                   if route["billing"] == "subscription" or level == "carbon"})


def pin_policy(model, provider, config):
    """Only an unambiguous, eligible exact native tuple is an approved pin."""
    policy = config["model_routing"]
    matches = [(level, route) for level in policy.get("routes", {})
               for route in eligible_routes(policy, level)
               if (route["billing"] == "subscription" or level == "carbon") and route["model"] == model
               and provider in (None, "", "auto", route["provider"])]
    if len(matches) != 1:
        raise ManagedPinRejectedError(model, provider, allowed_pins(config))
    level, route = matches[0]
    restricted = deepcopy(config)
    restricted["model_routing"]["routes"] = {level: [route]}
    restricted["model_routing"]["allow_metered"] = False
    return level, restricted


def resolve_managed_runtime(model, provider, *, config, session_id=None, requirements=None,
                            prepare_runtime=None, replace: bool | str = False):
    from agent import model_routing_walls as walls
    from agent.model_routing_runtime import is_temporary_resolution, resolve_class_runtime
    from agent.model_routing_unavailable import SubscriptionQuotaUnavailable
    from hermes_state_ids import new_session_id
    level, restricted = pin_policy(model, provider, config)
    durable = bool(session_id) and not is_temporary_resolution()
    session_id = session_id or new_session_id()
    source = walls.request_pin_source(model, provider, config, replace)
    if durable and not replace:
        # [daveey] A rerouted default pin stays on its reroute: the session's stored model still
        # names the walled one, and resolving that again must not move the chat back mid-episode.
        followed = walls.follows_reroute(walls.peek_route(session_id, level), model)
        if followed is not None:
            try:
                level, restricted = pin_policy(followed[1], followed[0], config)
            except ModelRoutingError:
                pass  # the reroute left the registry: resolve the session's own pin
    try:
        return resolve_class_runtime("class:" + level, "auto", config=restricted, session_id=session_id,
                                     requirements=requirements, exact_pin=True, replace=replace,
                                     prepare_runtime=prepare_runtime, pin_source=source)
    except SubscriptionQuotaUnavailable as wall:
        # The pre-build lookup (temporary) never reroutes: its caller defers to the agent build.
        if not durable or replace or not walls.may_reroute(wall, walls.peek_route(session_id, level), source):
            raise
        walled_from = {"provider": wall.provider, "model": wall.model, "resets_at": wall.resets_at}
        try:
            runtime = resolve_class_runtime("class:" + level, "auto", config=walls.class_config(config, level),
                                            session_id=session_id, requirements=requirements,
                                            excluded=[(wall.provider, wall.model)], walled_from=walled_from,
                                            prepare_runtime=prepare_runtime, pin_source="default")
        except ModelRoutingError:
            raise wall from None  # nothing else in the class can serve: the wall is the answer
        walls.log_reroute(wall, runtime["model_routing"])
        return runtime


def verify_selected_route(model, provider, metadata, config):
    """Validate a resolved tuple in its recorded class, not every registry class."""
    if not metadata or (metadata.get("billing_group") != "subscription"
                        and metadata.get("requested_class") != "carbon"):
        raise ModelRoutingError("Managed request lacks verified subscription provenance; recreate runtime")
    if (metadata.get("selected_model"), metadata.get("selected_provider")) != (model, provider):
        raise ModelRoutingError("Managed request changed selected route identity")
    routes = eligible_routes(config["model_routing"], metadata.get("requested_class"))
    if not any(route["model"] == model and route["provider"] == provider
               and route["billing"] == metadata["billing_group"] for route in routes):
        raise ModelRoutingError("Managed request route is no longer eligible in its recorded class")


def verify_managed_agent(agent, request):
    """Fence cached agents and stale overrides at each outbound attempt."""
    from hermes_cli.kanban_refusal_boundary import require_policy
    require_policy(getattr(agent, '_kanban_refusal_policy', None),
                   model=request.get('model', agent.model), provider=agent.provider, request=True)
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
