"""Pure, bounded lane matching of structured card provenance. [daveey]"""
from __future__ import annotations

import json
from pathlib import PurePosixPath


def skill_names(raw) -> set[str]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return set()
    return {s for s in raw if isinstance(s, str)} if isinstance(raw, (list, tuple)) else set()


def repo_names(opts: dict) -> set[str]:
    """Exact repo basename, or the repo immediately before a .worktrees directory.

    Never scan arbitrary ancestors: /src/hermes/ask-eve is Ask Eve, not Hermes.
    No filesystem/Git/network access is needed during a creation transaction.
    """
    names = set()
    for value in (opts.get("project_repo"), opts.get("workspace_path")):
        if not isinstance(value, str) or not value:
            continue
        parts = PurePosixPath(value.rstrip("/")).parts
        if not parts:
            continue
        names.add(parts[-1].removesuffix(".git"))
        for index, part in enumerate(parts):
            if part == ".worktrees" and index:
                names.add(parts[index - 1])
    return names
