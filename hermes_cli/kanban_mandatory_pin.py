"""A mandatory model pin travels with a pool card: the claiming worker runs exactly that pin or refuses. [daveey]

``kanban_create(model=..., pin_mandatory=true)`` used to keep every card on its home gateway: placement listed
``pin_mandatory`` among the courier's uncarried options, and pool portability reused that list, so the card's
``portable_reason`` was ``uncarried option`` (t_01a1cefa6145100f, an Opus exact-head review, 11-12). The pool
never needed the courier: ``model_override``, ``provider_override`` and ``model_pin_mandatory`` are Board row
fields (``kanban_db_sync.SYNCED_COLUMNS``) a worker's pickup copies with the card.

What the pool route needs instead, and where it lives:

* every pool destination must run a core that refuses a mandatory pin it cannot serve. That is the
  attestation :data:`LABEL` (published only for a worker whose core contains this change, like
  ``pool:worker``). The creator checks it on every eligible pool member (:func:`requirements` through
  ``kanban_placement_diagnostics.portability``), the destination checks it on itself
  (``kanban_placement_coordination.claim_refusal``). A pool without it, or an unknown registry, keeps
  the card home (:data:`REASON`): mixed versions fail closed;
* the claiming machine refuses a mandatory pin it cannot run exactly (:func:`refusal`): the card stays on
  the Board for a machine that can. There is never a fallback to another model, and a stored mandatory
  pin is never cleared (``kanban_model_assignment.route_model_mismatch``).

A pin that is not mandatory keeps every substitution it had (quota reroute, registry clear).
"""
from __future__ import annotations

import logging
from typing import Any, Mapping, Optional

_log = logging.getLogger(__name__)

LABEL = "capability:mandatory-model-pin"
REASON = "mandatory model pin unattested"
_logged: set[tuple[str, str]] = set()


def _str(value: Any) -> str:
    return str(value or "").strip()


def pin_of(item: Mapping[str, Any]) -> Optional[tuple[str, str]]:
    """``(model, provider)`` of a mandatory pin, from create args (``pin_mandatory``/``model``/``provider``) or a
    card row (``model_pin_mandatory``/``model_override``/``provider_override``); None when there is none.
    A mark without a model pins nothing (``kanban_pin_reroute.mandatory_column`` refuses it at create)."""
    if not (item.get("pin_mandatory") or item.get("model_pin_mandatory")):
        return None
    model = _str(item.get("model_override")) or _str(item.get("model"))
    if not model:
        return None
    return model, (_str(item.get("provider_override")) or _str(item.get("provider"))).lower()


def requirements(item: Mapping[str, Any]) -> set[str]:
    """The attestation labels a destination must carry to take ``item`` (create args or a card row)."""
    return {LABEL} if pin_of(item) else set()


def refusal(card: Mapping[str, Any], *, me: str = "") -> Optional[str]:
    """None when this machine can run ``card``'s mandatory pin exactly (or it has none), else why not.

    Configuration checks a worker would otherwise fail at startup, or answer with another model: the assignee
    profile must exist here, the model/provider family must match, the managed routing registry must accept the
    pin, the provider must not be disabled, and a class pin must be routable. Credential exhaustion is a quota
    wall (``kanban_quota_episode``), not a refusal. Never raises: an unreadable config refuses."""
    pin = pin_of(card)
    if pin is None:
        return None
    model, provider = pin
    reason = _why_not(card.get("assignee"), model, provider)
    if reason is None:
        return None
    reason = f"mandatory model pin {model}{f' on {provider}' if provider else ''} cannot run here: {reason}"
    key = (str(card.get("id")), reason)
    if key not in _logged:
        _logged.add(key)
        _log.info("kanban: not claiming %s on %s, left for a machine that serves its pin: %s",
                  card.get("id"), me or "this machine", reason)
    return reason


def _why_not(assignee: Any, model: str, provider: str) -> Optional[str]:
    from agent.model_routing import is_class_selector
    from hermes_cli.kanban_model_assignment import (
        ModelProviderMismatchError, _assignee_config, validate_model_assignment,
    )
    try:
        config = _assignee_config(_str(assignee) or None)
    except Exception as exc:  # health: allow BLE001 -- an unreadable profile refuses, it never guesses
        return f"profile config unreadable ({type(exc).__name__})"
    if not isinstance(config, dict):
        return f"no profile {_str(assignee)!r} here"
    if is_class_selector(model):
        from hermes_cli.kanban_model_tier import class_unroutable_reason
        why = class_unroutable_reason(model, config)
        return f"class unroutable ({why})" if why else None
    try:
        validate_model_assignment(_str(assignee) or None, model, provider or None)
    except ModelProviderMismatchError as exc:
        return exc.code
    if provider and provider != "auto":
        from hermes_cli.config import is_provider_enabled
        providers = config.get("providers")
        block = providers.get(provider) if isinstance(providers, dict) else None
        if not is_provider_enabled(block if isinstance(block, dict) else None):
            return "provider disabled"
    return None
