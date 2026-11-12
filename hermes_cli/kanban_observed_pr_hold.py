"""Current-episode dispatcher observations for operator-only resume."""
from __future__ import annotations


# Any lifecycle transition invalidates older observations, including a consuming
# claim. Diagnostic/comment events deliberately do not start another episode.
_BOUNDARIES = frozenset({
    "created", "promoted", "promoted_manual", "unblocked", "reopened", "status",
    "claimed", "dependency_wait", "blocked", "scheduled", "continued", "reclaimed",
    "crashed", "timed_out", "completed", "archived", "assigned", "review_requested",
    "review_reopened", "changes_requested", "operator_resumed",
})
_READY = frozenset({"promoted", "promoted_manual", "unblocked", "reopened", "reclaimed", "created"})


def _episode(conn, task_id):
    from hermes_cli import kanban_db as kb

    events = list(conn.execute("SELECT * FROM task_events WHERE task_id = ?", (task_id,)))
    timed = [(kb._to_epoch(e["created_at"]), e) for e in events]
    if any(at is None for at, _ in timed):
        return None, False
    ordered = sorted(timed, key=lambda pair: (pair[0], pair[1]["id"]))
    boundary = next((e for _, e in reversed(ordered) if e["kind"] in _BOUNDARIES), None)
    if boundary is None or boundary["kind"] not in _READY:
        return None, False
    point = (kb._to_epoch(boundary["created_at"]), boundary["id"])
    held = any((at, e["id"]) > point and e["kind"] == "respawn_guarded"
               and kb._json_dict(e["payload"]).get("reason") == "active_pr" for at, e in ordered)
    return boundary, held


def observed(conn, task_id):
    """Local or fresh Board evidence, bound to the home ready episode's UID."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_dependency_history import _with_fresh_replica

    boundary, held = _episode(conn, task_id)
    if held:
        return True
    if boundary is None or "uid" not in boundary.keys() or not boundary["uid"]:
        return False

    def read(replica):
        remote, held = _episode(replica, task_id)
        return bool(held and remote is not None and "uid" in remote.keys()
                    and remote["uid"] == boundary["uid"])

    return _with_fresh_replica(conn, kb._to_epoch(boundary["created_at"]), read, False)
