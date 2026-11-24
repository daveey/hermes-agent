"""Git working-tree probing for the gateway: run git, resolve repo roots, fold linked worktrees.
Probing runs where the gateway runs (covers remote backends). Roots go through a thread-safe
single-flight cache so concurrent identical probes share one ``git`` spawn: positives live for the
process, negatives (not a repo / deleted dir) for ``_NEG_TTL`` — hundreds of non-git session cwds
would otherwise re-spawn ``git`` on every sidebar open, while the TTL keeps ``git init`` re-probable."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor

from hermes_cli._subprocess_compat import bounded_git_probe

_GIT_TIMEOUT = 1.5
_WARM_WORKERS = 8
# "not a git repo" TTL. A negative is also dropped as soon as the cwd's own mtime moves (``git init``
# creates ``.git`` there), so the TTL only bounds rarer cases such as a parent dir becoming a repo.
# At 30 s, every sidebar/project poll re-spawned ``git`` for each of hundreds of non-repo session
# cwds (kanban scratch dirs) twice a minute, a top idle cost of Desktop backends.
_NEG_TTL = 300.0


def run_git(cwd: str, *args: str) -> str:
    """``git -C <cwd> <args>`` → stripped stdout, or ``""`` on any failure. ``bounded_git_probe``
    bounds post-kill cleanup on Windows (a killed git's suspended descendant held the pipes).

    Uses the shared :func:`bounded_git_probe` so the post-kill cleanup is bounded on Windows — a plain
    ``subprocess.run(timeout=...)`` here deadlocked Desktop session readiness when a killed git left a
    suspended descendant holding the pipe handles (issue #68609).
    """
    # A missing dir can only fail at the price of a fork; deleted worktrees dominate a long
    # session history's cwds, so the stat pays off.
    if not cwd or not os.path.isdir(cwd):
        return ""
    return bounded_git_probe(["git", "-C", cwd, *args], timeout=_GIT_TIMEOUT)


def branch(cwd: str) -> str:
    return run_git(cwd, "branch", "--show-current") or run_git(cwd, "rev-parse", "--short", "HEAD")


class _RootCache:
    """Thread-safe, single-flight cache of git-root probes: positives live for
    the process, negatives for ``_NEG_TTL``; followers wait on the leader."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._roots: dict[str, str] = {}
        self._neg: dict[str, tuple[float, object]] = {}  # key -> (monotonic expiry, dir stamp)
        self._inflight: dict[str, threading.Event] = {}

    def invalidate(self) -> None:
        with self._lock:
            self._roots.clear()
            self._neg.clear()
            self._inflight.clear()

    def settled(self, key: str, cwd: str = "") -> str | None:
        """The answer :meth:`resolve` would return without probing (``""`` = a negative that still
        holds), or ``None`` when it would have to probe or wait."""
        with self._lock:
            if hit := self._roots.get(key):
                return hit
            neg = self._neg.get(key)
        if neg is not None and neg[0] > time.monotonic() and neg[1] == _dir_stamp(cwd):
            return ""
        return None

    def resolve(self, key: str, probe, cwd: str = "") -> str:
        while True:
            with self._lock:
                if hit := self._roots.get(key):
                    return hit
                neg = self._neg.get(key)
            if neg is not None:
                stamp = _dir_stamp(cwd)
                with self._lock:
                    if neg[0] > time.monotonic() and neg[1] == stamp:
                        return ""  # recent "not a repo" and the dir has not changed: trust it
                    self._neg.pop(key, None)  # TTL elapsed or dir moved: re-probe (may be a repo now)
            with self._lock:
                if hit := self._roots.get(key):
                    return hit
                gate = self._inflight.get(key)
                leader = gate is None
                if leader:
                    gate = self._inflight[key] = threading.Event()
            if not leader:  # another thread is probing this key — wait, then re-read
                gate.wait(timeout=_GIT_TIMEOUT + 0.5)
                continue
            value = ""
            stamp = _dir_stamp(cwd)
            try:
                value = probe()
            finally:
                with self._lock:
                    if value:
                        self._roots[key] = value
                    else:
                        self._neg[key] = (time.monotonic() + _NEG_TTL, stamp)
                    self._inflight.pop(key, None)
                gate.set()
            return value


def _dir_stamp(cwd: str) -> object:
    """``cwd``'s mtime (``None`` when missing): ``git init`` there moves it."""
    if not cwd:
        return None
    try:
        return os.stat(cwd).st_mtime_ns
    except OSError:
        return None


_cache = _RootCache()


def invalidate() -> None:
    """Drop cached roots after a known mutation (e.g. a worktree was added)."""
    _cache.invalidate()


def repo_root(cwd: str) -> str:
    """Top-level git repo root for ``cwd`` (``""`` when not a repo)."""
    return _cache.resolve(cwd, lambda: run_git(cwd, "rev-parse", "--show-toplevel"), cwd) if cwd else ""


def common_repo_root(cwd: str) -> str:
    """The MAIN (common) repo root for ``cwd``, folding linked worktrees: ``--show-toplevel`` is a
    linked worktree's OWN root; the parent of the shared ``--git-common-dir`` is the one true root
    (fallback: toplevel). Normalized to git's forward-slash spelling so it compares equal to
    :func:`repo_root` (native ``\\`` on Windows made the main checkout look like a worktree)."""
    # Checking the (warmed, negative-cached) toplevel first spares every non-repo cwd a second
    # `git` spawn the parallel warm can't absorb.
    if not cwd or not repo_root(cwd):
        return ""

    def _probe() -> str:
        gitdir = run_git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
        if gitdir:
            gitdir = os.path.realpath(gitdir)
            if os.path.basename(gitdir) == ".git":
                return os.path.dirname(gitdir).replace(os.sep, "/")
        return repo_root(cwd)

    return _cache.resolve(f"common:{cwd}", _probe, cwd)


def resolve(cwd: str) -> dict | None:
    """Inject-able resolver for ``project_tree.build_tree``: ``{repo_root: <common root>,
    worktree_root: <this checkout>}`` or None outside a repo (equal roots = main checkout)."""
    worktree_root = repo_root(cwd)
    if not worktree_root:
        return None
    return {"repo_root": common_repo_root(cwd) or worktree_root, "worktree_root": worktree_root}


def _settled(cwd: str) -> bool:
    """True when :func:`resolve` would answer ``cwd`` from the cache alone."""
    root = _cache.settled(cwd, cwd)
    if root is None:
        return False
    return root == "" or _cache.settled(f"common:{cwd}", cwd) is not None


def warm_roots(cwds: Iterable[str], max_workers: int = _WARM_WORKERS) -> None:
    """Pre-resolve many cwds' roots in parallel (bounded) so a cold first paint
    doesn't serialize one git spawn per session cwd; results land in the cache."""
    # A cwd whose answers are already cached needs no warm-up. A sidebar poll re-offers every session
    # cwd; fanning thousands of cache hits through a fresh pool was most of an idle tree build.
    pending = sorted(cwd for cwd in {(cwd or "").strip() for cwd in cwds} - {""} if not _settled(cwd))
    if len(pending) == 1:
        resolve(pending[0])
    elif pending:
        with ThreadPoolExecutor(max_workers=min(max_workers, len(pending))) as pool:
            list(pool.map(resolve, pending))
