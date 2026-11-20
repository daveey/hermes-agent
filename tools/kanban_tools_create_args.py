"""Argument validators and constants for ``kanban_create``. [daveey]

Split out of tools/kanban_tools.py to keep that facade under its size cap.
"""
from __future__ import annotations

from typing import Any, Optional

# Fields read back from a freshly created card into the kanban_create result.
CREATED_FIELDS = ("status", "workspace_kind", "workspace_path", "project_id", "human_request",
                  "admission_class", "admission_frees")


def inherited_project(project_id, workspace_kind, workspace_path, self_task):
    if project_id is None and workspace_kind is None and workspace_path is None:
        if self_task is not None and self_task.project_id:
            return self_task.project_id, self_task.id
    return project_id, None


def effective_tenant(args, origin_intent):
    import os
    return args.get('tenant') if origin_intent is not None else args.get('tenant') or os.environ.get('HERMES_TENANT')


def validate_admission(args: dict) -> None:
    """Reject invalid metadata before a remote create can have side effects."""
    from hermes_cli.kanban_admission_class import AdmissionMetadataError, validate
    from tools.kanban_tools import _check

    try:
        validate(args.get("admission_class", "normal"), args.get("admission_frees"))
    except AdmissionMetadataError as exc:
        _check(False, str(exc), rejection="invalid_args")


def validate_immediate_human_action(conn, args, parents, human_action_now):
    """Do not turn a future dependency into an implied approval or dispatch."""
    from hermes_cli import kanban_db as kb
    from tools.kanban_tools import _check
    if args.get("initial_status") == "blocked" and not human_action_now:
        open_parents = [pid for pid in (parents or [])
                        if (parent := kb.get_task(conn, pid)) and parent.status != "done"]
        _check(not open_parents,
               "initial_status='blocked' with unfinished parents requires an immediate "
               "human action. For 'after X, then Y', keep parents and omit initial_status "
               "so the task waits in todo; the worker must request any required approval "
               "when it becomes due, never assume approval. For an external/time condition, "
               "use kanban_schedule with wake_check, or clock_only plus awaiting={kind:'clock', why:'<why this time>'} for a "
               "pure time wait (a park must name its wake reason). "
               "If a human really can act now despite the unfinished parents, set "
               "human_action_now=true and explain that present action in body. Nothing changed.",
               rejection="invalid_args")


def max_retries_arg(args: dict) -> Optional[int]:
    """``kanban_create``'s ``max_retries``: None when omitted, else an int 1..MAX_RETRY_LIMIT (bools
    refused). One cap for create and the supervisor ``retry_limit`` op. Checked before placement, so
    an invalid value never reaches a remote create."""
    from hermes_cli.kanban_retry_limit_exec import MAX_RETRY_LIMIT
    from tools.kanban_tools import _check  # lazy: kanban_tools imports this module

    value: Any = args.get("max_retries")
    if value is None:
        return None
    _check(isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= MAX_RETRY_LIMIT,
           f"max_retries must be an integer 1-{MAX_RETRY_LIMIT} (got {value!r}); omit it for the "
           "config/default breaker. Nothing changed.", rejection="invalid_args")
    return int(value)
