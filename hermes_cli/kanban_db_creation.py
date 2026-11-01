"""Creation lifecycle events, shared by ordinary and origin-bound tasks."""
import json


def record_created(conn, task_id, *, assignee, status, parents, creator_task_id,
                   tenant, workspace_kind, workspace_path, branch_name, project_id,
                   skills, goal_mode, model_override, provider_override, created_by, requires,
                   human_request=None):
    from hermes_cli import kanban_db as kb

    kb._append_event(conn, task_id, "created", {
        "assignee": assignee,
        "status": status,
        "parents": list(parents),
        "creator_task_id": creator_task_id,
        "tenant": tenant,
        "workspace_kind": workspace_kind,
        "workspace_path": workspace_path,
        "branch_name": branch_name,
        "project_id": project_id,
        "skills": list(skills) if skills else None,
        "requires": requires,
        "goal_mode": bool(goal_mode) or None,
        "model_override": model_override,
        "provider_override": provider_override,
    })
    if status == "blocked":
        # A creation-time human ask is a decision wait: validate_human_request
        # already refuses dependency/transient/time alongside one, and create
        # has no kind argument. Without an ask (lint_blocked=False trackers)
        # leave block_kind NULL rather than invent one.
        block_kind = "needs_input" if human_request else None
        conn.execute("UPDATE tasks SET human_request = ?, block_kind = ? WHERE id = ?",
                     (json.dumps(human_request) if human_request else None, block_kind, task_id))
        from hermes_cli.kanban_ask_resolved import filing_fields, sync_row
        sync_row(conn, task_id)  # an ask with resolved_check gets its probe row [daveey]
        payload = {
            "reason": "initial_status", "status": "blocked", "actor": created_by or "user",
            "human_request": human_request, **filing_fields(conn, human_request),
        }
        if block_kind:
            payload["kind"] = block_kind
        kb._append_event(conn, task_id, "blocked", payload)
    if status == "todo":
        # Match link_tasks: a card parked behind a parent needs a reason.
        gating = [p for p in parents if kb._task_status(conn, p) not in ("done", "archived")]
        if gating:
            kb._append_event(conn, task_id, "dependency_wait", {
                "reason": "parent_not_done", "parent": gating[0],
            })
