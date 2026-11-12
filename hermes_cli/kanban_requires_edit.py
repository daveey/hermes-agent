"""Audited operator correction of declared task capabilities, never a worker escape hatch."""
from __future__ import annotations

import json
import os

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_operator as operator


def set_requires(conn, task_id, requires):
    """Replace declared requirements on idle, open work; retain workspace/skill-implied needs."""
    from hermes_cli.kanban_capabilities import _required_capabilities, skill_requirements
    from hermes_cli.kanban_cli_actor import cli_actor

    if os.environ.get("HERMES_KANBAN_TASK") or os.environ.get("HERMES_KANBAN_WORKER_TASK"):
        raise operator.KanbanEditRefused("--requires is orchestrator-only; workers cannot change task capabilities")
    with kb.write_txn(conn):
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            return None
        row = dict(row)
        if row["status"] in ("done", "archived", "running", "review") or row.get("claim_lock") or row.get("lease_holder"):
            raise operator.KanbanEditRefused("--requires needs an idle, open card without a claim or lease")
        skills = json.loads(row.get("skills") or "[]")
        wanted = _required_capabilities([*requires, *skill_requirements(skills, row.get("assignee"))],
                                        row.get("workspace_kind") or "scratch")
        previous = json.loads(row.get("requires") or "[]")
        if wanted == previous:
            return {}
        changes = {"requires": {"old": previous, "new": wanted}}
        conn.execute("UPDATE tasks SET requires=? WHERE id=?", (json.dumps(wanted), task_id))
        kb._append_event(conn, task_id, "edited", {"fields": ["requires"], "changes": changes, "actor": cli_actor()})
    kb.notify_task_updated(conn, task_id, ["requires"])
    return changes


def edit_preflight(args):
    """Dispatch `kanban edit` flags with their own transaction; None means run the ordinary edit."""
    from hermes_cli import kanban_contract_cli
    from hermes_cli.kanban import _err

    if getattr(args, "admission_class", None) is not None or getattr(args, "admission_frees", None) is not None:
        # [daveey] these flags edit in their own transaction and would silently drop admission metadata
        for flag in ("completion_contract", "requires"):
            if getattr(args, flag, None) is not None:
                return _err(f"kanban edit: admission metadata cannot be combined with --{flag.replace('_', '-')}", 2)
    if getattr(args, "requires", None) is not None:
        return cmd_edit_requires(args)
    if getattr(args, "completion_contract", None) is not None:
        return kanban_contract_cli.cmd_edit_contract(args)
    if getattr(args, "reason", None) is not None and getattr(args, "priority", None) is None \
            and getattr(args, "priority_bump", None) is None:
        return _err("kanban edit: --reason only applies to --completion-contract or --priority", 2)
    return None


def cmd_edit_requires(args):
    """Separate edit transaction: reject mixed flags rather than partially applying an edit."""
    from hermes_cli.kanban import _OPERATOR_EDIT_FLAGS, _err

    other = (*_OPERATOR_EDIT_FLAGS, "title", "body", "priority", "priority_bump", "result", "summary",
             "metadata", "completion_contract", "reason")
    if any(getattr(args, key, None) is not None for key in other) or getattr(args, "force", False):
        return _err("kanban edit: --requires cannot be combined with other edit flags", 2)
    value = args.requires
    requires = [] if value == "" else [part.strip() for part in value.split(",")]
    try:
        with kbc.connect_closing() as conn:
            changes = set_requires(conn, args.task_id, requires)
    except ValueError as exc:
        return _err(f"kanban edit: {exc}", 2)
    if changes is None:
        return _err(f"cannot edit {args.task_id} (unknown id)")
    detail = changes.get("requires")
    message = f"requires: {detail['old']!r} -> {detail['new']!r}" if detail else "requires unchanged"
    print(f"Edited {args.task_id} ({message})")
    return 0
