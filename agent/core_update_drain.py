"""Core-update drain hold: hermes-sync asks for no NEW kanban claims while it drains.

hermes-sync's ``core-drain`` writes ``<home>/core-update/drain-hold`` (JSON ``{"pid", "started_at",
"reason"}``) for every installation root it is about to update, then waits until running workers
finish and no Desktop turn is live, applies ``hermes update --yes`` and removes the file. While the
hold is active the kanban dispatcher spawns nothing (running workers are never touched), so a
saturated board drains instead of refilling between update checks.

A hold only counts while its writer pid is alive: a crashed updater must not freeze dispatch.
A file that cannot be parsed counts for ``_UNPARSEABLE_GRACE_S`` (a writer mid-write), then not.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Optional

from agent.file_safety import _hermes_home_path as _hermes_home, _hermes_root_path as _canonical_root

HOLD_RELPATH = Path("core-update") / "drain-hold"
_UNPARSEABLE_GRACE_S = 600

_log_lock = threading.Lock()
_logged: set[str] = set()


def _pid_alive(pid: int) -> bool:
    import psutil

    return psutil.pid_exists(pid)


def hold_paths() -> list[Path]:
    """Process home first, then the fleet root when it is a different directory."""
    paths = [_hermes_home() / HOLD_RELPATH]
    try:
        root = _canonical_root() / HOLD_RELPATH
        if root.resolve() != paths[0].resolve():
            paths.append(root)
    except Exception:
        pass
    return paths


def read_hold(path: Path, now: Optional[float] = None) -> Optional[dict]:
    """The hold record when ``path`` is an active hold, else None."""
    try:
        raw = path.read_text(encoding="utf-8-sig")
        st = path.stat()
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError:
        return None
    try:
        data = json.loads(raw)
        pid = int(data["pid"])
    except (ValueError, KeyError, TypeError):
        age = (now if now is not None else time.time()) - st.st_mtime
        return {"pid": None, "reason": "unparseable drain hold"} if 0 <= age < _UNPARSEABLE_GRACE_S else None
    if pid <= 0 or not _pid_alive(pid):
        return None
    return data if isinstance(data, dict) else None


def active_hold() -> Optional[dict]:
    for path in hold_paths():
        record = read_hold(path)
        if record is not None:
            return {**record, "path": str(path)}
    return None


def check_drain_hold(component: str, logger: logging.Logger) -> bool:
    """True while a live drain hold exists; logs once per engagement per component."""
    record = active_hold()
    if record is None:
        with _log_lock:
            _logged.discard(component)
        return False
    with _log_lock:
        first = component not in _logged
        _logged.add(component)
    if first:
        logger.info("%s dispatch held for a core update drain (pid %s, %s); running workers continue",
                    component, record.get("pid"), record.get("path"))
    return True
