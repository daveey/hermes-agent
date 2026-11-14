"""Bound one scheduled scratch GC pass: archive-refusal backoff and a time budget. [daveey]

``hermes kanban gc`` archives every aged scratch tree holding git clones before
removing it. A tree the archiver refuses (a linked worktree, a bare repo, a
partial clone whose bundle needs the promisor remote, ...) used to be re-scanned
on every hourly pass, and some refusals cost over a minute each. On a busy
worker the pass then ran past the scheduler's 1800 s kill, so its ``GC complete``
line was never printed and the pass counted as failed (worker-2, 2026-11-13).

Two bounds, both fail-safe (each one only *keeps* a tree for later):

* :class:`Refusals` remembers each refused tree with exponential backoff
  (``BASE_S`` doubling to ``CAP_S``). A tree is retried at once when its
  fingerprint (path, ``completed_at``, root inode and mtime) changes.
* :class:`Budget` stops starting new archives once the pass's deadline has
  passed; the remaining candidates wait for the next pass.

Every guard in the removal path still runs for every tree that is attempted.
State lives at ``<kanban home>/kanban/scratch-gc/refusals-<board>.json``, host
local and never synced; an unreadable file is treated as empty and a malformed
entry (wrong field types) is dropped with one warning, so neither can stop a pass.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

BASE_S = 2 * 3600
CAP_S = 24 * 3600
FORGET_S = 14 * 86400
MAX_COUNT = 64  # count saturates here: CAP_S is reached at 4, and 2 ** count must stay cheap
DEFAULT_BUDGET_S = 1200  # the worker/Mac GC wrappers kill a board's pass at 1800 s
MAX_EPOCH_S = 3.2e10  # loaded timestamps stay below Windows' gmtime limit (~3.25e10, year 3000)


class Budget:
    """Monotonic deadline for one pass; ``None`` seconds (or <= 0) means unlimited."""

    def __init__(self, seconds: Optional[float] = None, *, clock=time.monotonic):
        self._clock = clock
        self.started = clock()
        self.deadline = self.started + seconds if seconds and seconds > 0 else None
        self.deferred = 0

    def exhausted(self) -> bool:
        return self.deadline is not None and self._clock() >= self.deadline

    def defer(self) -> None:
        self.deferred += 1

    def report(self, what: str) -> None:
        if self.deferred:
            log.warning("Scratch GC budget reached after %ds: deferred %d %s to the next pass; "
                        "kept, all safety guards apply when retried",
                        int(self._clock() - self.started), self.deferred, what)


def _path(board: str) -> Path:
    from hermes_cli import kanban_db as kb
    return kb.kanban_home() / "kanban" / "scratch-gc" / f"refusals-{board}.json"


def fingerprint(path: Path, completed_at) -> Optional[list]:
    try:
        st = os.lstat(path)
    except OSError:
        return None
    return [str(path), completed_at, st.st_ino, st.st_dev, st.st_mtime_ns]


def _finite(value) -> bool:
    """A real epoch-seconds number ``time.gmtime`` can format on every host (no bool, NaN, inf or
    year > ~2984: Windows' gmtime raises past 3000-12-31, about 3.25e10)."""
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and abs(value) < MAX_EPOCH_S)


def _utc(epoch) -> str:
    """ISO UTC for a log line; the raw number when the platform cannot format it (never raises)."""
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))
    except (OverflowError, OSError, ValueError):
        return f"{epoch:.0f}"


def _well_formed(entry) -> bool:
    """The shape :meth:`Refusals.record` writes, so :func:`skip_logged` never raises on loaded state."""
    return (isinstance(entry, dict)
            and _finite(entry.get("last_at")) and _finite(entry.get("retry_at"))
            and isinstance(entry.get("count"), int) and not isinstance(entry["count"], bool)
            and 1 <= entry["count"] <= MAX_COUNT
            and (entry.get("fingerprint") is None or isinstance(entry["fingerprint"], list))
            and (entry.get("reason") is None or isinstance(entry["reason"], str)))


