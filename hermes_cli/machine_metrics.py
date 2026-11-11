"""This machine's load, reported to the Board's machine registry (Desktop Runners panel and worker-first placement).

``collect()`` reads CPU load, memory in use, free disk and whole-host ``active_card_runs`` with a few cheap OS
calls; the kanban sync loop sends it with ``report_due()`` at most every :data:`REPORT_EVERY_S`. Not display-only
since worker-first-v1 (docs/worker-first-v1.md): the Board's worker-pool admission scores the reported sample, and
``kanban_worker_placement.local_admission`` takes a fresh ``collect()`` before turning a policy card's grant into a
local run. Cards without ``placement_policy`` are still placed without it.

Hub overflow: on a host whose installation-root config sets ``kanban.admission.cpu_busy_below_pct`` the reporter
also takes a measured ~1 s CPU busy sample (``machine_cpu_metrics.sample``) immediately before each send and attaches
the complete triple ``cpu_busy_pct``/``cpu_busy_window_ms``/``cpu_busy_sample_end_ms``. ``collect()`` itself never
samples CPU busy. A CPU-bearing report has a hard 2 s total transport budget and is never replayed: a failed round
drops its sample and the next round measures again.
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

REPORT_EVERY_S = 10.0
# A Board without the route (older deploy) answers 404/405: ask again rarely, not every 10 s.
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


def active_card_runs(exclude=None, detail: Optional[dict] = None) -> Optional[int]:
    """All profiles share the kanban root. Count its execution boards, not replicas.

    Unreadable/schema-unknown boards invalidate the whole sample. Conservative
    stale local runs consume slots until the normal reaper accounts for them.

    ``exclude`` (final admission only, a :class:`kanban_grant_echo.GrantEcho`): on the board whose file identity is
    ``exclude.db_identity``, the ONE row that is still exactly the echo of the claimer's own unused grant is not
    counted (review F5); it is re-proven here on this read connection, and more than one match fails closed.
    ``detail`` receives ``{"excluded": n}``.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_boards import named_board_db_files

    try:
        default = kb.kanban_home() / "kanban.db"
        paths = [default, *(p for _, p in named_board_db_files())]
        total = excluded = 0
        seen = set()
        for path in paths:
            try:
                stat = path.stat()
            except FileNotFoundError:
                # [daveey] A never-created default board holds no runs. Named boards are listed only when present,
                # so a vanished one still invalidates the sample (fails closed) like any other unreadable board.
                if path == default:
                    continue
                raise
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
                    if exclude is not None and identity == tuple(exclude.db_identity) and own[0] == exclude.gateway_id:
                        from hermes_cli.kanban_grant_echo import match_rows
                        matched = match_rows(db, exclude)
                        if matched and matched != [exclude.run_id]:
                            return None  # the proven row moved or duplicated under us: unknown, fail closed
                        total -= len(matched)
                        excluded += len(matched)
                else:
                    total += db.execute("SELECT count(*) FROM tasks WHERE status='running'").fetchone()[0]
            finally:
                db.close()
        if detail is not None:
            detail["excluded"] = excluded
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


HUB_CPU_SAMPLE_FILE = "hub-cpu-sample.json"


def _hub_cpu_path(root: Optional[Path] = None) -> Path:
    if root is None:
        from hermes_constants import get_default_hermes_root
        root = Path(get_default_hermes_root())
    return Path(root) / "cache" / HUB_CPU_SAMPLE_FILE


def record_hub_cpu_sample(sample: dict, root: Optional[Path] = None) -> None:
    """Keep the reporter's latest complete CPU triple for the lock-free advisory pre-check
    (``kanban_admission.hub_cpu_advisory``). Advisory only: the final gate always measures again under the lock."""
    import json
    path = _hub_cpu_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({k: sample[k] for k in ("cpu_busy_pct", "cpu_busy_window_ms",
                                                       "cpu_busy_sample_end_ms")}), encoding="utf-8")
    os.replace(tmp, path)


def read_hub_cpu_sample(root: Optional[Path] = None) -> Optional[dict]:
    import json
    try:
        doc = json.loads(_hub_cpu_path(root).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


class Reporter:
    """Sends ``collect()`` to the Board at most every REPORT_EVERY_S; never raises into the sync loop."""

    def __init__(self, send: Callable[[dict], Any], *, collect_fn: Optional[Callable[[], dict]] = None,
                 clock: Callable[[], float] = time.monotonic,
                 cpu_limit_fn: Optional[Callable[[], tuple]] = None,
                 cpu_sample_fn: Optional[Callable[[], Optional[dict]]] = None) -> None:
        self.send, self.collect_fn, self.clock = send, collect_fn, clock
        self.cpu_limit_fn, self.cpu_sample_fn = cpu_limit_fn, cpu_sample_fn
        self.next_at = 0.0

    def _sample(self) -> dict:
        from hermes_cli import kanban_sync_timing as timing
        from hermes_cli import machine_cpu_metrics as cpu
        with timing.span("collect"):
            sample = dict((self.collect_fn or collect)())
        with timing.span("limit"):
            state, _limit = (self.cpu_limit_fn or cpu.root_limit)()
        if state == cpu.ON:  # only the gated hub pays the ~1 s measurement, taken last: right before sending
            with timing.span("cpu"):
                measured = (self.cpu_sample_fn or cpu.sample)()
            timing.note_cpu_sample(measured)
            if measured and all(k in measured for k in cpu.FIELDS):
                sample.update({k: measured[k] for k in cpu.FIELDS})
                try:
                    with timing.span("adv"):
                        record_hub_cpu_sample(measured)
                except OSError as exc:
                    _log.debug("hub CPU advisory sample not recorded: %s", exc)
        return sample

    def report_due(self) -> bool:
        now = self.clock()
        if now < self.next_at:
            return False
        self.next_at = now + REPORT_EVERY_S
        try:
            from hermes_cli import kanban_sync_timing as timing
            from hermes_cli.machine_drain_metrics import send_compatible
            sample = self._sample()
            with timing.span("send"):  # the whole send: the private bounded transport, its join and its timeout
                send_compatible(self.send, sample)
            return True
        except Exception as exc:  # health: allow BLE001 -- load reporting must never stop kanban sync
            status = getattr(exc, "status", None)
            if status in (404, 405):
                self.next_at = now + UNSUPPORTED_RETRY_S
            _log.debug("machine metrics not reported (%s: %s)", type(exc).__name__, exc)
            return False
