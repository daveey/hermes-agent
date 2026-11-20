"""Hard lane worker ceilings, independent of soft shares and priority exceptions.

Use the existing fleet/local running census. The final check is inside the local
claim transaction, so serial claims cannot spend one local slot twice. Replica
observations are not a distributed reservation across gateways.
"""
from hermes_cli import kanban_lanes as lanes


def _policy():
    pol = lanes.safe_policy()
    if pol is None or pol.shadow or not any(s.max_workers is not None for s in pol.lanes.values()):
        return None
    return pol


def at_capacity(conn, task_id):
    """Recheck immediately before ready/review CAS; an echoed own claim counts only once."""
    pol = _policy()
    if pol is None:
        return False
    row = conn.execute("SELECT tenant, status FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        return False
    name = pol.lane_of(row["tenant"])
    cap = pol.lanes[name].max_workers
    if cap is None:
        return False
    count = lanes._running(conn, pol)[name] - int(row["status"] == "running")
    return count >= cap


def drop_full(conn, rows):
    """Filter full lanes without consuming slots for candidates not actually claimed."""
    pol = _policy()
    if pol is None or not rows:
        return rows
    counts = lanes._running(conn, pol)
    names, _alarms = lanes._candidate_lanes(conn, pol, [row["id"] for row in rows])
    full = {name for name, spec in pol.lanes.items()
            if spec.max_workers is not None and counts[name] >= spec.max_workers}
    return [row for row in rows if names.get(row["id"], pol.default) not in full]
