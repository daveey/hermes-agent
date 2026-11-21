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
  the card home (:data:`REASON`): mixed versions fail closed. The card leaves home only through the worker
  queue, whose ``runs_on`` carries the label into the Board lease; it never enrolls worker-first
  (:func:`worker_first_refusal`), and a move keeps the label (:func:`carry`);
* the claiming machine refuses a mandatory pin it cannot run exactly (:func:`refusal`): the card stays on
  the Board for a machine that can. There is never a fallback to another model, and a stored mandatory
  pin is never cleared (``kanban_model_assignment.route_model_mismatch``).

"Mandatory" here is the ``tasks.model_pin_mandatory`` column. A ``require_model: true`` body line
(``kanban_provider_walls.requires_model``) only exempts a pin from the quota reroute; it is not placed, gated or
protected here, as before this module.

A pin that is not mandatory keeps every substitution it had (quota reroute, registry clear).
"""
from __future__ import annotations

import logging
from typing import Any, Mapping, Optional

_log = logging.getLogger(__name__)

LABEL = "capability:mandatory-model-pin"
REASON = "mandatory model pin unattested"
WORKER_FIRST_REASON = "a mandatory model pin leaves home only through the worker queue"
_logged: set[tuple[str, str]] = set()


def _str(value: Any) -> str:
    return str(value or "").strip()


def pin_of(item: Mapping[str, Any]) -> Optional[tuple[str, str]]:
    """``(model, provider)`` of a mandatory pin, from create args (``pin_mandatory``/``model``/``provider``) or a
    card row (``model_pin_mandatory``/``model_override``/``provider_override``); None when there is none.
    A mark without a model pins nothing (``kanban_pin_reroute.mandatory_column`` refuses it at create)."""
    # Stored/synced mandatory flags remain historical evidence, never placement
    # authority. New flags are rejected at ingress instead of transported.
    return None


def requirements(item: Mapping[str, Any]) -> set[str]:
    """The attestation labels a destination must carry to take ``item`` (create args or a card row)."""
    return {LABEL} if pin_of(item) else set()


def worker_first_refusal(item: Mapping[str, Any]) -> Optional[str]:
    """Why ``item`` (create args or a card row) may not enroll worker-first, or None.

    Only the worker queue carries a mandatory pin: there :data:`LABEL` sits in ``runs_on``, and the Board leases
    the card only to a machine carrying every label. The worker-first pool's Board lease checks just profile,
    ``requires`` and skill labels, and the create-time attestation sees only members eligible now, so a held or
    stale old-core member could be assigned the card later (review 5878972889 on #1622)."""
    return WORKER_FIRST_REASON if pin_of(item) else None


def carry(labels: list[str], item: Mapping[str, Any], *, me: str = "") -> list[str]:
    """``labels`` (a new ``runs_on``) with :data:`LABEL` added when ``item`` has a mandatory pin, so a move keeps
    the attestation (``[pool:worker]`` alone would let an old core lease it). Unchanged when every ``machine:``
    label names ``me`` and no ``pool:`` label is present: this machine runs this core, which refuses a pin it
    cannot serve. A ``pool:`` label always gets it (fail closed: the pool may hold an old core)."""
    need = requirements(item)
    if not need or need <= set(labels):
        return list(labels)
    machines = [label[8:] for label in labels if label.startswith("machine:")]
    pooled = any(label.startswith("pool:") for label in labels)
    if not pooled and machines and me and all(name == me for name in machines):
        return list(labels)
    return sorted(set(labels) | need)


def mark_refusal(conn: Any, task_id: str) -> Optional[str]:
    """Why ``task_id`` may not be marked mandatory now, or None. A worker-first card's ``runs_on`` is Board-owned
    (its pool lease never checks :data:`LABEL`), so marking it would let an old core run it on another model."""
    from hermes_cli import kanban_db_sync as sync
    if "placement_policy" not in sync.table_columns(conn, "tasks"):
        return None
    row = conn.execute("SELECT placement_policy FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None or not row[0]:
        return None
    return (f"{task_id} is placed worker-first ({row[0]}): {WORKER_FIRST_REASON}. Move it to the worker queue "
            f"(`hermes kanban move {task_id} --pool --reason ...`) first, or recreate it with the mark")


def mark(conn: Any, task_id: str) -> bool:
    """Set ``model_pin_mandatory`` on a placed card and union :data:`LABEL` into its ``runs_on`` in the same write
    (``hermes kanban set-model <id> --mandatory``), so a later mark is fenced like a create-time one (review
    5879416189 on #1622). Raises ValueError for a card without a model pin or a worker-first card
    (:func:`mark_refusal`), RuntimeError for an archived one. False when the card does not exist."""
    from hermes_cli.kanban_model_neutral import ERROR
    raise ValueError(ERROR)


def _gateway_id(conn: Any) -> str:
    """The board file's own gateway id (``kanban_sync_state``), or "" when it was never attached. Read from the
    file, not ``board_mode``: that answers ``off`` in a process that did not install sync."""
    import sqlite3
    try:
        row = conn.execute("SELECT gateway_id FROM kanban_sync_state LIMIT 1").fetchone()
    except sqlite3.Error:
        return ""
    return _str(row[0]) if row else ""


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
