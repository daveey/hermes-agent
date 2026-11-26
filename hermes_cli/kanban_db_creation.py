"""Creation lifecycle events, shared by ordinary and origin-bound tasks."""
import json


def validate_worktree(validate_workspace, workspace_kind, project_id, workspace_path, board):
    """Worktree declaration preflight, before allocating a task ID."""
    if validate_workspace and workspace_kind == "worktree" and not project_id:
        from hermes_cli.kanban_db_workspace import (
            WorktreeWorkspaceConfigError, worktree_workspace_problem,
        )
        if problem := worktree_workspace_problem(workspace_path, board=board):
            raise WorktreeWorkspaceConfigError(problem)


def existing_creation_key(conn, key):
    """Legacy best-effort lookup; the caller chooses the transaction boundary."""
    return conn.execute(
        "SELECT id FROM tasks WHERE idempotency_key = ? AND status != 'archived' "
        "ORDER BY created_at DESC LIMIT 1", (key,),
    ).fetchone()


def reject_reserved_key(key, created_by) -> None:
    """Refuse a detector-namespace idempotency key from an ordinary creator, before any lookup or write.

    The Board fences ``pickup-stall:``/``worker-resources-unknown:`` keys to the pickup-stall detector and rejects
    the whole never-created card (and every child op) otherwise. The detector itself is unchanged. Existing bad
    cards are corrected only by ``hermes kanban sync reserved-key`` (``kanban_sync_reserved_key``)."""
    from hermes_cli.kanban_pickup_alarm_identity import DETECTOR, reserved
    if reserved(key) and created_by != DETECTOR:
        raise ValueError(f"idempotency_key {key!r} is reserved for the pickup-stall detector; choose another key")


def default_ready_assignee() -> str:
    """``kanban.default_assignee`` (config) or the ``default`` profile."""
    try:
        from hermes_cli.config import load_config

        name = str(((load_config().get("kanban") or {}).get("default_assignee")) or "").strip()
    except Exception:
        name = ""
    return name or "default"


def tool_creation_audit(args: dict, *, admission=None, **effective) -> dict | None:
    """Audit the same normalized fields placement judged, including inherited identity/project. [daveey]"""
    from hermes_cli.kanban_worker_placement import creation_audit

    audit = creation_audit({**args, **effective})
    if admission and admission.get("hub_admission"):
        audit = {**(audit or {}), "hub_admission": admission["hub_admission"]}
    return audit


def inherit_tool_priority(conn, args, creator_task_id, *, receipt=None, creator_present=True):
    """Resolve omitted worker urgency before placement/courier serialization."""
    from hermes_cli.kanban_fairness_creation import observe_intent

    if not creator_present:
        # Preserve cross-board lineage without inventing a local priority snapshot.
        observe_intent(conn, args, creator_task_id, receipt, effective={})
        return args
    if creator_task_id is None or args.get("priority") is not None:
        observe_intent(conn, args, creator_task_id, receipt)
        return args
    from hermes_cli.kanban_admission_score import effective_priorities

    # Explicit zero wins; this snapshot excludes age/class/unblock bonuses.
    effective = effective_priorities(conn, [creator_task_id])
    observe_intent(conn, args, creator_task_id, receipt, effective=effective)
    return {**args, "priority": effective.get(creator_task_id, 0)}


def record_created(conn, task_id, *, assignee, status, parents, creator_task_id,
                   tenant, workspace_kind, workspace_path, branch_name, project_id,
                   skills, goal_mode, model_override, provider_override, created_by, requires,
                   placement_policy, human_request=None, placement_audit=None, lane=None,
                   creation_receipt=None, board=None):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_consent import snapshot

    source = conn.execute("SELECT created_by, session_id, priority FROM tasks WHERE id=?", (task_id,)).fetchone()
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
        # Why a worker pool can or cannot take it (kanban_worker_placement.creation_audit); absent with no pool.
        **(placement_audit if isinstance(placement_audit, dict) else {}),
        # Final insertion policy, not a later assignment or a caller's diagnostic override.
        # Audit stored origin, independently of creator-card authority and any
        # caller diagnostic override (interactive and spin-out creates included).
        "creator_source": {"author": source[0], "session_id": source[1]},
        "placement_policy": placement_policy,
        "priority": source[2],
        "consent_stops": snapshot(conn, task_id, creator_task_id),
    })
    from hermes_cli.kanban_fairness_creation import stage
    stage(conn, task_id, creator_task_id=creator_task_id, board=board, receipt=creation_receipt)
    from hermes_cli.kanban_lanes import record as record_lane
    record_lane(conn, task_id, lane)
    if isinstance(placement_audit, dict) and placement_audit.get("hub_admission"):
        kb._append_event(conn, task_id, "hub_admission", placement_audit["hub_admission"])
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


