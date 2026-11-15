"""Boot-scoped, persisted awake observations of sendable outbox identities. [daveey]

Like replica-age, resume within one boot only across bounded observation gaps.
No op (including an unreadable outbox) ends the observation. Wall age is diagnostic,
never a substitute for observing the same op. The caller serializes read/observe/claim.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from hermes_cli import kanban_sync_replica_age as replica_age

log = logging.getLogger(__name__)
LOCK = threading.RLock()
MAX_GAP_S = 600.0
_SEEN: dict[str, Observation] = {}


@dataclass
class Observation:
    op_id: str
    since: float
    seen_at: float
    boot: Optional[str]
    filed: bool = False
    warned_at: Optional[float] = None


def _path(path: Path) -> Path:
    return path.with_suffix(".outbox-age.json")


def _load(path: Path, now: float, boot: Optional[str]) -> Optional[Observation]:
    try:
        rec = json.loads(_path(path).read_text(encoding="utf-8-sig"))
        seen = Observation(**rec)
        if (boot is not None and seen.boot == boot and isinstance(seen.op_id, str)
                and 0 <= seen.since <= seen.seen_at <= now
                and now - seen.seen_at <= MAX_GAP_S
                and type(seen.filed) is bool
                and (seen.warned_at is None or seen.since <= seen.warned_at <= seen.seen_at)):
            return seen
    except (OSError, ValueError, TypeError):
        pass
    return None


def remember(path: Path, seen: Optional[Observation]) -> None:
    target = _path(path)
    try:
        if seen is None:
            target.unlink(missing_ok=True)
            return
        temp = target.with_name(f"{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        temp.write_text(json.dumps(asdict(seen)), encoding="utf-8")
        temp.replace(target)
    except OSError:
        log.debug("kanban sync: cannot persist outbox observation at %s", target, exc_info=True)


def observe(path: Path, op_id: Optional[str], now: float) -> Optional[Observation]:
    key = str(path.resolve())
    boot = replica_age._boot_id()
    seen = _SEEN.get(key)
    if seen is None:
        seen = _load(path, now, boot)
    if op_id is None:
        _SEEN.pop(key, None)
        remember(path, None)
        return None
    if (seen is None or seen.op_id != op_id or seen.boot != boot
            or not seen.seen_at <= now or now - seen.seen_at > MAX_GAP_S):
        seen = Observation(op_id, now, now, boot)
    seen.seen_at = now
    _SEEN[key] = seen
    remember(path, seen)
    return seen


def reset() -> None:
    with LOCK:
        _SEEN.clear()
