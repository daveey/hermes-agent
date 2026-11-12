"""Whether a new scratch card assigned to a profile has somewhere to run. [daveey]

Without worker pools (legacy routing) this gateway's own dispatcher runs every local profile. With a pool
configured for this gateway, a portable card (``kanban_placement.portable_reason`` is None) is enrolled
``worker-first-v1`` and only a pool worker attesting ``profile:<name>`` may lease it; the origin never runs it
(it waits for a manual repin). The re-park escalation once handed its owner cards to ``mac-ops``, which no pool
worker carries, so the owner sat queued forever while the parked card waited behind it (t_f6d930b7).
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def runnable(name: str) -> bool:
    """False only when a new scratch card for ``name`` would enroll in this gateway's worker pool and no
    active, unheld, picking-up pool member attests ``profile:<name>`` (an unreadable registry counts as none)."""
    from hermes_cli import kanban_placement as placement

    cfg = placement.config()
    pools = cfg.get("worker_pools")
    if not isinstance(pools, dict) or not pools:
        return True
    try:
        members = pools.get(placement.reg.self_gateway())
        if not isinstance(members, list) or not members:
            return True
        if placement.portable_reason({"assignee": name, "workspace_kind": "scratch"}, cfg):
            return True  # not portable: it stays on this gateway, which has the profile
    except Exception:  # health: allow BLE001 -- an unreadable rule set must not invent a runnable pool
        logger.warning("kanban: placement check for profile %s failed", name, exc_info=True)
        return False
    return _attested(name, members)


def runnable_for(name: str, task: object) -> bool:
    """Whether an existing card can run once assigned to ``name``. Its placement is fixed at creation: an
    enrolled ``worker-first-v1`` card runs only on a pool worker attesting ``profile:<name>``, whatever the
    assignee allowlist says (the decomposer once moved two enrolled owners to ``mac-ops``, which no pool
    worker carries); any other card stays on this gateway, which has every local profile."""
    from hermes_cli.kanban_worker_placement import POLICY

    if getattr(task, "placement_policy", None) != POLICY:
        return True
    from hermes_cli import kanban_placement as placement

    try:
        pools = placement.config().get("worker_pools")
        members = pools.get(placement.reg.self_gateway()) if isinstance(pools, dict) else None
    except Exception:  # health: allow BLE001 -- no pool config: judge against every registered machine
        logger.warning("kanban: pool config unreadable for profile %s", name, exc_info=True)
        members = None
    return _attested(name, members if isinstance(members, list) and members else None)


def _attested(name: str, members: list | None) -> bool:
    """An active, unheld, picking-up machine among ``members`` (None = every registered one) attests
    ``profile:<name>``; an unreadable registry counts as none."""
    from hermes_cli import kanban_worker_node as wn

    try:
        snapshot = wn.registry()
    except Exception:  # health: allow BLE001 -- no Board identity/registry: no pool worker is known to run it
        logger.warning("kanban: machine registry unreadable for profile %s", name, exc_info=True)
        return False
    machines = snapshot.get("machines") if isinstance(snapshot, dict) else None
    if not isinstance(machines, dict):
        return False
    label = f"profile:{name}"
    for member in machines if members is None else members:
        entry = machines.get(member)
        if (isinstance(entry, dict) and entry.get("state", "active") == "active" and not entry.get("hold")
                and wn.advertises_pickup(entry) and label in (entry.get("labels") or [])):
            return True
    return False
