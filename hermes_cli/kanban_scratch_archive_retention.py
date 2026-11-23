"""Retention for scratch archives under ``<kanban home>/archive/scratch/<board>/<task>``. [daveey]

``gc_task_scratch`` archives every aged scratch tree before removing it, on the same disk, and nothing
used to remove the archives: a busy worker filled its disk with them (worker-a2, 2026-11-23: 62 GB in
403 archives over three days) until the dispatcher's disk floor stopped new claims.

Two rules, run by ``hermes kanban gc`` after the scratch pass:

* **Age**: an archive older than ``kanban.scratch_archive_retention_days`` (default 7; 0 keeps them) is
  removed.
* **Pressure**: when ``kanban.admission.disk_free_floor_bytes`` is set and the archive filesystem has
  less than twice that free, the oldest archives are removed until it has three times that free or none
  is left that is at least ``kanban.scratch_archive_pressure_min_age_hours`` old (default 24; the most
  recent finished work stays recoverable). Hosts without a floor only get the age rule.

An archive's age is its directory's mtime (publication, or the last detach intent written into it).
Never touched: anything that is not a real directory two levels down (symlinks are not followed),
in-flight ``.partial-<pid>`` builds, and an archive that still holds a ``detach-*.json`` recovery
intent (an interrupted detach; ``remove_archived`` leaves those for manual recovery).
"""
from __future__ import annotations

import logging
import math
import os
import shutil
import stat
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = 7
DEFAULT_PRESSURE_MIN_AGE_HOURS = 24
# Pressure starts below PRESSURE_START x floor and stops at PRESSURE_TARGET x floor free.
PRESSURE_START = 2
PRESSURE_TARGET = 3


def archive_root() -> Path:
    from hermes_cli import kanban_db as kb
    return kb.kanban_home() / "archive" / "scratch"


def _number(value, name: str, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f"invalid kanban.{name}: {value!r}")
    return float(value)


def settings() -> tuple[float, float, Optional[int]]:
    """``(retention_days, pressure_min_age_hours, floor_bytes)``; raises ValueError on a bad value."""
    from hermes_cli.config import load_config
    kanban = load_config().get("kanban") or {}
    days = _number(kanban.get("scratch_archive_retention_days"), "scratch_archive_retention_days",
                   DEFAULT_RETENTION_DAYS)
    min_age = _number(kanban.get("scratch_archive_pressure_min_age_hours"),
                      "scratch_archive_pressure_min_age_hours", DEFAULT_PRESSURE_MIN_AGE_HOURS)
    floor = (kanban.get("admission") or {}).get("disk_free_floor_bytes")
    if floor is not None and (type(floor) is not int or floor <= 0):
        raise ValueError(f"invalid kanban.admission.disk_free_floor_bytes: {floor!r}")
    return days, min_age, floor


def _free_bytes(path: Path) -> int:
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


def _holds_intent(entry: Path) -> bool:
    with os.scandir(entry) as it:
        return any(e.name.startswith("detach-") and e.name.endswith(".json") for e in it)


def candidates(root: Path) -> list[tuple[float, Path]]:
    """Removable archives as ``(mtime, path)``, oldest first."""
    found: list[tuple[float, Path]] = []
    try:
        boards = list(os.scandir(root))
    except FileNotFoundError:
        return found
    for board in boards:
        try:
            if not board.is_dir(follow_symlinks=False):
                continue
            entries = list(os.scandir(board.path))
        except OSError as exc:
            log.warning("Scratch archive retention: skipping board dir %s (%s)", board.path, exc)
            continue
        for entry in entries:
            if ".partial-" in entry.name:
                continue
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue  # removed meanwhile
            if not stat.S_ISDIR(st.st_mode):
                continue
            path = Path(entry.path)
            try:
                if _holds_intent(path):
                    log.warning("Scratch archive retention: keeping %s (holds a detach recovery intent)", path)
                    continue
            except OSError as exc:
                log.warning("Scratch archive retention: keeping %s (unreadable: %s)", path, exc)
                continue
            found.append((st.st_mtime, path))
    found.sort(key=lambda item: (item[0], str(item[1])))
    return found


def _remove(path: Path, why: str, age_s: float) -> bool:
    try:
        shutil.rmtree(path)
    except OSError as exc:
        log.warning("Scratch archive retention: could not remove %s (%s): %s", path, why, exc)
        return False
    log.info("Scratch archive retention: removed %s (%s, age %.1f h)", path, why, age_s / 3600)
    return True


def prune_scratch_archives(*, root: Optional[Path] = None, now: Optional[float] = None) -> int:
    """Apply the age and pressure rules; return how many archives were removed."""
    try:
        days, min_age_hours, floor = settings()
    except Exception:  # health: allow BLE001 -- unreadable policy must remove nothing, not abort the gc pass
        log.warning("Cannot read scratch archive retention; keeping every archive", exc_info=True)
        return 0
    root = archive_root() if root is None else root
    now = time.time() if now is None else now
    removed = 0
    survivors: list[tuple[float, Path]] = []
    for mtime, path in candidates(root):
        age = now - mtime
        if days and age > days * 86400:
            removed += _remove(path, f"older than {days:g} days", age)
        else:
            survivors.append((mtime, path))
    if floor is None or not root.is_dir():
        return removed
    free = _free_bytes(root)
    if free >= PRESSURE_START * floor:
        return removed
    for mtime, path in survivors:
        age = now - mtime
        if free >= PRESSURE_TARGET * floor or age < min_age_hours * 3600:
            break
        if _remove(path, f"disk pressure: {free} bytes free, floor {floor}", age):
            removed += 1
        free = _free_bytes(root)
    if free < PRESSURE_START * floor:
        log.warning("Scratch archive retention: still %d bytes free (under %d) after pruning; no older archive "
                    "than %g h could be removed", free, PRESSURE_START * floor, min_age_hours)
    return removed
