"""``kanban create`` flag parsers (``--workspace``, ``--branch``), split out of :mod:`hermes_cli.kanban` for its
file-size cap; ``kanban`` re-exports them under their old ``_parse_*_flag`` names."""
from __future__ import annotations

import argparse
import os
from typing import Optional


def parse_workspace_flag(value: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """``--workspace`` -> ``(kind, path|None)``: ``scratch``, ``worktree``, ``worktree:<p>``, ``dir:<p>``.
    Omitted -> ``(None, None)`` so ``create_task`` can tell "default" from an explicit scratch."""
    if not value:
        return (None, None)
    v = value.strip()
    if v in {"scratch", "worktree"}:
        return (v, None)
    for prefix, kind in (("dir:", "dir"), ("worktree:", "worktree")):
        if not v.startswith(prefix):
            continue
        path = v[len(prefix):].strip()
        if not path:
            raise argparse.ArgumentTypeError(f"--workspace {prefix} requires a path after the colon")
        return (kind, os.path.expanduser(path))
    raise argparse.ArgumentTypeError(f"unknown --workspace value {value!r}: use scratch, worktree, "
                                     "worktree:<path>, or dir:<path>")


def parse_branch_flag(value: Optional[str]) -> Optional[str]:
    """Normalize an optional branch name from ``kanban create --branch``."""
    if value is None:
        return None
    branch = value.strip()
    if not branch:
        raise argparse.ArgumentTypeError("--branch requires a non-empty name")
    if branch.startswith("-"):
        raise argparse.ArgumentTypeError("--branch must not start with '-'")
    if any(ch.isspace() for ch in branch):
        raise argparse.ArgumentTypeError("--branch must not contain whitespace")
    return branch
