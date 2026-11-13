"""Narrow creator cleanup over the authenticated operator courier. [daveey]

The replica is only a preflight hint. The home rechecks creator and claim state
inside the archive transaction; operator_call authenticates the requesting run.
"""
from __future__ import annotations

ACTION = "creator_archive"


def refusal(conn, target: str, requester: str | None) -> str | None:
    from hermes_cli.kanban_archive_scope import _creator_task_id, archive_scope_for
    if not requester or requester == target or _creator_task_id(conn, target) != requester:
        return "creator_archive requires a different card created by the requesting task"
    return archive_scope_for(conn, target, requester)[0]


def forward(conn, target: str, requester: str, run_id):
    from hermes_cli import kanban_supervisor_requests as requests
    try:
        out = requests.submit(conn, env_task=requester, env_run=str(run_id) if run_id else None,
                              ops=[{"op": "operator", "action": ACTION, "task_id": target,
                                    "reason": "Creator cleanup through kanban_archive"}])
    except requests.SupervisorRequestError as exc:
        return str(exc), None
    return None, {"status": "archive_pending", "request_id": out["request_id"],
                  "message": "Creator archive queued for the card's home gateway; not yet archived."}


def apply(conn, payload: dict, extra: dict):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_operator_call import _marker
    target, uid = payload["target_task_id"], payload["request_uid"]
    requester = payload["caller"].get("task_id")
    if _marker(conn, target, "archived", uid):
        return "already_applied", None
    why = refusal(conn, target, requester)
    if why:
        return "refused", why
    ok = kb.archive_task(conn, target, via="creator archive operator call", event_extra=extra,
                         precondition=lambda c: refusal(c, target, requester))
    return ("applied", None) if ok else ("refused", "archive scope changed before application")
