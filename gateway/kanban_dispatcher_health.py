"""Bounded native-loop evidence, never dispatch_once observer evidence.

Linux readers verify kernel flock ownership, process start identity and expiry.
Other platforms fail closed. A successful pass is not a delivery guarantee.
"""
from __future__ import annotations
import json
import math
import os
from pathlib import Path
import time
from gateway.status import get_process_start_time


def _owns_lock(path: Path, pid: int) -> bool:
    """Require this process, not merely somebody, to hold the native flock."""
    stat = path.stat()
    identity = f"{os.major(stat.st_dev):02x}:{os.minor(stat.st_dev):02x}:{stat.st_ino}"
    # /proc/locks is a host-wide, multi-read snapshot: unrelated lock churn
    # can shift entries between reads and omit our still-held lock. fdinfo
    # observes only this process's descriptor, including its actual lock owner.
    for fd in Path(f"/proc/{pid}/fd").iterdir():
        try:
            opened = fd.stat()
            if (opened.st_dev, opened.st_ino) != (stat.st_dev, stat.st_ino):
                continue
            info = Path(f"/proc/{pid}/fdinfo/{fd.name}").read_text(encoding="utf-8")
        except OSError:
            continue  # descriptors may close while being enumerated
        for line in info.splitlines():
            fields = line.split()
            if (len(fields) >= 7 and fields[0] == "lock:"
                    and fields[2:5] == ["FLOCK", "ADVISORY", "WRITE"]
                    and fields[5] == str(pid) and fields[6] == identity):
                return True
    return False


def available(root: Path, board: Path, profile: str, lane: str) -> bool:
    """Read-only exact scope; root is the native singleton lock directory."""
    try:
        root = Path(root).resolve()
        record = json.loads((root / ".dispatcher-health.json").read_text(encoding="utf-8-sig"))
        age = time.monotonic() - record["stamp"]
        if not (record["version"] == 1 and 0 <= age <= record["ttl"] <= 300):
            return False
        if record["boot_id"] != Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip():
            return False
        pid = record["pid"]
        if (record["started_at"] is None
                or get_process_start_time(pid) != record["started_at"]
                or not _owns_lock(root / ".dispatcher.lock", pid)):
            return False
        if not record["pause_paths"] or any(Path(p).exists() for p in record["pause_paths"]):
            return False
        return any(isinstance(profiles, list) and isinstance(lanes, list)
                   and 0 <= time.monotonic() - observed_at <= record["ttl"]
                   and path == str(Path(board).resolve()) and profile in profiles and lane in lanes
                   for path, profiles, lanes, observed_at in record["boards"])
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return False


class DispatcherHealth:
    """Owned by the watcher coroutine; worker threads cannot publish.

    Each cycle replaces all scope. Invalidate before work so hung, failed and
    skipped boards cannot retain the preceding successful receipt.
    """
    def __init__(self, lock_handle, *, interval: float):
        self.handle = lock_handle
        self.path = Path(lock_handle.name).parent / ".dispatcher-health.json" if lock_handle else None
        self.ttl = min(300.0, max(5.0, 2 * interval + 5)) if math.isfinite(interval) else 5.0
        self.closed = False
        self.invalidate()

    def _write(self, record: dict) -> None:
        if self.path is None:
            return
        temp = self.path.with_suffix(".tmp")
        try:
            with temp.open("w", encoding="utf-8") as stream:
                json.dump(record, stream)
            temp.replace(self.path)
        except OSError:
            from gateway.kanban_watchers_common import logger
            logger.warning("kanban dispatcher health: cannot write receipt", exc_info=True)

    def invalidate(self) -> None:
        self._write({})

    def publish(self, boards: list) -> None:
        if self.closed or self.handle is None or self.handle.closed:
            return
        from agent.estop import _candidate_sentinel_paths
        pid = os.getpid()
        try:
            if not _owns_lock(Path(self.handle.name), pid):
                return
            boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
            pause_paths = [str(p.resolve()) for p in _candidate_sentinel_paths()]
            if any(Path(p).exists() for p in pause_paths):
                return
        except OSError:
            return
        self._write(dict(version=1, pid=pid, boot_id=boot_id, started_at=get_process_start_time(pid),
                         stamp=time.monotonic(), ttl=self.ttl, boards=boards,
                         pause_paths=pause_paths))

    def close(self) -> None:
        self.closed = True
        self.invalidate()