class Refusals:
    """Per-tree archive refusals of one board, with exponential retry backoff."""

    def __init__(self, board: str, *, now=time.time):
        self._now = now
        self.file = _path(board)
        self.entries: dict[str, dict] = {}
        try:
            data = json.loads(self.file.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, TypeError):
            return
        if not (isinstance(data, dict) and isinstance(data.get("entries"), dict)):
            return
        cutoff = now() - FORGET_S
        dropped = 0
        for key, entry in data["entries"].items():
            if not _well_formed(entry):
                dropped += 1
            elif entry["last_at"] >= cutoff:
                self.entries[key] = entry
        if dropped:
            # Hand-edited or foreign state: those trees count as never refused (fail-safe,
            # every removal guard still runs on them).
            log.warning("Scratch GC refusal backoff %s: dropped %d malformed entr%s; "
                        "those trees are retried as never refused", self.file, dropped,
                        "y" if dropped == 1 else "ies")

    def backing_off(self, key: str, path: Path, completed_at) -> Optional[dict]:
        """The entry when *key* was refused and its retry time is still ahead for the same tree."""
        entry = self.entries.get(key)
        if not entry or entry.get("fingerprint") != fingerprint(path, completed_at):
            return None
        return entry if float(entry.get("retry_at") or 0) > self._now() else None

    def record(self, key: str, path: Path, completed_at, reason: str) -> None:
        prior = self.entries.get(key) or {}
        fp = fingerprint(path, completed_at)
        count = (min(MAX_COUNT, int(prior.get("count") or 0) + 1)
                 if prior.get("fingerprint") == fp else 1)
        now = self._now()
        self.entries[key] = {"fingerprint": fp, "count": count, "last_at": now,
                             "retry_at": now + min(CAP_S, BASE_S * 2 ** (count - 1)),
                             "reason": str(reason)[:300]}
        self._save()

    def clear(self, key: str) -> None:
        if self.entries.pop(key, None) is not None:
            self._save()

    def _save(self) -> None:
        try:
            self.file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.file.with_name(f".{self.file.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps({"version": 1, "entries": self.entries}, sort_keys=True), encoding="utf-8")
            os.replace(tmp, self.file)
        except OSError:
            log.warning("Scratch GC refusal backoff not saved (%s); refused trees retry next pass",
                        self.file, exc_info=True)


# ``hermes kanban gc`` prints every ``Scratch preserved: `` record on stdout (kanban_ops._cmd_gc)
# and the Mac/worker GC wrappers relay those lines into their own log, so a kept tree's reason
# outlives agent.log's rotation. The detail is cut to this alphabet: the relay full-matches it.
_DETAIL_UNSAFE = re.compile(r"[^A-Za-z0-9_./:=,()' -]")
DETAIL_MAX = 200


def keep_detail(reason) -> str:
    """*reason* as one relay-safe line: any character outside the alphabet becomes ``?``, cut to 200."""
    return _DETAIL_UNSAFE.sub("?", str(reason))[:DETAIL_MAX]


def report_archive_refused(path: Path, task_id: str, entry: Optional[dict], reason) -> None:
    """One ``Scratch preserved: reason=archive_refused`` line for a tree kept by an archive refusal.

    *entry* is the tree's backoff entry (attempt count and next retry); ``None`` when the caller
    keeps no backoff state, which reads as a first attempt with no retry time.
    """
    attempts, retry_at = (entry["count"], int(entry["retry_at"])) if entry else (1, None)
    log.warning("Scratch preserved: reason=archive_refused path=%s task=%s attempts=%s retry_at=%s; %s",
                path, task_id, attempts, retry_at, keep_detail(reason))


def skip_logged(refusals: Refusals, key: str, path: Path, completed_at, task_id: str) -> bool:
    """True (and one INFO line) when *key* is inside its backoff window: the tree is kept untouched."""
    entry = refusals.backing_off(key, path, completed_at)
    if entry is None:
        return False
    log.info("Keeping aged scratch workspace for task %s: %s (archive refused %d time(s), "
             "next retry after %s; last: %s)", task_id, path, entry["count"],
             _utc(entry["retry_at"]), entry.get("reason"))
    report_archive_refused(path, task_id, entry, entry.get("reason"))
    return True
