"""Checkpoint staging guards: what ``git add -A`` must never read, and what a timed-out add leaves behind.

The size cap (``checkpoints.max_file_size_mb``) has to act BEFORE staging. ``git add`` hashes and
compresses every file it stages, so a cap applied afterwards (``_drop_oversize_from_index``) still
paid for a multi-GB file in full: the add ran past its timeout, the snapshot was lost, and the killed
git left a partial pack and its index lock in the store.

The cap alone does not bound the add: a tree of many files under the cap (a fresh source checkout)
still costs one hash, and on a first snapshot one compressed object write, per file. ``estimate``
prices the add before it runs, and a folder over the budget is skipped without waiting out the
timeout. Skipped and timed-out folders back off in a file inside the store, so a new process (the
next ``hermes chat``, a respawned backend) does not pay for the same folder again.
"""
from __future__ import annotations

import fnmatch
import heapq
import json
import logging
import os
import re
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("tools.checkpoint_manager")

# A folder whose staging timed out (or was priced over budget) is not snapshotted again for this long:
# every new turn would otherwise wait out the full timeout again for a snapshot that cannot finish.
# Persisted in the store (wall clock, so it means the same in every process); a sleep only shortens it.
_SLOW_DIR_BACKOFF_S = 3600
_SLOW_DIRS_FILE = "slow-dirs.json"
_slow_dirs: dict[tuple[str, str], float] = {}
_now = time.time

# Cost model for ``git add -A`` into the checkpoint store, measured on hermes-hub (16 cores, load
# average 11-14, git 2.55) with a bare store and a separate index, as _stage_all runs it:
#   first add, 20,000 x 4 KB files (78 MB)      25.2 s  -> ~1.1 ms per file (hash + loose-object write)
#   first add, 120 x 8 MB files (960 MB)        34.1 s  -> ~28 MB/s (hash + zlib)
#   first add, a hermes-agent checkout (19,021 files, 209 MB) 37.1 s; the same tree after a
#   stat-less read-tree 18.8 s; with a stat-carrying index 0.03 s.
# Every file git has to (re)hash is priced at first-add rates; that pessimism covers rehash-only
# adds of objects already in the store. The estimate is scaled by load average / cores (the MacBook
# hit ran at load ~56) and must fit in half the add timeout.
ADD_SECONDS_PER_FILE = 0.0011
ADD_BYTES_PER_SECOND = 28 * 1024 * 1024
BUDGET_FRACTION = 0.5

# Temp files a killed git writes into the store: pack-objects/index-pack output in objects/pack,
# loose-object temps in objects/??/.
_TMP_PACK_PREFIXES = ("tmp_pack_", "tmp_idx_", "tmp_rev_", "tmp_mtimes_", ".tmp-")
_TMP_OBJ_PREFIX = "tmp_obj_"
# Filesystems with 1-2 s mtime granularity (HFS+, FAT) can stamp a file created after the call
# started slightly before it.
_MTIME_SLACK_S = 2.0


@dataclass
class TreeScan:
    """One walk of a working tree: entry count (capped), oversize files, biggest files, and the
    size and mtime of every regular file under the cap (what ``git add -A`` may have to hash)."""

    count: int = 0
    oversize: list[str] = field(default_factory=list)
    biggest: list[tuple[int, str]] = field(default_factory=list)
    files: dict[str, tuple[int, float]] = field(default_factory=dict)

    def biggest_text(self) -> str:
        return ", ".join(f"{rel} ({size / 1048576:.1f} MB)" for size, rel in self.biggest) or "none"


def _has_negation(gitignore: Path) -> bool:
    """True when a ``.gitignore`` holds a ``!`` re-include: it may override the store's excludes."""
    try:
        text = gitignore.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return True  # unreadable: assume it could re-include, never prune under it
    return any(line.lstrip().startswith("!") for line in text.splitlines())


