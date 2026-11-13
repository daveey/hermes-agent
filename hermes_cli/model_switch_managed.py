"""The managed subscription policy's verdict on a ``/model`` switch, before anything is saved. [daveey]

Under ``model_routing.enforce_subscription_only`` every turn resolves a concrete model through
``pin_policy``. A switch that saved a model this host's registry lacks left a chat that failed every
later turn, and on the current provider the switch even reported success (the credential step swallows
the resolver's refusal). This step runs the same check first, on the name the switch would persist.
Class selectors and hosts without the policy pass unchanged.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from hermes_cli.model_switch import ModelSwitchResult, _Switch


def refuse_unregistered_pin(st: _Switch) -> Optional[ModelSwitchResult]:
    from agent.model_routing import ModelRoutingError, is_class_selector
    from agent.model_routing_managed import managed_config, pin_policy
    from hermes_cli.model_normalize import normalize_model_for_provider
    model = normalize_model_for_provider(st.new_model, st.target_provider) if st.new_model else ""
    if not model or is_class_selector(model):
        return None
    try:
        managed = managed_config()
        if managed is not None:
            pin_policy(model, st.target_provider or None, managed)
    except ModelRoutingError as exc:
        return st.fail(str(exc), new_model=model, target_provider=st.target_provider,
                       provider_label=st.provider_label)
    return None
