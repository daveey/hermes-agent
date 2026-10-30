"""Which profile's ``processes.json`` owns a background job, and settling finished jobs everywhere. [daveey]

The multiplexed gateway serves every profile from one process and one ``ProcessRegistry``, but
``_checkpoint_path`` is profile-scoped, so each profile's checkpoint used to receive EVERY live job.
A finished job then left only the file of the profile that saw its exit; every other profile kept its
copy and re-attached that finished job at the next restart, one profile per restart (proc_05186a9e0419,
owned by alpha-askeve, re-attached by alpha-follow, bb-tuner and ci-sweeper on 2026-10-30).

Rules:
  * a job whose session key names a profile (``agent:<profile>:...``) belongs to that profile's file;
    ``agent:main:...`` names the default profile only in a multiplexed registry (a single-profile
    gateway builds ``agent:main`` keys for its own profile). Keys that name no profile (Desktop/TUI
    session ids, keyless CLI and worker jobs) belong wherever they are written, as before.
  * a job whose exit is recorded (a result receipt in any profile) is settled: it leaves every
    profile's checkpoint and is never re-attached.
"""

from __future__ import annotations

import contextlib
import json
import logging
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger("tools.process_registry")


def entry_profile(session_key: Any) -> Optional[str]:
    """Profile a job's session key names, or None when it names none."""
    from hermes_state_keys import profile_from_session_key
    return profile_from_session_key(session_key)


def scope_profile() -> Optional[str]:
    """Profile of the home this thread's checkpoint writes go to (None = not a recognisable profile home)."""
    from hermes_constants import get_hermes_home, profile_name_for_home
    return profile_name_for_home(get_hermes_home())


def owner_of(session_key: Any, *, multiplexed: bool) -> Optional[str]:
    """Profile that owns a job with *session_key*, or None when any profile's file may carry it."""
    profile = entry_profile(session_key)
    if profile == "default" and not multiplexed:
        return None
    return profile


def owned_here(session_key: Any, *, multiplexed: bool, scope: Optional[str] = None) -> bool:
    """True when this thread's profile checkpoint may carry (and recover) the job."""
    owner = owner_of(session_key, multiplexed=multiplexed)
    if owner is None:
        return True
    here = scope if scope is not None else scope_profile()
    return here is None or owner == here


def profile_homes() -> list[Path]:
    """The default home and every named profile home under it."""
    from hermes_constants import get_default_hermes_root
    root = get_default_hermes_root()
    homes = [root]
    with contextlib.suppress(OSError):
        homes += sorted(p for p in (root / "profiles").iterdir() if p.is_dir() and not p.name.startswith("."))
    return homes


def home_of_profile(profile: str) -> Optional[Path]:
    from hermes_constants import profile_name_for_home
    for home in profile_homes():
        if profile_name_for_home(home) == profile:
            return home
    return None


def result_recorded(session_id: Any) -> bool:
    """True when any profile holds the job's completion receipt: its exit was observed and delivered."""
    if not isinstance(session_id, str) or not session_id.startswith("proc_") or "/" in session_id:
        return False
    return any((home / "logs" / "process-results" / f"{session_id}.json").exists() for home in profile_homes())


def checkpoint_paths() -> list[Path]:
    from tools.process_registry import _checkpoint_path
    paths = [home / "processes.json" for home in profile_homes()]
    current = _checkpoint_path()
    if current not in paths:
        paths.append(current)
    return paths


@contextlib.contextmanager
def _file_lock(path: Path):
    """The same ``processes.json.lock`` flock every writer takes. Callers never hold another one."""
    try:
        import fcntl
    except ImportError:  # Windows
        yield
        return
    with open(str(path) + ".lock", "ab") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        except OSError:
            pass
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _holding_a_checkpoint_lock() -> bool:
    from tools.process_registry_checkpoint import _CHECKPOINT_LOCK_HELD
    return bool(getattr(_CHECKPOINT_LOCK_HELD, "depth", 0))


def _rewrite(path: Path, edit) -> bool:
    """Read-edit-write one checkpoint under its lock; True when it changed."""
    from utils import atomic_json_write
    if not path.exists():
        return False
    with _file_lock(path):
        try:
            entries = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return False
        if not isinstance(entries, list):
            return False
        updated = edit(entries)
        if updated is None or updated == entries:
            return False
        atomic_json_write(path, updated)
        return True


