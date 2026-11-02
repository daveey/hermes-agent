"""This machine's load, reported to the Board's machine registry for display (Desktop Runners panel).

``collect()`` reads CPU load, memory in use and free disk with a few cheap OS calls; the kanban sync loop
sends it with ``report_due()`` at most every :data:`REPORT_EVERY_S`. Display only: placement never reads it.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

_log = logging.getLogger(__name__)

REPORT_EVERY_S = 30.0
# A Board without the route (older deploy) answers 404/405: ask again rarely, not every 30 s.
UNSUPPORTED_RETRY_S = 600.0
_GB = 1024 ** 3


def _linux_mem_used_pct(meminfo: str) -> Optional[float]:
    values = {k: int(v) for k, v in re.findall(r"^(\w+):\s+(\d+) kB", meminfo, re.MULTILINE)}
    total, avail = values.get("MemTotal"), values.get("MemAvailable")
    if not total or avail is None:
        return None
    return round(100.0 * (total - avail) / total, 1)


def _macos_mem_used_pct(memsize: int, vm_stat: str) -> Optional[float]:
    page = re.search(r"page size of (\d+) bytes", vm_stat)
    pages = {k.strip().lower(): int(v) for k, v in re.findall(r"^([^:\n]+):\s+(\d+)\.", vm_stat, re.MULTILINE)}
    if not page or not memsize:
        return None
    # Activity Monitor's "available": free + inactive + speculative + purgeable pages.
    avail = sum(pages.get(k, 0) for k in ("pages free", "pages inactive", "pages speculative", "pages purgeable"))
    return round(max(0.0, min(100.0, 100.0 * (memsize - avail * int(page.group(1))) / memsize)), 1)


def _mem_used_pct() -> Optional[float]:
    try:
        if sys.platform.startswith("linux"):
            return _linux_mem_used_pct(Path("/proc/meminfo").read_text(encoding="utf-8"))
        if sys.platform == "darwin":
            def out(argv: list[str]) -> str:
                return subprocess.run(argv, capture_output=True, timeout=5, check=True).stdout.decode("utf-8", "replace")
            memsize = int(out(["/usr/sbin/sysctl", "-n", "hw.memsize"]).strip())
            vm = out(["/usr/bin/vm_stat"])
            return _macos_mem_used_pct(memsize, vm)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return None


def active_card_runs() -> Optional[int]:
    """All profiles share the kanban root. Count its execution boards, not replicas.

    Unreadable/schema-unknown boards invalidate the whole sample. Conservative
    stale local runs consume slots until the normal reaper accounts for them.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_boards import named_board_db_files

    try:
        paths = [kb.kanban_home() / "kanban.db", *(p for _, p in named_board_db_files())]
        total = 0
        seen = set()
        for path in paths:
            stat = path.stat()
            identity = (stat.st_dev, stat.st_ino)
            if identity in seen:
                continue
            seen.add(identity)
            db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
            try:
                cols = {row[1] for row in db.execute("PRAGMA table_info(tasks)")}
                if "lease_holder" in cols:
                    own = db.execute("SELECT gateway_id FROM kanban_sync_state LIMIT 1").fetchone()
                    if own is None or not own[0]:
                        return None
                    total += db.execute("SELECT count(*) FROM tasks WHERE status='running' AND "
                                        "(claim_lock IS NOT NULL OR lease_holder IS NULL OR lease_holder=?)",
                                        (own[0],)).fetchone()[0]
                else:
                    total += db.execute("SELECT count(*) FROM tasks WHERE status='running'").fetchone()[0]
            finally:
                db.close()
        return total
    except (OSError, sqlite3.Error):
        return None


def collect(disk_path: Optional[Path] = None) -> dict[str, Any]:
    """``{cpu_load1, cpus, mem_used_pct, disk_free_gb, disk_total_gb}``; a value that cannot be read is left out."""
    out: dict[str, Any] = {"active_card_runs": active_card_runs()}
    from hermes_cli import machine_drain_metrics
    out.update(machine_drain_metrics.collect())
    try:
        out["cpu_load1"] = round(os.getloadavg()[0], 2)
    except (OSError, AttributeError):
        pass
    if os.cpu_count():
        out["cpus"] = int(os.cpu_count())
    mem = _mem_used_pct()
    if mem is not None:
        out["mem_used_pct"] = mem
    try:
        if disk_path is None:
            from hermes_cli import kanban_db
            disk_path = kanban_db.kanban_home()
        usage = shutil.disk_usage(disk_path)
        out["disk_free_gb"] = round(usage.free / _GB, 1)
        out["disk_total_gb"] = round(usage.total / _GB, 1)
    except (OSError, ImportError):
        pass
    return out


class Reporter:
    """Sends ``collect()`` to the Board at most every REPORT_EVERY_S; never raises into the sync loop."""

    def __init__(self, send: Callable[[dict], Any], *, collect_fn: Optional[Callable[[], dict]] = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.send, self.collect_fn, self.clock = send, collect_fn, clock
        self.next_at = 0.0

    def report_due(self) -> bool:
        now = self.clock()
        if now < self.next_at:
            return False
        self.next_at = now + REPORT_EVERY_S
        try:
            from hermes_cli.machine_drain_metrics import send_compatible
            send_compatible(self.send, (self.collect_fn or collect)())
            return True
        except Exception as exc:  # health: allow BLE001 -- load reporting must never stop kanban sync
            status = getattr(exc, "status", None)
            if status in (404, 405):
                self.next_at = now + UNSUPPORTED_RETRY_S
            _log.debug("machine metrics not reported (%s: %s)", type(exc).__name__, exc)
            return False
