"""Checked history rewrites shared by snapshot limits and store maintenance."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
from typing import Callable


class PruneError(RuntimeError):
    """Pruning stopped without permission to discard another snapshot."""


GC_PENDING_NAME = ".gc-pending"
GC_STALE_AFTER_S = 48 * 3600
GC_STALE_REPORTED: set[str] = set()  # stores already warned about, once per process


# Seconds a maintenance ``git gc`` may run. It only runs off the tool-call path (periodic prune,
# ``hermes checkpoints prune``), so it is sized for a multi-GB loose store on a loaded laptop: the
# old 3x tool timeout (90 s) killed a 14 GB gc in its prune phase every time, so it never shrank.
MAINTENANCE_GC_TIMEOUT = 1800


def store_lock_path(base: Path) -> Path:
    base = base.resolve()
    return base.with_name(f".{base.name}.lock")


@contextmanager
def store_lock(base: Path):
    """Serialize whole operations, including GC and clear, across processes."""
    from hermes_cli.runtime_state import _lock

    base.parent.mkdir(parents=True, exist_ok=True)
    # Outside base so clear_all and legacy migration cannot replace its inode.
    fd = os.open(store_lock_path(base), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if not _lock(fd, wait=False):
            raise PruneError(f"checkpoint store is busy: {base}")
        yield
    finally:
        os.close(fd)


@dataclass(frozen=True)
class Pruner:
    run_git: Callable
    store: Path
    cwd: str
    timeout: int
    dir_size: Callable[[Path], int]
    refs_prefix: str
    # Maintenance callers (never the take path): the gc runs niced with MAINTENANCE_GC_TIMEOUT.
    maintenance: bool = False

    def _git(self, args: list[str], **kwargs) -> str:
        ok, output, error = self.run_git(args, self.store, self.cwd, **kwargs)
        if not ok:
            raise PruneError(f"checkpoint {args[0]} failed: {error}")
        return output

    def reclaim(self) -> None:
        """Full ``git gc`` — tens of seconds on a GB store, so only the periodic prune calls it.
        A ref rewrite that ran without it leaves a marker so the next prune reclaims."""
        self._git(["reflog", "expire", "--expire=now", "--all"])
        if self.maintenance:
            self._git(["gc", "--prune=now", "--quiet"], timeout=MAINTENANCE_GC_TIMEOUT, low_priority=True)
        else:
            self._git(["gc", "--prune=now", "--quiet"], timeout=self.timeout * 3)
        # Bare stores must remain usable when gc packs all refs.
        for name in ("refs/heads", "branches"):
            (self.store / name).mkdir(parents=True, exist_ok=True)
        try:
            (self.store / GC_PENDING_NAME).unlink()
        except OSError:
            pass

    def gc_pending(self) -> bool:
        return (self.store / GC_PENDING_NAME).exists()

    def gc_pending_age(self) -> float | None:
        """Seconds since a take-path rewrite first deferred its gc, or None when none is pending."""
        import time

        try:
            return max(0.0, time.time() - (self.store / GC_PENDING_NAME).stat().st_mtime)
        except OSError:
            return None

    def stale_gc_notice(self) -> str | None:
        """One warning per store per process when the deferred gc has waited past two default
        prune intervals: only the periodic prune reclaims, so it is not running (auto_prune off),
        the store keeps its size and, over the cap, no further snapshot is dropped until it does."""
        age = self.gc_pending_age()
        if age is None or age <= GC_STALE_AFTER_S or str(self.store) in GC_STALE_REPORTED:
            return None
        GC_STALE_REPORTED.add(str(self.store))
        return (f"Checkpoint store {self.store} has waited {age / 3600:.0f} h for a gc that only the "
                "periodic prune runs (is checkpoints.auto_prune off?); run `hermes checkpoints prune` "
                "to reclaim it")

    def _mark_gc_pending(self) -> None:
        # O_CREAT without utime: the mtime keeps the FIRST deferral, so its age says how long the
        # store has waited for a reclaim (touch() would reset it on every rewrite).
        try:
            os.close(os.open(self.store / GC_PENDING_NAME, os.O_CREAT | os.O_WRONLY, 0o644))
        except OSError:
            pass

    def _count_at_most(self, ref: str, limit: int) -> int:
        """Commits on ``ref``, counted only up to ``limit``: bounded whatever the history length."""
        return len(self._git(["rev-list", f"--max-count={max(1, limit)}", ref]).splitlines())

    def _commits(self, ref: str) -> list[str]:
        commits = self._git(["rev-list", "--reverse", ref]).splitlines()
        if not commits:
            raise PruneError(f"checkpoint ref has no readable history: {ref}")
        return commits

    def _rewrite(self, ref: str, original: list[str], keep: list[str], *, gc: bool) -> None:
        parent = None
        for sha in keep:
            metadata = self._git(["log", "-1", "--format=%T%x00%aI%x00%cI%x00%B", sha])  # whole body: keeps the take-time line
            fields = metadata.split("\x00", 3)
            if len(fields) != 4:
                raise PruneError(f"invalid checkpoint metadata: {sha}")
            tree, author_date, committer_date, message = fields
            args = ["commit-tree", tree, "--no-gpg-sign", "-m", message]
            if parent is not None:
                args += ["-p", parent]
            parent = self._git(args, extra_env={"GIT_AUTHOR_DATE": author_date, "GIT_COMMITTER_DATE": committer_date})
            if not parent:
                raise PruneError("commit-tree returned no checkpoint identity")
        if parent is None:
            raise PruneError("cannot remove a project's last snapshot")
        # Never overwrite a snapshot published since we read this history.
        self._git(["update-ref", ref, parent, original[-1]])
        if gc:
            self.reclaim()
        else:
            self._mark_gc_pending()

    def trim(self, ref: str, keep_count: int) -> None:
        """Snapshot-count budget on the checkpoint-take path: rewrites the ref, defers the gc.
        A bounded probe first, so a ref within budget costs one short ``rev-list``."""
        keep_count = max(1, keep_count)
        if self._count_at_most(ref, keep_count + 1) <= keep_count:
            return
        commits = self._commits(ref)
        if len(commits) > keep_count:
            self._rewrite(ref, commits, commits[-keep_count:], gc=False)

    def drop_oldest_once(self, ref: str, cap_bytes: int) -> bool:
        """Over the cap on the take path: drop the oldest snapshot of THIS project only, and only
        when no earlier drop still awaits its gc. Until the periodic prune reclaims, the store's
        size cannot move, so re-measuring it every turn and dropping again only erased history
        (a laptop store ended with 778 of 779 projects at one snapshot and still 14 GB), and a
        round over every project's ref ran one ``rev-list`` per project inside the tool call.
        Store-wide round-robin with a gc per round stays in ``enforce_size``."""
        if cap_bytes <= 0 or self.gc_pending():
            return False
        if self._count_at_most(ref, 2) < 2 or self.dir_size(self.store) <= cap_bytes:
            return False
        commits = self._commits(ref)
        if len(commits) < 2:
            return False
        self._rewrite(ref, commits, commits[1:], gc=False)
        return True

    def enforce_size(self, cap_bytes: int) -> bool:
        """Periodic prune: gc per round (the measurement only moves after a repack) until the
        store fits. Returns whether it did; a failed Git step raises immediately."""
        if cap_bytes <= 0 or self.dir_size(self.store) <= cap_bytes:
            return True
        self.reclaim()
        if self.dir_size(self.store) <= cap_bytes:
            return True
        refs = self._git(["for-each-ref", "--format=%(refname)", self.refs_prefix]).splitlines()
        # Round-robin, oldest snapshot per project. The size only moves after a gc, and a gc of a
        # GB store takes minutes, so gc after 1, 2, 4, ... rewrites: O(log projects) gcs per round
        # (one per rewrite held the store lock for hours over ~800 projects) and at most twice the
        # needed drops. Twenty rounds bound maintenance when many large histories remain.
        for _ in range(20):
            changed, pending, batch = False, 0, 1
            for ref in refs:
                commits = self._commits(ref)
                if len(commits) <= 1:
                    continue
                self._rewrite(ref, commits, commits[1:], gc=False)
                changed, pending = True, pending + 1
                if pending >= batch:
                    self.reclaim()
                    pending, batch = 0, batch * 2
                    if self.dir_size(self.store) <= cap_bytes:
                        return True
            if pending:
                self.reclaim()
                if self.dir_size(self.store) <= cap_bytes:
                    return True
            if not changed:
                break
        return False
