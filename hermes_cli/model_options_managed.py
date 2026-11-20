"""Which concrete pins this host's managed subscription policy lets a turn run. [daveey]

With ``model_routing.enforce_subscription_only`` every turn resolves its concrete model through
``agent.model_routing_managed.pin_policy``: a pin no eligible subscription route registers on its
provider is refused before the agent starts, and no fallback rescues it. ``session.create`` accepted
any pin and the picker (``model.options``) listed every authenticated catalog (201 ids on the hub, 9 of
them runnable), so a pick like ``gemini-4-argon`` minted a chat whose every turn failed. Both now ask
this module, so the picker offers exactly what ``session.create`` accepts.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Every per-row list naming model ids; ``total_models`` counts ``models`` (as the aggregator strip does).
_ID_LISTS = ("models", "featured_models", "unavailable_models", "subscription_overlap_models")


def _policy(config=None):
    """The validated managed config; ``None`` when the policy is off or unreadable (every turn reports that)."""
    from agent.model_routing import ModelRoutingError
    from agent.model_routing_managed import managed_config

    try:
        return managed_config(config)
    except ModelRoutingError as exc:
        logger.warning("model_routing policy unreadable, pins left to the turn: %s", exc)
        return None


def _config_model(policy) -> str:
    """The profile's default model, read as ``agent_factory._config_model_target`` reads it."""
    cfg_model = policy.get("model")
    if isinstance(cfg_model, dict):
        return str(cfg_model.get("default", "") or "").strip()
    return cfg_model.strip() if isinstance(cfg_model, str) else ""


def _refusal(model, provider, policy, allowed):
    from agent.model_routing import is_class_selector
    from agent.model_routing_managed import ManagedPinRejectedError, pin_policy
    from hermes_cli.runtime_provider_custom import rederived_override_provider

    if is_class_selector(model):
        return None  # routed by class, never pinned
    if model not in allowed:  # no route names it, on any provider
        return ManagedPinRejectedError(model, provider, allowed)
    # The turn judges the pin on the provider it re-derives for a bare ``custom`` / empty one.
    judged = rederived_override_provider(model, provider, config_model=_config_model(policy))
    try:
        pin_policy(model, judged, policy)
    except ManagedPinRejectedError as exc:
        return exc
    return None


def managed_pin_refusal(model, provider, config=None):
    """The ``ManagedPinRejectedError`` a turn would raise for ``model`` on ``provider``, else ``None``."""
    from agent.model_routing_managed import allowed_pins

    policy = _policy(config)
    return None if policy is None else _refusal(model, provider, policy, allowed_pins(policy))


def drop_refused_managed_pins(rows, config=None) -> None:
    """Remove, in place, every model id a turn would refuse on its row's provider (``slug``)."""
    from agent.model_routing_managed import allowed_pins

    if (policy := _policy(config)) is None:
        return
    allowed = allowed_pins(policy)
    for row in rows:
        if not isinstance(row, dict):  # shapes as ``drop_unofferable_model_ids``: a crash here blanks the picker
            continue
        lists = {key: row[key] for key in _ID_LISTS if isinstance(row.get(key), list)}
        ids = {model for models in lists.values() for model in models if isinstance(model, str)}
        refused = {model for model in ids if _refusal(model, row.get("slug"), policy, allowed) is not None}
        if not refused:
            continue
        for key, models in lists.items():  # a featured list naming only dropped ids would hide the row's defaults
            row[key] = [model for model in models if not (isinstance(model, str) and model in refused)]
        if "models" in lists and len(row["models"]) != len(lists["models"]):
            row["total_models"] = len(row["models"])