def validate_create_assignment(conn, assignee, *, runs_on, requires, workspace_kind, workspace_path, skills,
                               validate_skills, require_declaration, model, provider, project_id=None,
                               project_repo=None):
    """``create_task``'s assignee checks: pinned skills, required tools (here and where ``runs_on`` lands
    it), the model pin and its provider binding. Returns ``(required, model, provider)`` or raises.

    A card whose ``runs_on`` rules this machine out runs with the landing machine's profile of that name:
    this machine's profiles (missing, or a same-named one with other tools, skills or registry) prove
    nothing. :func:`~hermes_cli.kanban_runs_on_capabilities.check_create` reads the landing machines'
    published tools (``claim_refusal`` re-checks them at pickup); pinned skills load or get the
    missing-skill note in the landing worker (``missing_pin_note``); the model registry is checked by the
    landing dispatcher (``route_model_mismatch``). A concrete pin must name its provider, since only the
    landing machine's credentials could bind it. [daveey]

    ``host_repo`` comes only from an attestation of the active trusted capsule create scope
    (:func:`~hermes_cli.kanban_capsule_admission.attest_create`); outside one it is ``None``. [daveey]
    """
    from hermes_cli.kanban_capsule_admission import attest_create
    from hermes_cli.kanban_capabilities import validate_assignment
    from hermes_cli.kanban_model_assignment import validate_model_assignment
    from hermes_cli.kanban_pin_provider import require_remote_pin_provider, resolve_pin_provider
    from hermes_cli.kanban_runs_on_capabilities import check_create, runs_elsewhere
    from hermes_cli.kanban_skill_check import check_task_skills
    from hermes_cli.kanban_gate_only import refuse_create

    refuse_create(assignee)  # kanban.gate_only_assignees: only the gate assigns them, by reassignment [daveey]
    remote = runs_elsewhere(conn, runs_on)
    check_task_skills(skills, assignee, enabled=validate_skills and not remote)
    required = validate_assignment(assignee, requires=requires, workspace_kind=workspace_kind,
                                   workspace_path=workspace_path, skills=skills,
                                   require_declaration=require_declaration, check_profile=not remote,
                                   host_repo_attestation=attest_create(
                                       conn, assignee=assignee, project_id=project_id, project_repo=project_repo,
                                       workspace_kind=workspace_kind, workspace_path=workspace_path,
                                       runs_on=runs_on))
    check_create(conn, runs_on, assignee=assignee, requires=required, workspace_kind=workspace_kind)
    validate_model_assignment(assignee, model, provider, local_profile=not remote)
    if remote:
        require_remote_pin_provider(model, provider)
        return required, model, provider
    # Bind a provider-less concrete pin now (or refuse), never late-bind it to config at spawn.
    model, provider = resolve_pin_provider(model, provider, assignee=assignee)
    return required, model, provider


def run_insert_guard(conn, guard) -> None:
    """Run ``create_task(insert_guard=...)`` inside the INSERT's write transaction, before the row exists. The
    guard raises to roll the create back: ``kanban_overlap.AfterCreate.guard`` re-runs the overlap match under the
    board's write lock, so a concurrent create that passed the same first check is refused. [daveey]"""
    if guard is not None:
        guard(conn)


def project_branch_name(project_obj, task_id: str, title) -> "str | None":
    """A project worktree card's deterministic branch (moved from ``kanban_db``)."""
    from hermes_cli import projects_db as _pdb

    try:
        return _pdb.branch_name_for(project_obj, task_id, title=title or "")
    except Exception:  # health: allow BLE001 -- moved unchanged from kanban_db; no branch = worker wt/<id> fallback
        return None
