"""Explicit, exact-probe standing listeners; no persisted exemption or schema."""
from __future__ import annotations

import re


def snapshot() -> dict[str, str]:
    """Resolve on the scoped caller, then carry this value into probe workers."""
    from hermes_cli.config import load_config_readonly
    from hermes_cli.config_backend import ConfigBackendUnavailable

    try:
        cfg = load_config_readonly()
    except (ConfigBackendUnavailable, OSError, ValueError):
        return {}
    section = cfg.get("kanban") if isinstance(cfg, dict) else None
    value = section.get("passive_intake") if isinstance(section, dict) else None
    if not isinstance(value, dict):
        return {}
    # A malformed mapping disables the policy, rather than partially granting it.
    for tid, cmd in value.items():
        if (not isinstance(tid, str) or not re.fullmatch(r"t_[0-9a-f]+", tid)
                or not isinstance(cmd, str) or not cmd.strip()):
            return {}
    return dict(value)


def eligible(task_id, cmd, awaiting, blocker_id=None, *, policy=None) -> bool:
    """One permissive exit, only an explicitly declared external producer."""
    from hermes_cli.kanban_wait_info import producer_from_wake_check

    policy = snapshot() if policy is None else policy
    if not isinstance(policy, dict) or not isinstance(cmd, str) or not cmd.strip():
        return False
    if policy.get(task_id) != cmd or blocker_id is not None:
        return False
    if not isinstance(awaiting, dict) or awaiting.get("kind") != "external" or awaiting.get("inferred"):
        return False
    for key in ("ref", "why"):
        if not isinstance(awaiting.get(key), str) or not awaiting[key].strip():
            return False
    producer = producer_from_wake_check(cmd)
    if not producer or producer[0] != "probe":
        return False
    return True


def current(conn, task_id, cmd, *, policy=None, park=None) -> bool:
    from hermes_cli.kanban_continuation import park_state

    park = park_state(conn, task_id) if park is None else park
    payload = park["payload"]
    if not park["timed"] or payload.get("wake_check") != cmd:
        return False
    return eligible(task_id, cmd, payload.get("awaiting"), payload.get("blocker_id"), policy=policy)
