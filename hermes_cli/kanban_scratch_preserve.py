"""``kanban.scratch_gc_preserve``: task ids whose scratch trees no removal path may touch. [daveey]

An explicit pin for workspaces that must survive every cleanup (research trees with no
card row, hand-kept evidence). Every scratch removal path checks it: completion and deferred
parent cleanup, the retention GC and its archive pass, custody GC and the disk-pressure
adapter (all through ``kanban_workspace_jobs.defer_owned_job_cleanup``), plus custody
receipt discovery, which never records a receipt for a pinned id.

A tree is pinned when its task id is listed or any component of its path is a listed id,
so a nested tree or a detached ``.<id>.gc-*`` sibling is covered too. An unreadable or
malformed setting fails closed: every tree is treated as pinned and a warning is logged.
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

_warned: set[str] = set()


def preserved_ids() -> frozenset[str] | None:
    """The configured ids, empty when unset; None when the setting cannot be trusted."""
    try:
        from hermes_cli.config import load_config_readonly
        kanban = load_config_readonly().get("kanban") or {}
        if not isinstance(kanban, dict):
            _warn(f"invalid kanban section {type(kanban).__name__}")
            return None
        value = kanban.get("scratch_gc_preserve")
    except Exception as exc:  # health: allow BLE001 -- unreadable config pins every tree (fail closed)
        _warn(f"unreadable config ({type(exc).__name__})")
        return None
    if value is None:
        return frozenset()
    if (not isinstance(value, (list, tuple)) or
            not all(isinstance(v, str) and v.strip() and "/" not in v for v in value)):
        _warn(f"invalid value {value!r}: expected a list of task ids")
        return None
    return frozenset(v.strip() for v in value)


def _warn(reason: str) -> None:
    if reason not in _warned:
        _warned.add(reason)
        log.warning("kanban.scratch_gc_preserve: %s; preserving every scratch tree", reason)


def preserved(task_id: str | None, path: Path | str | None = None) -> bool:
    """True when the tree must be kept: a pinned task id, a pinned path component, or a bad setting."""
    ids = preserved_ids()
    if ids is None:
        return True
    if not ids:
        return False
    if task_id in ids:
        return True
    if path is not None:
        for part in Path(path).parts:
            if part in ids or (part.startswith(".") and part[1:].split(".gc-", 1)[0] in ids):
                return True
    return False
