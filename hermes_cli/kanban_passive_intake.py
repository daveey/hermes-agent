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


def quiet_until(conn, task_id, park, now, *, policy=None) -> int | None:
    """Bound timer deferral by observed custody, not by a registration alone."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_wake_check as wc

    payload = park["payload"]
    cmd = payload.get("wake_check")
    if not current(conn, task_id, cmd, policy=policy, park=park):
        return None
    host, own = payload.get("probe_host"), wc.own_gateway(conn)
    every = wc.normalize_every(payload.get("check_every"))
    if host and own and host != own:
        rows = conn.execute(
            "SELECT payload, created_at FROM task_events WHERE task_id = ? AND id > ? AND kind = ?",
            (task_id, park["event_id"], wc.ALIVE_EVENT)).fetchall()
        observed = max((int(at) for raw, at in rows
                        if (note := kb._json_dict(raw)).get("host") == host
                        and note.get("wake_check") == cmd), default=0)
    else:
        state = wc.get_state(conn, task_id)
        if not state or state["cmd"] != cmd or state["last_rc"] != 1 or state["fails"]:
            return None
        observed = int(state["last_at"] or 0)
    if observed < park["parked_at"] or not observed:
        return None
    deadline = observed + wc.ALIVE_EVERY + 2 * every + wc.SILENT_GRACE + 1
    return deadline if deadline > now else None
