"""Moves onto one machine: the pool is the default, a single machine needs a host reason. [daveey]

Pinning a card to one worker for capacity froze the choice when capacity changed: on 11-12 ten ready cards waited
up to 261 min on ``machine:worker-2`` while it was drain-held and worker-1 and worker-4 had free slots. The queue
for any worker is ``runs_on: [pool:worker]`` (:data:`hermes_cli.kanban_placement.POOL_LABEL`, ``hermes kanban move
<id> --pool``). A move whose ``runs_on`` names any ``machine:<x>`` is refused without ``host_reason`` naming state on
x (a worktree, a stamp, unpublished work, a live process, Mac-only or hub-only work): a capacity stall is a
condition of the machine the card leaves, not state on x, and a new single-machine pin only moves the stall (11-13:
three pickup-stall alarm moves pinned published work to machine:worker-1/-4/-8; two stalled again there). A
``host_reason`` that only cites capacity is refused too (:func:`capacity_only`). The worker-first origin repin and
the stranded-label reroute (:mod:`hermes_cli.kanban_creator_repin`) carry their own target state. A pooled card
re-pinned by hand to the machine it is on must also hold unpushed state there when a tree of the card was found: a
clean tree whose commits are on a remote stays on the pool. The park itself pins on unpushed state
(:mod:`hermes_cli.kanban_park_pin`).
"""
from __future__ import annotations

import fnmatch
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

HOST_REASON_REQUIRED = "host_reason_required"
HOST_REASON_CAPACITY = "host_reason_capacity"
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


def cli_labels(args: Any) -> tuple[Optional[list[str]], dict[str, Any], str]:
    """``hermes kanban move`` target: (labels, {host_reason}, \"\") from exactly one of ``--pool`` / ``--runs-on``,
    else (None, {}, error)."""
    pool, runs_on = bool(getattr(args, "pool", False)), getattr(args, "runs_on", None)
    if pool == bool(runs_on):
        return None, {}, "kanban move: pass exactly one of --pool or --runs-on [invalid_runs_on]"
    labels = [pool_label()] if pool else [x.strip() for x in str(runs_on).split(",") if x.strip()]
    return labels, {"host_reason": getattr(args, "host_reason", None)}, ""


def machine_pins(labels: Iterable[str]) -> list[str]:
    """Machines named by ``machine:<x>`` labels."""
    return [lab.split(":", 1)[1] for lab in labels if isinstance(lab, str) and lab.startswith("machine:")]


