"""Checkpoint staging guards: what ``git add -A`` must never read, and what a timed-out add leaves behind.

The size cap (``checkpoints.max_file_size_mb``) has to act BEFORE staging. ``git add`` hashes and
compresses every file it stages, so a cap applied afterwards (``_drop_oversize_from_index``) still
paid for a multi-GB file in full: the add ran past its timeout, the snapshot was lost, and the killed
git left a partial pack and its index lock in the store.
"""
from __future__ import annotations

import heapq
import logging
import os
import re
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("tools.checkpoint_manager")

# A folder whose staging timed out is not snapshotted again for this long (per process): every new
# turn would otherwise wait out the full timeout again for a snapshot that cannot finish.
_SLOW_DIR_BACKOFF_S = 3600
_slow_dirs: dict[tuple[str, str], float] = {}
_now = time.monotonic

# Temp files a killed git writes into the store: pack-objects/index-pack output in objects/pack,
# loose-object temps in objects/??/.
_TMP_PACK_PREFIXES = ("tmp_pack_", "tmp_idx_", "tmp_rev_", "tmp_mtimes_", ".tmp-")
_TMP_OBJ_PREFIX = "tmp_obj_"
# Filesystems with 1-2 s mtime granularity (HFS+, FAT) can stamp a file created after the call
# started slightly before it.
_MTIME_SLACK_S = 2.0


@dataclass
class TreeScan:
    """One walk of a working tree: entry count (capped), oversize files, biggest files."""

    count: int = 0
    oversize: list[str] = field(default_factory=list)
    biggest: list[tuple[int, str]] = field(default_factory=list)

    def biggest_text(self) -> str:
        return ", ".join(f"{rel} ({size / 1048576:.1f} MB)" for size, rel in self.biggest) or "none"


def scan_tree(path: str, cap_bytes: int, max_entries: int) -> TreeScan:
    """Walk ``path`` once: count entries (stop past ``max_entries``), collect files over ``cap_bytes``
    (0 = no cap) as POSIX paths relative to ``path``, and keep the 3 biggest files for the logs.

    Symlinks are not followed (git stages the link, not its target)."""
    scan = TreeScan()
    root = Path(path)
    heap: list[tuple[int, str]] = []
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            scan.count += len(dirnames) + len(filenames)
            if scan.count > max_entries:
                break
            if ".git" in dirnames:
                dirnames.remove(".git")
            for name in filenames:
                full = Path(dirpath) / name
                try:
                    st = full.lstat()
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode):
                    continue
                rel = full.relative_to(root).as_posix()
                if cap_bytes > 0 and st.st_size > cap_bytes:
                    scan.oversize.append(rel)
                if len(heap) < 3:
                    heapq.heappush(heap, (st.st_size, rel))
                elif st.st_size > heap[0][0]:
                    heapq.heapreplace(heap, (st.st_size, rel))
    except OSError:
        pass
    scan.biggest = sorted(heap, reverse=True)
    return scan


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


def mark_slow(base: Path, working_dir: str) -> None:
    _slow_dirs[(str(base), working_dir)] = _now() + _SLOW_DIR_BACKOFF_S


def slow_remaining_s(base: Path, working_dir: str) -> float:
    """Seconds left in ``working_dir``'s back-off window, 0 when it may be snapshotted."""
    key = (str(base), working_dir)
    until = _slow_dirs.get(key)
    if until is None:
        return 0.0
    left = until - _now()
    if left <= 0:
        _slow_dirs.pop(key, None)
        return 0.0
    return left
