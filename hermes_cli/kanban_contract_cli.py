"""Completion-contract CLI/tool helpers: the audited orchestrator re-point
(``hermes kanban edit --completion-contract``), the creation-time warning for a PR
contract that can never pass CI acceptance, and naming a PR-acceptance refusal on
``hermes kanban complete``. The worker's own way out is ``metadata.contract_waiver``
(kanban_pr_acceptance_store). [daveey]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import Optional
from urllib.parse import quote

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_operator as kbo
from hermes_cli import kanban_pr_acceptance as _kpa


def contract_warning(contract: Optional[str], assignee: Optional[str] = None) -> Optional[str]:
    """Creation-time advisory: an OWNER/REPO (or PR URL) contract whose repository has no required
    checks on its default branch can only complete through a contract_waiver. Any read error, an
    unreadable repo or configured checks return None: this never refuses or delays a create beyond
    the gh read itself. [daveey]"""
    if not contract or contract == "local-only":
        return None
    match = _kpa._REPO.fullmatch(contract) or _kpa._PR.fullmatch(contract)
    if not match:
        return None
    repo = contract if _kpa._REPO.fullmatch(contract) else match[1]
    owner, name = repo.split("/")
    query = (f'{{repository(owner:{json.dumps(owner)},name:{json.dumps(name)})'
             '{defaultBranchRef{name branchProtectionRule{requiredStatusChecks{context}}}}}')
    try:
        profile_home = _kpa._assignee_profile_home(assignee)
        repository = _kpa._api("graphql", query=query, profile_home=profile_home)["data"]["repository"]
        ref = (repository or {}).get("defaultBranchRef")
        if not ref or not ref.get("name"):
            return None
        if ((ref.get("branchProtectionRule") or {}).get("requiredStatusChecks") or []):
            return None
        branch = ref["name"]
        rules = _kpa._api(f"repos/{repo}/rules/branches/{quote(branch, safe='')}?per_page=100",
                     paginate=True, profile_home=profile_home)
        if any(rule.get("type") == "required_status_checks" for page in rules for rule in page):
            return None
    except (RuntimeError, OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError,
            IndexError, AttributeError):
        return None  # advisory only: gate setup/auth errors are RuntimeError subclasses
    return ("completion_contract " + contract + ": " + _kpa.NO_REQUIRED_CHECKS_DETAIL.format(where=f"{repo}@{branch}", repoint=_kpa._REPOINT_HINT)
            + " Prefer completion_contract='local-only' for this card if no CI applies.")


def create_warning_fields(task, *, skip: bool = False, stderr: bool = False) -> dict:
    """``{"contract_warning": text}`` for a new card whose PR contract's repo has no required
    checks (printed to stderr too when asked), else ``{}``. Never refuses the create."""
    contract = getattr(task, "completion_contract", None) if task is not None and not skip else None
    warning = contract_warning(contract, getattr(task, "assignee", None))
    if warning and stderr:
        print(f"⚠  {warning}", file=sys.stderr)
    return {"contract_warning": warning} if warning else {}


def complete_refusal(conn, task_id: str) -> Optional[str]:
    """The recorded PR-acceptance refusal (with its way out) for a card complete just refused."""
    task = kb.get_task(conn, task_id)
    reason = (getattr(task, "last_failure_error", None) or "") if task and task.status != "done" else ""
    return f"cannot complete {task_id}: {reason}" if reason.startswith("PR acceptance") else None


def cmd_edit_contract(args: argparse.Namespace) -> int:
    """``hermes kanban edit <id> --completion-contract X --reason Y``: the audited way out
    of a completion gate that can never pass. Orchestrator-only, like unblock: a worker
    that could re-point its own card's gate would make the gate meaningless. [daveey]"""
    from hermes_cli.kanban import _OPERATOR_EDIT_FLAGS, _err, _profile_author
    if os.environ.get("HERMES_KANBAN_TASK"):
        return _err(
            "kanban edit: --completion-contract is orchestrator-only; a worker can't change its own "
            "card's gate. Block without a human_request so the owning chat re-points it", 2,
        )
    others = [f"--{name.replace('_', '-')}" for name in (
        "title", "body", "priority", "result", "summary", "metadata", *_OPERATOR_EDIT_FLAGS,
    ) if getattr(args, name, None) is not None]
    if others or getattr(args, "force", False):
        return _err(
            "kanban edit: --completion-contract can't be combined with other edit flags "
            f"({', '.join(others) or '--force'}); run them as separate edits", 2,
        )
    with kbc.connect_closing() as conn:
        try:
            change = kbo.set_completion_contract(
                conn, args.task_id, args.completion_contract,
                reason=getattr(args, "reason", None) or "", actor=_profile_author(),
            )
        except kbo.KanbanEditRefused as exc:
            return _err(f"kanban edit: {exc}", 2)
    if change is None:
        return _err(f"cannot edit {args.task_id} (unknown id)")
    if not change:
        print(f"Edited {args.task_id} (completion_contract unchanged)")
        return 0
    print(f"Edited {args.task_id} (completion_contract: {change['old']!r} -> {change['new']!r})")
    return 0