def pool_pins(labels: list[str], machines: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """Machines named by ``machine:<x>`` labels that are pool workers (carry the pool label)."""
    label = pool_label()
    return [n for n in machine_pins(labels) if label in ((machines.get(n) or {}).get("labels") or [])]


# A host reason names state on the target; one that only cites capacity (free slots, 10/10, eligible now) does not.
_CAPACITY = re.compile(r"free (?:card )?slots?|slots? free|\b\d+\s*/\s*\d+\b|capacity|budget_exhausted|"
                       r"less loaded|eligible now|has room|\bidle\b|\bunheld\b|\bfull\b", re.IGNORECASE)
_STATE = re.compile(r"worktree|\btrees?\b|stamp|unpushed|unpublished|uncommitted|dirty|\bpids?\b|process|"
                    r"\blogs?\b|artifact|evidence|\bfiles?\b|checkout|workspace|fixture|\bmac\b|macos|"
                    r"xcode|\bios\b|simulator|\bgpus?\b|hub-only|hub only|keychain|chrome|desktop|device|"
                    r"database|signed-in|/home/|~/|\.json|\.log", re.IGNORECASE)


def capacity_only(host_reason: str) -> bool:
    """Whether ``host_reason`` cites capacity and names no state on a host (the refused alarm-move shape)."""
    return bool(_CAPACITY.search(host_reason or "")) and not _STATE.search(host_reason or "")


def host_reason_message(name: str, *, pooled: bool) -> str:
    """The ``host_reason_required`` refusal for a move to ``machine:<name>``: queue on the pool instead."""
    label = pool_label()
    what = f"machine:{name} is a pool worker ({label})" if pooled else f"runs_on names one machine (machine:{name})"
    return (f"{what}. Queue the card for any worker instead (runs_on [{label!r}], `hermes kanban move <id> --pool`): "
            "it waits in the single queue until any worker has a free slot. A capacity stall is a condition of the "
            f"machine the card leaves, not state on {name}; a new single-machine pin only moves the stall. Pin one "
            f"machine only with host_reason naming state on {name}: a worktree, a stamp, unpublished work, a live "
            "process, or Mac-only or hub-only work.")


def _is_tree(path: Path) -> bool:
    return (path / ".git").exists()


def _trees_home() -> Path:
    """Root of the card worktree globs (a seam: tests point it away from the developer's real ``~/code/_wt``)."""
    return Path.home()


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


def _per_card(path: Path) -> bool:
    """Whether ``path`` sits where per-card trees live (one of :data:`_WORKTREE_GLOBS` under the trees home)."""
    try:
        rel = path.resolve().relative_to(_trees_home().resolve())
    except (OSError, ValueError):
        return False
    return any(len(rel.parts) == len(Path(g).parts) and fnmatch.fnmatchcase(rel.as_posix(), g)
               for g in _WORKTREE_GLOBS)


def card_trees(task_id: Optional[str], path: Optional[str], text: Optional[str] = "",
               extra: Iterable[str] = (), *, linked_text_only: bool = False) -> list[str]:
    """Git trees on this host that hold the card's work: the workspace and its direct children, linked worktrees
    whose worktree-owner marker names the card, trees the park text names (``~/code/_wt/<slug>``) unless their
    owner marker names another card, and ``extra``.
    ``linked_text_only`` keeps, among the text-named trees and ``extra``, only linked worktrees (a ``.git`` file) and
    clones at a per-card location (``~/code/_wt/<slug>``): a shared main checkout a card body merely mentions
    (``~/code/babybear``, the gateway's live ``~/.hermes/hermes-agent``) is not the card's work."""
    found: list[Path] = []
    root = Path(path).expanduser() if path else None
    if root is not None and root.is_dir():
        found += [root] if _is_tree(root) else []
        try:
            found += sorted(p for p in root.iterdir() if p.is_dir() and _is_tree(p))[:_MAX_TREES]
        except OSError:
            pass
    if task_id:
        home = _trees_home()
        for pattern in _WORKTREE_GLOBS:
            found += [p for p in sorted(home.glob(pattern)) if p.is_dir() and _owner(p) == task_id]
    named = [(raw, True) for raw in _PATH.findall(text or "")] + [(raw, False) for raw in extra]
    for raw, from_text in named:
        p = Path(raw.rstrip(".,;:)]'\"")).expanduser()
        if not (p.is_dir() and _is_tree(p)) or (linked_text_only and (p / ".git").is_dir() and not _per_card(p)):
            continue
        # A tree the text only names but whose owner marker names another card is that card's work (a reviewer
        # card naming the implementer's ~/code/_wt/<slug>): its state is not this card's to carry.
        if from_text and task_id and (owner := _owner(p)) and owner != task_id:
            continue
        found.append(p)
    out: list[str] = []
    for p in found:
        key = str(p.resolve())
        if key not in out:
            out.append(key)
    return out[:_MAX_TREES]


_REMOTE_TIMEOUT = 10
_MAX_ANCESTRY_CHECKS = 50
_MAX_REFRESHES = 5


def pushed_on_remote(tree: str, *, budget: Optional[Callable[[float], float]] = None,
                     refresh: bool = True) -> bool:
    """Whether a tree the local refs call unpushed has HEAD on a remote branch after all. Fails toward False.

    ``git push https://github.com/<o>/<r>.git HEAD:wt/x`` (a URL, not the remote's name: the HTTPS-credential push
    workers use) leaves ``refs/remotes/origin/wt/x`` at the old tip, and a server-side ``update-branch`` moves the
    branch past HEAD again. The local check then reports pushed work as unpushed, and the park pinned a card to
    its worker (11-12: t_01a1d0a1eaa00371 sat ready on ``machine:worker-2`` while worker-2 was on a Board hold).
    So ask the remote: HEAD is a remote branch tip, or a stale tracking ref HEAD builds on, once refreshed from
    the remote, contains HEAD. Every git call draws its timeout from ``budget`` (:mod:`hermes_cli.git_budget`); a
    spent budget raises ``GitUnchecked`` (no verdict) rather than answering False ("unpushed"). ``refresh=False``
    (a create's bounded probe) asks only whether HEAD is a remote branch tip: no ancestry checks, no fetch."""
    from functools import partial
    from hermes_cli.git_budget import GitUnchecked, ensure_left, timeout_for
    from hermes_cli.worktree_ops import _git_out, _worktree_has_unpushed_commits
    # Keep credential helpers for private remotes, but never consume the caller's stdin.
    git_out = partial(_git_out, stdin=subprocess.DEVNULL)

    def t(cap: float) -> float:
        return timeout_for(budget, cap)
    try:
        head = git_out(["rev-parse", "HEAD"], tree, timeout=t(10))
        remotes = (git_out(["remote"], tree, timeout=t(10)) or "").split()
        if not head or not remotes:
            return False
        branch = git_out(["symbolic-ref", "-q", "HEAD"], tree, timeout=t(10))
        preferred: set[tuple[str, str]] = set()
        if branch:
            destinations = git_out(["for-each-ref", "--format=%(upstream:remotename) %(upstream:remoteref)"
                                    "%0a%(push:remotename) %(push:remoteref)", branch], tree,
                                   timeout=t(10)) or ""
            preferred = {(remote, ref) for line in destinations.splitlines() if " " in line
                         for remote, ref in [line.split(" ", 1)]}
        stale: list[tuple[str, str, str]] = []
        fallback: list[tuple[str, str, str]] = []
        checks = 0
        for remote in remotes:
            listing = git_out(["ls-remote", "--heads", remote], tree, timeout=t(_REMOTE_TIMEOUT))
            if not listing:
                continue
            if not refresh:
                if any(line.partition("\t")[0] == head for line in listing.splitlines()):
                    return True
                continue
            tracked = dict(line.split(" ", 1)[::-1] for line in (git_out(
                ["for-each-ref", "--format=%(objectname) %(refname)", f"refs/remotes/{remote}"], tree, timeout=t(10))
                or "").splitlines() if " " in line)
            for line in listing.splitlines():
                sha, _, ref = line.partition("\t")
                if sha == head:
                    return True
                tracking = f"refs/remotes/{remote}/{ref[len('refs/heads/'):]}"
                local = tracked.get(tracking)
                if local == sha:
                    continue
                candidate = (remote, ref, tracking)
                if local and checks < _MAX_ANCESTRY_CHECKS:
                    checks += 1
                    if git_out(["merge-base", "--is-ancestor", local, "HEAD"], tree, timeout=t(10)) is not None:
                        stale.append(candidate)
                        continue
                # New URL-pushed branches have no tracking ref; rebased URL pushes
                # can leave a tracking tip unrelated to HEAD. Both need a refresh.
                fallback.append(candidate)
        # Spend the bounded refresh budget on this branch's likely destinations first,
        # not the first five alphabetic heads of a large remote.
        fetches = sorted(stale + fallback, key=lambda c: (
            (c[0], c[1]) not in preferred, c[1] != branch,
        ))[:_MAX_REFRESHES]
        for remote, ref, tracking in fetches:
            git_out(["fetch", "--no-tags", "-q", remote, f"+{ref}:{tracking}"], tree, timeout=t(_REMOTE_TIMEOUT))
        return bool(fetches) and not _worktree_has_unpushed_commits(tree, timeout=10, budget=budget)
    except GitUnchecked:
        raise
    except subprocess.TimeoutExpired:
        ensure_left(budget)  # cut short by the deadline: no verdict; else an unreachable remote proves nothing
        return False
    except (OSError, subprocess.SubprocessError, ValueError):
        return False


_DIRTY, _UNPUSHED, _UNCHECKED = "dirty worktree ", "unpushed commits in ", "unchecked worktree "


def state_parts(state: str) -> tuple[str, str]:
    """``(check, tree)`` of a :func:`tree_state` string: ``("dirty", <p>)``, ``("unpushed", <p>)`` or
    ``("unchecked", <p>)`` (its `` (<cause>)`` suffix dropped)."""
    for check, prefix in (("dirty", _DIRTY), ("unpushed", _UNPUSHED), ("unchecked", _UNCHECKED)):
        if state.startswith(prefix):
            rest = state[len(prefix):]
            return check, (rest.rsplit(" (", 1)[0] if check == "unchecked" and rest.endswith(")") else rest)
    return "", state


# Git time for one card's trees. The operator call's scan and the pickup-stall alarm run the move guard inline, and
# 20+ trees at 10-20 s per git call took minutes; past the budget the rest count as unchecked (fail safe: state).
# Every git call of the scan (the hardening probes and the hidden-edit scan of the dirty check, every call of the
# unpushed check and the remote refresh) draws its timeout from this one budget (:mod:`hermes_cli.git_budget`).
STATE_BUDGET_S = 20.0


def _clock() -> float:
    """Monotonic seconds (a seam: tests advance a fake clock per git call)."""
    return time.monotonic()


def _commit_less(tree: str, budget: Callable[[float], float]) -> bool:
    """Whether a clean ``tree`` has no commits at all: HEAD is unborn and the repo has no ref. A scratch fixture
    (``git init``, nothing committed) then holds nothing another worker would miss, though the unpushed check's
    fail-safe reads its unborn HEAD as unpushed (11-13: it pinned t_01a1d5e41b24e463 to worker-6). Any other
    answer, a git error or a ref listing with any stderr (a skipped broken ref) included, is False: the unpushed
    check and its fail-safe decide. Timeouts come from
    ``budget``, whose end raises ``GitUnchecked``."""
    from hermes_cli.git_budget import timeout_for
    from hermes_cli.worktree_ops import _git
    try:
        head = _git(["rev-parse", "--verify", "-q", "HEAD"], tree, timeout=timeout_for(budget, 10),
                    stdin=subprocess.DEVNULL)
        if head.returncode != 1:
            return False
        refs = _git(["for-each-ref", "--count=1"], tree, timeout=timeout_for(budget, 10), stdin=subprocess.DEVNULL)
        # A broken ref is skipped with only a stderr warning (exit 0, no stdout): that is no proof of an empty repo.
        return refs.returncode == 0 and not refs.stdout.strip() and not refs.stderr.strip()
    except (OSError, subprocess.SubprocessError, ValueError):
        return False


def tree_state(trees: Iterable[str], *, budget: float = STATE_BUDGET_S,
               refresh: bool = True) -> Optional[str]:
    """``dirty worktree <p>`` / ``unpushed commits in <p>`` for the first tree with state, else None. Git runs at
    most ``budget`` seconds in all (per-call timeouts shrink to the time left); a tree it could not check (budget
    spent, git timed out) reads ``unchecked worktree <p> (<cause>)``, which callers treat as state (fail safe).
    ``refresh`` goes to :func:`pushed_on_remote` (a create probes with ``refresh=False`` and a short ``budget``).
    A clean tree with no commits at all (:func:`_commit_less`) has no state."""
    from hermes_cli.git_budget import BUDGET_SPENT, GitUnchecked, deadline_budget
    from hermes_cli.worktree_ops import _worktree_has_unpushed_commits, _worktree_is_dirty
    timeouts = deadline_budget(budget, clock=lambda: _clock())
    for tree in trees:
        if not Path(tree).is_dir():
            continue
        try:
            if _worktree_is_dirty(tree, tree, timeout=10, budget=timeouts):
                from hermes_cli.kanban_checkout_deletions import published_checkout_deletions
                if published_checkout_deletions(tree, timeouts):
                    continue
                return f"{_DIRTY}{tree}"
            if _commit_less(tree, timeouts):
                continue
            if (_worktree_has_unpushed_commits(tree, timeout=10, budget=timeouts)
                    and not pushed_on_remote(tree, budget=timeouts, refresh=refresh)):
                return f"{_UNPUSHED}{tree}"
        except GitUnchecked as exc:
            cause = f"git checks of the card's trees took over {budget:g}s" if str(exc) == BUDGET_SPENT else str(exc)
            return f"{_UNCHECKED}{tree} ({cause})"
    return None


def card_trees_state(task_id: Optional[str], path: Optional[str], text: Optional[str] = "",
                     extra: Iterable[str] = (), *, linked_text_only: bool = False) -> tuple[list[str], Optional[str]]:
    """``(trees found, unpushed state or None)`` for the card on this host."""
    trees = card_trees(task_id, path, text, extra, linked_text_only=linked_text_only)
    return trees, tree_state(trees)


def check(labels: list[str], machines: Mapping[str, Mapping[str, Any]], *, me: str, running: bool,
          previous: Optional[list[str]], workspace_path: Optional[str], host_reason: Optional[str],
          force: bool, refused: type, task_id: Optional[str] = None) -> dict[str, Any]:
    """Event fields for a move (``host_reason``, ``unpushed_state``), or raise ``refused(code, message)``."""
    named = machine_pins(labels)
    pins = pool_pins(labels, machines)
    reason = (host_reason or "").strip()
    if not named:
        return {"host_reason": reason[:500]} if reason else {}
    label = pool_label()
    if not reason:
        raise refused(HOST_REASON_REQUIRED, host_reason_message((pins or named)[0], pooled=bool(pins)))
    if capacity_only(reason):
        raise refused(HOST_REASON_CAPACITY, (
            f"host_reason {reason[:200]!r} cites capacity, not state on {named[0]}. Capacity is never a host reason: "
            f"use runs_on [{label!r}] (`hermes kanban move <id> --pool`), or name the worktree, stamp, unpublished "
            "work, live process, or Mac-only or hub-only work that holds the card there."))
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
