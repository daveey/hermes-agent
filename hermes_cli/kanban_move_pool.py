"""Moves onto one worker: the pool is the default, a single worker needs a host reason. [daveey]

Pinning a card to one worker for capacity froze the choice when capacity changed: on 11-12 ten ready cards waited
up to 261 min on ``machine:worker-2`` while it was drain-held and worker-1 and worker-4 had free slots. The queue
for any worker is ``runs_on: [pool:worker]`` (:data:`hermes_cli.kanban_placement.POOL_LABEL`, ``hermes kanban move
<id> --pool``). A move to ``machine:<x>`` where x carries the pool label is refused without ``host_reason`` (the
card works on that host, or holds unpushed state there). A pooled card re-pinned by hand to the machine it is on
must also hold unpushed state there when a tree of the card was found: a clean tree whose commits are on a remote
stays on the pool. The park itself pins on unpushed state (:mod:`hermes_cli.kanban_park_pin`).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

HOST_REASON_REQUIRED = "host_reason_required"
POOL_KEEPS_PUSHED = "pool_keeps_pushed"

_PATH = re.compile(r"(?<![\w.:/-])~?(?:/[\w.@+-]+){2,}")
# Where linked worktrees live (skill hermes-mods-repo, Git worktrees): a card's tree is often ``~/code/_wt/<slug>``,
# not inside its scratch workspace.
_WORKTREE_GLOBS = ("code/_wt/*", "code/_wt-remote/*", "code/*/.worktrees/*", ".hermes/hermes-agent/.worktrees/*")
OWNER_MARKER = "hermes-owner.json"  # the worktree-owner plugin's marker in a linked worktree's gitdir
_MAX_TREES = 20


def pool_label() -> str:
    from hermes_cli.kanban_placement import POOL_LABEL
    return POOL_LABEL


def pool_pins(labels: list[str], machines: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """Machines named by ``machine:<x>`` labels that are pool workers (carry the pool label)."""
    label = pool_label()
    names = [lab.split(":", 1)[1] for lab in labels if lab.startswith("machine:")]
    return [n for n in names if label in ((machines.get(n) or {}).get("labels") or [])]


def _is_tree(path: Path) -> bool:
    return (path / ".git").exists()


def _owner(tree: Path) -> Optional[str]:
    """``task_id`` from the worktree-owner marker in a linked worktree's gitdir, else None."""
    try:
        head = (tree / ".git").read_text(encoding="utf-8-sig", errors="replace").strip()
    except OSError:  # a directory .git (a main checkout) carries no owner marker
        return None
    if not head.startswith("gitdir:"):
        return None
    try:
        data = json.loads((Path(head.split(":", 1)[1].strip()) / OWNER_MARKER).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data.get("task_id") if isinstance(data, dict) else None


def card_trees(task_id: Optional[str], path: Optional[str], text: Optional[str] = "",
               extra: Iterable[str] = ()) -> list[str]:
    """Git trees on this host that hold the card's work: the workspace and its direct children, linked worktrees
    whose worktree-owner marker names the card, trees the park text names (``~/code/_wt/<slug>``), and ``extra``."""
    found: list[Path] = []
    root = Path(path).expanduser() if path else None
    if root is not None and root.is_dir():
        found += [root] if _is_tree(root) else []
        try:
            found += sorted(p for p in root.iterdir() if p.is_dir() and _is_tree(p))[:_MAX_TREES]
        except OSError:
            pass
    if task_id:
        home = Path.home()
        for pattern in _WORKTREE_GLOBS:
            found += [p for p in sorted(home.glob(pattern)) if p.is_dir() and _owner(p) == task_id]
    for raw in [*_PATH.findall(text or ""), *extra]:
        p = Path(raw.rstrip(".,;:)]'\"")).expanduser()
        if p.is_dir() and _is_tree(p):
            found.append(p)
    out: list[str] = []
    for p in found:
        key = str(p.resolve())
        if key not in out:
            out.append(key)
    return out[:_MAX_TREES]


def tree_state(trees: Iterable[str]) -> Optional[str]:
    """``dirty worktree <p>`` / ``unpushed commits in <p>`` for the first tree with state, else None."""
    from hermes_cli.worktree_ops import _worktree_has_unpushed_commits, _worktree_is_dirty
    for tree in trees:
        if not Path(tree).is_dir():
            continue
        if _worktree_is_dirty(tree, tree):
            return f"dirty worktree {tree}"
        if _worktree_has_unpushed_commits(tree):
            return f"unpushed commits in {tree}"
    return None


def card_trees_state(task_id: Optional[str], path: Optional[str], text: Optional[str] = "",
                     extra: Iterable[str] = ()) -> tuple[list[str], Optional[str]]:
    """``(trees found, unpushed state or None)`` for the card on this host."""
    trees = card_trees(task_id, path, text, extra)
    return trees, tree_state(trees)


def check(labels: list[str], machines: Mapping[str, Mapping[str, Any]], *, me: str, running: bool,
          previous: Optional[list[str]], workspace_path: Optional[str], host_reason: Optional[str],
          force: bool, refused: type, task_id: Optional[str] = None) -> dict[str, Any]:
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
        trees, unpushed = card_trees_state(task_id, workspace_path)
        # Refuse only on evidence: with no tree of the card found here, the host reason stands.
        if trees and unpushed is None and not force:
            raise refused(POOL_KEEPS_PUSHED, (
                f"the card is on the pool ({label}) and its trees on {me} ({', '.join(trees)}) are clean with their "
                "commits on a remote, so any worker can resume it: it stays on the pool. Only unpushed state pins it "
                "here (pass force if the card works on this host)."))
        if unpushed:
            out["unpushed_state"] = unpushed
    return out
