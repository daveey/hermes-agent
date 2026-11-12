"""Moves onto one worker: the pool is the default, a single worker needs a host reason. [daveey]

Pinning a card to one worker for capacity froze the choice when capacity changed: on 11-12 ten ready cards waited
up to 261 min on ``machine:worker-2`` while it was drain-held and worker-1 and worker-4 had free slots. The queue
for any worker is ``runs_on: [pool:worker]`` (:data:`hermes_cli.kanban_placement.POOL_LABEL`, ``hermes kanban move
<id> --pool``). A move to ``machine:<x>`` where x carries the pool label is refused without ``host_reason`` (the
card works on that host, or holds unpushed state there). A pooled card re-pinned to the machine it is on (a park or
continue) must also hold unpushed state there: a clean workspace whose commits are on a remote stays on the pool.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional

HOST_REASON_REQUIRED = "host_reason_required"
POOL_KEEPS_PUSHED = "pool_keeps_pushed"


def pool_label() -> str:
    from hermes_cli.kanban_placement import POOL_LABEL
    return POOL_LABEL


def pool_pins(labels: list[str], machines: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """Machines named by ``machine:<x>`` labels that are pool workers (carry the pool label)."""
    label = pool_label()
    names = [lab.split(":", 1)[1] for lab in labels if lab.startswith("machine:")]
    return [n for n in names if label in ((machines.get(n) or {}).get("labels") or [])]


def workspace_unpushed(path: Optional[str]) -> Optional[str]:
    """``dirty worktree <p>`` / ``unpushed commits in <p>`` for a git tree in the workspace (itself or a direct
    child), else None (no workspace, no git tree, or every tree clean with its commits on a remote)."""
    from hermes_cli.worktree_ops import _worktree_has_unpushed_commits, _worktree_is_dirty
    if not path:
        return None
    root = Path(path).expanduser()
    if not root.is_dir():
        return None
    trees = [root] if (root / ".git").exists() else []
    try:
        trees += sorted(p for p in root.iterdir() if p.is_dir() and (p / ".git").exists())[:20]
    except OSError:
        return f"unreadable workspace {root}"
    for tree in trees:
        if _worktree_is_dirty(str(tree), str(tree)):
            return f"dirty worktree {tree}"
        if _worktree_has_unpushed_commits(str(tree)):
            return f"unpushed commits in {tree}"
    return None


def check(labels: list[str], machines: Mapping[str, Mapping[str, Any]], *, me: str, running: bool,
          previous: Optional[list[str]], workspace_path: Optional[str], host_reason: Optional[str],
          force: bool, refused: type) -> dict[str, Any]:
    """Event fields for a move (``host_reason``, ``unpushed_state``), or raise ``refused(code, message)``."""
    pins = pool_pins(labels, machines)
    reason = (host_reason or "").strip()
    if not pins:
        return {"host_reason": reason[:500]} if reason else {}
    label = pool_label()
    if not reason:
        raise refused(HOST_REASON_REQUIRED, (
            f"machine:{pins[0]} is a pool worker ({label}). Queue the card for any worker instead (runs_on "
            f"[{label!r}], `hermes kanban move <id> --pool`), so whichever worker has a free slot claims it. Pin one "
            "worker only with host_reason: the card works on that host, or holds unpushed state there."))
    out: dict[str, Any] = {"host_reason": reason[:500]}
    if me in pins and not running and label in (previous or []):
        unpushed = workspace_unpushed(workspace_path)
        if unpushed is None and not force:
            raise refused(POOL_KEEPS_PUSHED, (
                f"the card is on the pool ({label}) and its workspace on {me} is clean with its commits on a "
                "remote, so any worker can resume it: it stays on the pool. Only unpushed state pins it here "
                "(pass force if the card works on this host)."))
        if unpushed:
            out["unpushed_state"] = unpushed
    return out
