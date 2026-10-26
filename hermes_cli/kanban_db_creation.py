"""Creation lifecycle events, shared by ordinary and origin-bound tasks."""


def record_created(conn, task_id, *, assignee, status, parents, creator_task_id,
                   tenant, workspace_kind, workspace_path, branch_name, project_id,
                   skills, goal_mode, model_override, provider_override, created_by, requires):
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
        kb._append_event(conn, task_id, "blocked", {
            "reason": "initial_status", "status": "blocked", "actor": created_by or "user",
        })
    if status == "todo":
        # Match link_tasks: a card parked behind a parent needs a reason.
        gating = [p for p in parents if kb._task_status(conn, p) not in ("done", "archived")]
        if gating:
            kb._append_event(conn, task_id, "dependency_wait", {
                "reason": "parent_not_done", "parent": gating[0],
            })