def drop_from_every_checkpoint(session_ids: Iterable[Any]) -> list[Path]:
    """Remove settled jobs from every profile's ``processes.json``; returns the files changed.

    Takes one file lock at a time, and never while this thread holds a checkpoint lock (a second
    flock on the same file from this process would wait on itself): then it does nothing, and the
    next recovery's recorded-result check settles the copies instead."""
    ids = {sid for sid in session_ids if sid}
    if not ids or _holding_a_checkpoint_lock():
        return []
    changed = []
    for path in checkpoint_paths():
        try:
            if _rewrite(path, lambda entries: [e for e in entries
                                               if not (isinstance(e, dict) and e.get("session_id") in ids)]):
                changed.append(path)
        except Exception:
            logger.debug("Could not drop %s from %s", sorted(ids), path, exc_info=True)
    if changed:
        logger.info("Dropped finished process(es) %s from %d other profile checkpoint(s)",
                    ", ".join(sorted(ids)), len(changed))
    return changed


def hand_to_owners(entries: list[dict[str, Any]], source: Path, *, multiplexed: bool) -> int:
    """Move jobs another profile owns out of *source*: add each to its owner's checkpoint when that file
    lacks it, then drop it from *source* (in that order, so a crash in between leaves a copy, never none).
    Returns how many left *source*. Never called while holding a checkpoint lock."""
    if not entries or _holding_a_checkpoint_lock():
        return 0
    moved: set = set()
    for entry in entries:
        owner = owner_of(entry.get("session_key"), multiplexed=multiplexed)
        home = home_of_profile(owner) if owner else None
        if home is None:
            continue  # owner not on this host: leave it where it is
        target = home / "processes.json"
        sid = entry.get("session_id")

        held = []

        def _add(existing, entry=entry, sid=sid, held=held):
            held.append(True)  # the owner's file was read: it now carries the job either way
            if any(isinstance(e, dict) and e.get("session_id") == sid for e in existing):
                return None
            return [*existing, entry]

        try:
            if not target.exists():
                from utils import atomic_json_write
                with _file_lock(target):
                    if not target.exists():
                        atomic_json_write(target, [])
            _rewrite(target, _add)
            if held:
                moved.add(sid)
        except Exception:
            logger.debug("Could not hand %s to %s", sid, target, exc_info=True)
    if moved:
        _rewrite(source, lambda existing: [e for e in existing
                                           if not (isinstance(e, dict) and e.get("session_id") in moved)])
        logger.info("Handed %d process(es) owned by another profile out of %s: %s",
                    len(moved), source, ", ".join(sorted(moved)))
    return len(moved)


def consolidate_checkpoints(*, multiplexed: bool) -> int:
    """Startup pass of a multiplexed gateway, before any profile recovers: drop finished jobs from every
    profile's checkpoint and move each other job into its owner's, so each profile's recovery sees exactly
    its own jobs whatever order the profiles recover in. Returns how many entries left a file."""
    from hermes_constants import profile_name_for_home
    if _holding_a_checkpoint_lock():
        return 0
    changed = 0
    for home in profile_homes():
        path = home / "processes.json"
        try:
            entries = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else []
        except (OSError, ValueError):
            continue
        if not isinstance(entries, list):
            continue
        here = profile_name_for_home(home)
        finished = {e.get("session_id") for e in entries
                    if isinstance(e, dict) and result_recorded(e.get("session_id"))}
        if finished:
            changed += len(finished)
            _rewrite(path, lambda existing, finished=finished: [
                e for e in existing if not (isinstance(e, dict) and e.get("session_id") in finished)])
        foreign = [e for e in entries if isinstance(e, dict) and e.get("session_id") not in finished
                   and not owned_here(e.get("session_key"), multiplexed=multiplexed, scope=here)]
        changed += hand_to_owners(foreign, path, multiplexed=multiplexed)
    if changed:
        logger.info("Process checkpoints: settled or re-homed %d entr%s across profiles",
                    changed, "y" if changed == 1 else "ies")
    return changed
