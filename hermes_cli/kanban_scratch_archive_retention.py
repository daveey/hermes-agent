"""Retention for scratch archives under ``<kanban home>/archive/scratch/<board>/<task>``. [daveey]

``gc_task_scratch`` archives every aged scratch tree before removing it, on the same disk, and nothing
used to remove the archives: a busy worker filled its disk with them (worker-a2, 2026-11-23: 62 GB in
403 archives over three days) until the dispatcher's disk floor stopped new claims.

Run by ``hermes kanban gc`` after the scratch pass, over every board's archives (one shared disk):

* **Age**: an archive older than ``kanban.scratch_archive_retention_days`` (whole days, default 7; 0
  keeps them) is removed.
* **Pressure**: when ``kanban.admission.disk_free_floor_bytes`` is set and the archive filesystem has
  less than twice that free, the oldest archives are removed until it has three times that free or none
  is left that is at least ``kanban.scratch_archive_pressure_min_age_hours`` old (default 24, at least
  1: the most recent finished work stays recoverable).
* **Abandoned builds**: a ``<task>.partial-<pid>`` whose pid is no longer running and that is at least
  the pressure min-age old (a gc killed mid-archive) is removed; a live one is never touched.

Settings come from the installation-root config, the same document the disk floor is read from
(``kanban_disk_admission``), so every profile on a host applies one policy to the shared archive root.
An archive's age runs from the newest mtime of the directory and the entries up to two levels below it
(payload files, manifest, repo files, detach intents), so neither a build still writing its payload
nor an archive between publication and its detach intent ever looks old.
Never touched: anything that is not a real directory two levels down (symlinks are not followed) and an
archive that still holds a ``detach-*.json`` recovery intent (an interrupted detach, left for manual
recovery). A missing or unreadable root, an unreadable entry or a failed removal is logged and skipped;
it never aborts the gc pass.
"""
from __future__ import annotations

import logging
import math
import os
import re
import shutil
import stat
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = 7
DEFAULT_PRESSURE_MIN_AGE_HOURS = 24
MIN_PRESSURE_MIN_AGE_HOURS = 1
# Pressure starts below PRESSURE_START x floor and stops at PRESSURE_TARGET x floor free.
PRESSURE_START = 2
PRESSURE_TARGET = 3
_PARTIAL = re.compile(r".+\.partial-(\d+)")


def archive_root() -> Path:
    from hermes_cli import kanban_db as kb
    return kb.kanban_home() / "archive" / "scratch"


def _kanban_doc() -> dict:
    from hermes_cli.config_backend import read_config_doc_readonly
    from hermes_constants import get_default_hermes_root
    try:
        doc = read_config_doc_readonly(Path(get_default_hermes_root()) / "config.yaml")
    except FileNotFoundError:
        return {}
    if doc is None:
        return {}
    if not isinstance(doc, dict):
        raise ValueError("config is not a mapping")
    kanban = doc.get("kanban")
    if kanban is None:
        return {}
    if not isinstance(kanban, dict):
        raise ValueError("kanban config is not a mapping")
    return kanban


def settings() -> tuple[int, float, Optional[int]]:
    """``(retention_days, pressure_min_age_hours, floor_bytes)``; raises ValueError on a bad value."""
    kanban = _kanban_doc()
    days = kanban.get("scratch_archive_retention_days", DEFAULT_RETENTION_DAYS)
    if type(days) is float and days.is_integer():
        days = int(days)  # YAML 7.0
    if type(days) is not int or days < 0:
        raise ValueError(f"invalid kanban.scratch_archive_retention_days: {days!r} (whole days >= 0)")
    min_age = kanban.get("scratch_archive_pressure_min_age_hours", DEFAULT_PRESSURE_MIN_AGE_HOURS)
    if (isinstance(min_age, bool) or not isinstance(min_age, (int, float)) or not math.isfinite(min_age)
            or min_age < MIN_PRESSURE_MIN_AGE_HOURS):
        raise ValueError(f"invalid kanban.scratch_archive_pressure_min_age_hours: {min_age!r} "
                         f"(>= {MIN_PRESSURE_MIN_AGE_HOURS})")
    admission = kanban.get("admission") or {}
    floor = admission.get("disk_free_floor_bytes") if isinstance(admission, dict) else None
    if floor is not None and (type(floor) is not int or floor <= 0):
        raise ValueError(f"invalid kanban.admission.disk_free_floor_bytes: {floor!r}")
    return days, float(min_age), floor


def _free_bytes(path: Path) -> int:
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