def scan_tree(
    path: str, cap_bytes: int, max_entries: int, *,
    excludes: tuple[str, ...] = (), skip: frozenset[str] = frozenset(),
) -> TreeScan:
    """Walk ``path`` once: count entries (stop past ``max_entries``), collect files over ``cap_bytes``
    (0 = no cap) as POSIX paths relative to ``path``, and keep the 3 biggest files for the logs.

    ``skip`` holds relative POSIX paths git is told never to stage (the checkpoint store nested in
    the tree): they are neither walked nor counted. ``excludes`` are gitignore-style name patterns
    (the store's ``info/exclude``; a trailing ``/`` means directories only) pruned the way git
    applies them, so an excluded ``node_modules/`` is not walked. A subtree under a ``.gitignore``
    with a ``!`` re-include is walked in full: git may stage what the patterns name there.

    Symlinks are not followed (git stages the link, not its target)."""
    scan = TreeScan()
    root = Path(path)
    heap: list[tuple[int, str]] = []
    dir_patterns = tuple(p[:-1] for p in excludes if p.endswith("/"))
    file_patterns = tuple(p for p in excludes if not p.endswith("/"))
    unprunable: set[str] = set()  # dirpaths under a re-including .gitignore

    def excluded(name: str, patterns: tuple[str, ...]) -> bool:
        return any(fnmatch.fnmatchcase(name, p) for p in patterns)

    try:
        for dirpath, dirnames, filenames in os.walk(root):
            rel_dir = Path(dirpath).relative_to(root).as_posix()
            prefix = "" if rel_dir == "." else rel_dir + "/"
            if skip:
                dirnames[:] = [d for d in dirnames if prefix + d not in skip]
                filenames = [f for f in filenames if prefix + f not in skip]
            if ".git" in dirnames:
                dirnames.remove(".git")
            if excludes:
                parent = str(Path(dirpath).parent)
                prune = not (
                    (dirpath != str(root) and parent in unprunable)
                    or (".gitignore" in filenames and _has_negation(Path(dirpath) / ".gitignore"))
                )
                if prune:
                    # git applies slash-less patterns (``*.log``) to directories too
                    dirnames[:] = [d for d in dirnames if not excluded(d, dir_patterns + file_patterns)]
                    filenames = [f for f in filenames if not excluded(f, file_patterns)]
                else:
                    unprunable.add(dirpath)
            scan.count += len(dirnames) + len(filenames)
            if scan.count > max_entries:
                break
            for name in filenames:
                full = Path(dirpath) / name
                try:
                    st = full.lstat()
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode):
                    continue
                rel = prefix + name
                if cap_bytes > 0 and st.st_size > cap_bytes:
                    scan.oversize.append(rel)
                else:
                    scan.files[rel] = (st.st_size, st.st_mtime)
                if len(heap) < 3:
                    heapq.heappush(heap, (st.st_size, rel))
                elif st.st_size > heap[0][0]:
                    heapq.heapreplace(heap, (st.st_size, rel))
    except OSError:
        pass
    scan.biggest = sorted(heap, reverse=True)
    return scan


def add_tracked(scan: TreeScan, root: str, tracked: list[str], cap_bytes: int) -> None:
    """Price and size-cap index entries the pruned walk did not see.

    ``add -A`` re-reads every path already in the index whatever the excludes say, so a tracked
    file inside a pruned directory must still reach ``oversize`` before git hashes it."""
    seen = set(scan.files) | set(scan.oversize)
    base = Path(root)
    for rel in tracked:
        if rel in seen:
            continue
        try:
            st = (base / rel).lstat()
        except (OSError, ValueError):
            continue
        if not stat.S_ISREG(st.st_mode):
            continue
        if cap_bytes > 0 and st.st_size > cap_bytes:
            scan.oversize.append(rel)
        else:
            scan.files[rel] = (st.st_size, st.st_mtime)
        seen.add(rel)


_GITIGNORE_SPECIAL = re.compile(r"([\\*?\[\]!#])")


def gitignore_literal(rel: str) -> Optional[str]:
    """An anchored gitignore pattern matching exactly the path ``rel``; None when gitignore cannot
    express it (a newline in the name)."""
    if "\n" in rel or "\r" in rel:
        return None
    escaped = _GITIGNORE_SPECIAL.sub(r"\\\1", rel)
    stripped = escaped.rstrip(" ")
    escaped = stripped + "\\ " * (len(escaped) - len(stripped))
    return "/" + escaped


def write_oversize_excludes(store: Path, dir_hash: str, oversize: list[str]) -> Optional[Path]:
    """Write the per-project excludes file passed to ``git add`` as ``core.excludesFile``."""
    lines = [p for p in (gitignore_literal(rel) for rel in oversize) if p]
    if not lines:
        return None
    path = store / "info" / f"oversize-{dir_hash}"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # fsencode: POSIX names need not be UTF-8 (surrogate-escaped here).
        path.write_bytes(b"".join(os.fsencode(line) + b"\n" for line in lines))
    except (OSError, UnicodeError) as exc:
        logger.warning("Checkpoint: could not write oversize excludes %s: %s", path, exc)
        return None
    return path


def write_oversize_pathspec(store: Path, dir_hash: str, paths: list[str]) -> Optional[Path]:
    """Write the NUL-separated literal exclude pathspecs ``git add`` reads via ``--pathspec-from-file``.

    The excludes file alone loses to a project ``.gitignore`` re-include (``*.bin`` then
    ``!weights.bin``): git then opens the oversize file and runs its clean filter. An exclude
    pathspec is not a gitignore rule, so nothing in the tree can override it, and ``literal``
    keeps any name (glob characters, leading/trailing spaces, newlines) exact.
    """
    path = store / "info" / f"oversize-{dir_hash}.pathspec"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # fsencode: POSIX names need not be UTF-8 (surrogate-escaped here).
        path.write_bytes(b"".join(os.fsencode(f":(exclude,top,literal){rel}") + b"\0" for rel in paths))
    except (OSError, UnicodeError) as exc:
        logger.warning("Checkpoint: could not write oversize pathspec %s: %s", path, exc)
        return None
    return path