def _newest_mtime(path: Path, st: os.stat_result, depth: int = 2) -> float:
    """Newest lstat mtime of *path* and its entries up to *depth* levels below (links not followed)."""
    newest = st.st_mtime
    if depth <= 0 or not stat.S_ISDIR(st.st_mode):
        return newest
    with os.scandir(path) as it:
        for child in it:
            try:
                cst = child.stat(follow_symlinks=False)
            except OSError:
                continue  # removed meanwhile
            newest = max(newest, _newest_mtime(Path(child.path), cst, depth - 1))
    return newest


def _holds_intent(entry: Path) -> bool:
    with os.scandir(entry) as it:
        return any(e.name.startswith("detach-") and e.name.endswith(".json") for e in it)


def _pid_alive(pid: int) -> bool:
    from hermes_cli.kanban_db_dispatch_pid import _pid_alive as alive
    return alive(pid)


def _entries(root: Path) -> list[os.DirEntry]:
    try:
        boards = list(os.scandir(root))
    except FileNotFoundError:
        return []
    except OSError as exc:
        log.warning("Scratch archive retention: cannot read %s (%s); removing nothing", root, exc)
        return []
    out: list[os.DirEntry] = []
    for board in boards:
        try:
            if board.is_dir(follow_symlinks=False):
                out.extend(os.scandir(board.path))
        except OSError as exc:
            log.warning("Scratch archive retention: skipping board dir %s (%s)", board.path, exc)
    return out


def candidates(root: Path) -> tuple[list[tuple[float, Path]], list[tuple[float, Path]]]:
    """``(archives, abandoned partial builds)`` as ``(mtime, path)``, oldest first."""
    found: list[tuple[float, Path]] = []
    partials: list[tuple[float, Path]] = []
    for entry in _entries(root):
        try:
            st = entry.stat(follow_symlinks=False)
        except OSError:
            continue  # removed meanwhile
        if not stat.S_ISDIR(st.st_mode):
            continue
        path = Path(entry.path)
        partial = _PARTIAL.fullmatch(entry.name)
        try:
            if partial:
                if not _pid_alive(int(partial.group(1))):
                    partials.append((_newest_mtime(path, st), path))
                continue
            if _holds_intent(path):
                log.warning("Scratch archive retention: keeping %s (holds a detach recovery intent)", path)
                continue
            found.append((_newest_mtime(path, st), path))
        except OSError as exc:
            log.warning("Scratch archive retention: keeping %s (unreadable: %s)", path, exc)
    found.sort(key=lambda item: (item[0], str(item[1])))
    partials.sort(key=lambda item: (item[0], str(item[1])))
    return found, partials


def _remove(path: Path, why: str, age_s: float) -> bool:
    try:
        shutil.rmtree(path)
    except OSError as exc:
        log.warning("Scratch archive retention: could not remove %s (%s): %s", path, why, exc)
        return False
    log.info("Scratch archive retention: removed %s (%s, age %.1f h)", path, why, age_s / 3600)
    return True


def _pressure(root: Path, survivors: list[tuple[float, Path]], floor: int, min_age_s: float, now: float) -> int:
    removed = 0
    try:
        free = _free_bytes(root)
        if free >= PRESSURE_START * floor:
            return 0
        for mtime, path in survivors:
            age = now - mtime
            if free >= PRESSURE_TARGET * floor or age < min_age_s:
                break
            removed += _remove(path, f"disk pressure: {free} bytes free, floor {floor}", age)
            free = _free_bytes(root)
    except OSError as exc:
        log.warning("Scratch archive retention: cannot measure free space on %s (%s); pressure rule stopped",
                    root, exc)
        return removed
    if free < PRESSURE_START * floor:
        log.warning("Scratch archive retention: %d bytes free (under %d) after pruning: every archive left is "
                    "younger than %g h or could not be removed", free, PRESSURE_START * floor, min_age_s / 3600)
    return removed


def prune_scratch_archives(*, root: Optional[Path] = None, now: Optional[float] = None) -> int:
    """Apply the age, abandoned-build and pressure rules; return how many directories were removed."""
    try:
        days, min_age_hours, floor = settings()
    except Exception:  # health: allow BLE001 -- unreadable policy must remove nothing, not abort the gc pass
        log.warning("Cannot read scratch archive retention; keeping every archive", exc_info=True)
        return 0
    root = archive_root() if root is None else root
    now = time.time() if now is None else now
    min_age_s = min_age_hours * 3600
    archives, partials = candidates(root)
    removed = 0
    for mtime, path in partials:
        if now - mtime >= min_age_s:
            removed += _remove(path, "abandoned partial archive build", now - mtime)
    survivors: list[tuple[float, Path]] = []
    for mtime, path in archives:
        age = now - mtime
        if days and age > days * 86400:
            removed += _remove(path, f"older than {days} days", age)
        else:
            survivors.append((mtime, path))
    if floor is not None and survivors:
        removed += _pressure(root, survivors, floor, min_age_s, now)
    return removed