def clear_timeout_debris(store: Path, since: float, index_file: Optional[Path]) -> list[str]:
    """Delete what a git killed at its timeout left in the store: partial packs, loose-object temps
    and the index lock, written since ``since`` (wall clock).

    The caller holds the checkpoint store lock, so every store write since ``since`` is this call's.
    Older debris belongs to the periodic sweep (``gitlock.clear_stale_tmp_packs``)."""
    objects = store / "objects"
    candidates: list[Path] = []
    try:
        for entry in objects.iterdir():
            if entry.name == "pack":
                candidates += [p for p in entry.iterdir() if p.name.startswith(_TMP_PACK_PREFIXES)]
            elif len(entry.name) == 2 and entry.is_dir():
                candidates += [p for p in entry.iterdir() if p.name.startswith(_TMP_OBJ_PREFIX)]
    except OSError:
        pass
    if index_file is not None:
        candidates.append(Path(f"{index_file}.lock"))
    removed: list[str] = []
    cutoff = since - _MTIME_SLACK_S
    for path in candidates:
        try:
            if path.is_file() and path.stat().st_mtime >= cutoff:
                path.unlink()
                removed.append(str(path))
        except OSError as exc:
            logger.debug("Checkpoint: could not remove timeout debris %s: %s", path, exc)
    return removed


@dataclass
class AddEstimate:
    """What ``git add -A`` is expected to hash, and how long that takes on this host now."""

    files: int
    bytes: int
    seconds: float

    def text(self) -> str:
        return f"{self.files} files / {self.bytes / 1048576:.0f} MB to hash, ~{self.seconds:.0f}s"


def load_factor() -> float:
    """How much slower than the measured rates this host runs right now (>= 1)."""
    try:
        return max(1.0, os.getloadavg()[0] / (os.cpu_count() or 1))
    except (AttributeError, OSError):  # no getloadavg (Windows)
        return 1.0


def estimate(scan: TreeScan, fresh_since: Optional[float], only: Optional[set[str]] = None) -> AddEstimate:
    """Price the add.

    ``fresh_since`` is the mtime of an index that carries stat data from the last snapshot: git
    skips files unchanged since then, so only files modified at or after it count (None = a cold
    index, every file is hashed). ``only`` narrows to the paths git itself reports as new or
    modified (``ls-files -o -m``), which also drops gitignored files the walk counted."""
    files = nbytes = 0
    cutoff = None if fresh_since is None else fresh_since - _MTIME_SLACK_S
    for rel, (size, mtime) in scan.files.items():
        if only is not None:
            if rel not in only:
                continue
        elif cutoff is not None and mtime < cutoff:
            continue
        files += 1
        nbytes += size
    seconds = (files * ADD_SECONDS_PER_FILE + nbytes / ADD_BYTES_PER_SECOND) * load_factor()
    return AddEstimate(files, nbytes, seconds)


def budget_s(add_timeout_s: float) -> float:
    return add_timeout_s * BUDGET_FRACTION


def _slow_file(store: Path) -> Path:
    return store / "info" / _SLOW_DIRS_FILE


def _read_slow(store: Path) -> dict[str, float]:
    try:
        data = json.loads(_slow_file(store).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: float(v) for k, v in data.items() if isinstance(k, str) and isinstance(v, (int, float))}


def mark_slow(store: Path, working_dir: str) -> None:
    """Back ``working_dir`` off for ``_SLOW_DIR_BACKOFF_S``, in this process and in the store.

    Callers hold the checkpoint store lock, so the read-modify-write does not race."""
    now = _now()
    until = now + _SLOW_DIR_BACKOFF_S
    _slow_dirs[(str(store), working_dir)] = until
    entries = {k: v for k, v in _read_slow(store).items() if v > now}
    entries[working_dir] = until
    path = _slow_file(store)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(entries, indent=0, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("Checkpoint: could not persist the back-off for %s: %s", working_dir, exc)
        try:
            tmp.unlink()
        except OSError:
            pass


def slow_remaining_s(store: Path, working_dir: str) -> float:
    """Seconds left in ``working_dir``'s back-off window, 0 when it may be snapshotted.

    Reads this process's registry first, then the store's file (another process's back-off)."""
    key = (str(store), working_dir)
    until = _slow_dirs.get(key)
    if until is None:
        until = _read_slow(store).get(working_dir)
    if until is None:
        return 0.0
    # Clamp: a wall clock set back must not stretch the window past one back-off period.
    left = min(until - _now(), float(_SLOW_DIR_BACKOFF_S))
    if left <= 0:
        _slow_dirs.pop(key, None)
        return 0.0
    return left
