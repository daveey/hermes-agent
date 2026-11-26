"""This machine's load, reported to the Board's machine registry (Desktop Runners panel and worker-first placement).

``collect()`` reads CPU load, memory in use, free disk and whole-host ``active_card_runs`` with a few cheap OS
calls; the kanban sync loop sends it with ``report_due()`` at most every :data:`REPORT_EVERY_S`. Not display-only
since worker-first-v1 (docs/worker-first-v1.md): the Board's worker-pool admission scores the reported sample, and
``kanban_worker_placement.local_admission`` takes a fresh ``collect()`` before turning a policy card's grant into a
local run. Cards without ``placement_policy`` are still placed without it.

Hub overflow: on a host whose installation-root config sets ``kanban.admission.cpu_busy_below_pct`` the reporter
also attaches the complete triple ``cpu_busy_pct``/``cpu_busy_window_ms``/``cpu_busy_sample_end_ms`` measured by the
sync loop's advisory sampler thread (``machine_cpu_sampler``: rolling ~1 s windows, off the loop thread; a triple
ends at most 1.5 s before the report takes it, inside the Board's 2 s window).
``collect()`` itself never samples CPU busy. A CPU-bearing report has a hard 2 s total transport budget and is never
replayed: each sample is reported at most once, and a failed round drops it.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

_log = logging.getLogger(__name__)

REPORT_EVERY_S = 10.0
# A Board without the route (older deploy) answers 404/405: ask again rarely, not every 10 s.
UNSUPPORTED_RETRY_S = 600.0
# The anchored sender waits at most this long for the advisory sampler's next sample when it has none fresh (a stalled
# process restarts the sampler's window: its next sample is one ~1 s window away). Never on the loop thread.
CPU_WAIT_S = 1.5
# While the root CPU gate is on, a report without the CPU triple would erase the Board row's triple: it goes out only
# as a keepalive, when no report was sent for this long (well inside the Board's 90 s stale-signal limit).
KEEPALIVE_S = 30.0
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
            from hermes_cli.machine_memory_darwin import read_used_pct
            return read_used_pct()
    except (OSError, ValueError):
        return None
    return None


# One statement, one result row, whatever the column or running-row count. In the gateway every sqlite3 call and row
# step releases the GIL and must win it back from busy Python threads (~one 5 ms switch interval each under
# contention); iterating ``PRAGMA table_info`` and the running rows cost 95 sqlite steps for one board (measured: the
# hub's 55-column ``tasks``, 22-column ``task_runs``, 8 running rows; this statement makes 4), seconds over the hub's
# boards, so the final hub gate (which then ran this census twice under admission.lock) overran its 2.25 s hold.
_RECEIPTS = ("json_group_array(json_array(t.id, t.current_run_id, r.admission_pool, r.admission_class, "
             "r.admission_origin)) FROM tasks t LEFT JOIN task_runs r ON r.id = t.current_run_id AND r.task_id = t.id "
             "WHERE t.status = 'running'")
_SYNCED_SQL = (f"SELECT (SELECT gateway_id FROM kanban_sync_state LIMIT 1), (SELECT {_RECEIPTS} AND "
               "(t.claim_lock IS NOT NULL OR t.lease_holder IS NULL "
               "OR t.lease_holder = (SELECT gateway_id FROM kanban_sync_state LIMIT 1)))")
_LOCAL_SQL = f"SELECT (SELECT count(*) FROM pragma_table_info('tasks') WHERE name = 'lease_holder'), (SELECT {_RECEIPTS})"


def _running_rows(db) -> Optional[tuple[bool, Any, list]]:
    """``(synced, own gateway id, [(task_id, run_id, pool, class, origin), ...])`` for the running rows this host
    counts, read in one statement; ``None`` when the board's schema predates claim receipts, when this SQLite lacks
    JSON1 (``no such function: json_group_array``), or when it has lease holders without sync state (read it the long
    way, which decides those exactly as before)."""
    for synced, sql in ((True, _SYNCED_SQL), (False, _LOCAL_SQL)):
        try:
            head, rows = db.execute(sql).fetchone()
        except sqlite3.OperationalError as exc:
            # "no such table/column" (pre-receipt schema, unsynced board) or "no such function" (no JSON1): both
            # fall through to the long path, which needs neither.
            if str(exc).startswith("no such "):
                continue
            raise
        if not synced and head:
            return None
        return synced, head if synced else None, [tuple(item) for item in json.loads(rows)]
    return None


def table_columns(db, table: str) -> set:
    """Column names of ``table`` in ONE result row (empty set when absent).

    [daveey] Runs under admission.lock in the gateway. CPython's sqlite3 drops the GIL around every row step, and
    in a process with a GIL-bound thread each re-acquire waits out the switch interval (~5 ms). ``PRAGMA
    table_info`` returns one row per column (55 for the hub's ``tasks``, 22 for ``task_runs``), so per-column steps
    made one board's census 95 sqlite steps (see ``_RECEIPTS``; a hub probe beside one busy thread took 4.2 s instead
    of 20 ms, and every hub claim overran its hold). ``table`` is a fixed identifier from our own code, never user
    input."""
    row = db.execute("SELECT group_concat(name, char(31)) FROM pragma_table_info(?)", (table,)).fetchone()
    return set(row[0].split("\x1f")) if row and row[0] else set()


def active_card_runs(exclude=None, detail: Optional[dict] = None, *, owner_output=None, extra_paths=(),
                     board_paths=None) -> Optional[int]:
    """All profiles share the kanban root. Count its execution boards, not replicas.

    Unreadable/schema-unknown boards invalidate the whole sample. Conservative
    stale local runs consume slots until the normal reaper accounts for them.

    ``exclude`` (final admission only, a :class:`kanban_grant_echo.GrantEcho`): on the board whose file identity is
    ``exclude.db_identity``, the ONE row that is still exactly the echo of the claimer's own unused grant is not
    counted (review F5); it is re-proven here on this read connection, and more than one match fails closed.
    ``detail`` receives ``{"excluded": n}``. ``board_paths`` is an authoritative
    inventory for account worker admission; no current-home discovery is added.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_boards import named_board_db_files

    try:
        default = kb.kanban_home() / "kanban.db" if board_paths is None else None
        paths = [default, *(p for _, p in named_board_db_files()), *extra_paths] if board_paths is None else board_paths
        total = excluded = 0
        owners = set()
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
            # ``absolute()`` not ``resolve()``: realpath lstat()s every path component, each a GIL handoff in the
            # gateway; sqlite follows a symlinked board file itself, and identity comes from the stat above.
            db = sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=1)
            try:
                # Count, own-grant matching and receipt grouping must see one WAL snapshot.
                db.execute("BEGIN")
                own, matched, receipts = None, [], None
                if (fast := _running_rows(db)) is not None:
                    synced, own_id, rows = fast
                    if synced and not own_id:
                        return None
                    own = (own_id,) if synced else None
                    ids = {row[0] for row in rows}
                    total += len(rows)
                    receipts = [row[1:] for row in rows]
                elif "lease_holder" in table_columns(db, "tasks"):
                    own = db.execute("SELECT gateway_id FROM kanban_sync_state LIMIT 1").fetchone()
                    if own is None or not own[0]:
                        return None
                    rows = db.execute("SELECT id FROM tasks WHERE status='running' AND "
                                        "(claim_lock IS NOT NULL OR lease_holder IS NULL OR lease_holder=?)",
                                        (own[0],)).fetchall()
                    ids = {row[0] for row in rows}
                    total += len(rows)
                else:
                    ids = {row[0] for row in db.execute("SELECT id FROM tasks WHERE status='running'")}
                    total += len(ids)
                if own is not None and exclude is not None and identity == tuple(exclude.db_identity) \
                        and own[0] == exclude.gateway_id:
                    from hermes_cli.kanban_grant_echo import match_rows
                    matched = match_rows(db, exclude)
                    if matched and matched != [exclude.run_id]:
                        return None  # the proven row moved or duplicated under us: unknown, fail closed
                    total -= len(matched)
                    excluded += len(matched)
                    if matched:
                        ids.discard(exclude.task_id)
                if detail is not None and "pools" in detail:
                    from hermes_cli.kanban_admission_pools import board_pools, count_pools
                    origins = detail.get("capacity_origins")
                    counts = count_pools(receipts, excluded=matched, origins=origins) if receipts is not None \
                        else board_pools(db, own=own[0] if own else None, excluded=matched, origins=origins)
                    for pool, count in counts.items():
                        detail["pools"][pool] += count
                if owner_output is not None:
                    from hermes_cli.worker_compute_capacity import owner_for_db
                    owners.update(owner_for_db(path, task) for task in ids)
            finally:
                db.close()
        if detail is not None:
            detail["excluded"] = excluded
        if owner_output is not None:
            owner_output.update(owners)
        return total
    except (OSError, sqlite3.Error, ValueError):
        return None


# The sync loop's Reporter names its census (machine_report_census: kept readers, reused while a board's
# data_version is unchanged) for the collect() it runs on this thread; every other caller reads fresh connections.
_REPORT = threading.local()


def collect(disk_path: Optional[Path] = None) -> dict[str, Any]:
    """``{cpu_load1, cpus, mem_used_pct, disk_free_gb, disk_total_gb}`` plus the throughput counters
    (``machine_throughput_metrics``); a value that cannot be read is left out."""
    from hermes_cli import machine_admission_metrics, machine_drain_metrics, machine_throughput_metrics
    from hermes_cli import worker_compute_capacity as capacity
    from hermes_cli.worker_resource_admission import occupied_count_for_host
    # Inclusive, fixed-name diagnostics overlap report.collect; never add them to step/thread CPU.
    from hermes_cli.kanban_sync_cpu import detail
    out: dict[str, Any]
    try:
        worker = detail("collect.worker", capacity.worker_enabled)
    except Exception:  # health: allow BLE001 -- malformed worker observations must report unknown capacity
        worker = None
    if worker is None:
        out = {"active_card_runs": None}
    elif worker:
        # [daveey] Worker occupancy includes parked owners' detached cgroups; hub pool telemetry does not apply.
        try:
            out = {"active_card_runs": detail("collect.occupancy", occupied_count_for_host)}
        except Exception:  # health: allow BLE001 -- malformed worker observations must report unknown capacity
            out = {"active_card_runs": None}
    else:
        out = detail("collect.admission", machine_admission_metrics.collect,
                     getattr(_REPORT, "census", None) or active_card_runs)
    from hermes_cli import machine_persistent_jobs
    out.update(detail("collect.persistent", machine_persistent_jobs.collect))
    out.update(detail("collect.drain", machine_drain_metrics.collect))
    from hermes_cli.kanban_sync_claims import claiming_bit
    out["claiming"] = detail("collect.claiming", claiming_bit)
    out.update(detail("collect.throughput", machine_throughput_metrics.cached_counters))
    try:
        out["cpu_load1"] = round(detail("collect.load", os.getloadavg)[0], 2)
    except (OSError, AttributeError):
        pass
    if detail("collect.cpus", os.cpu_count):
        out["cpus"] = int(detail("collect.cpus", os.cpu_count))
    mem = detail("collect.memory", _mem_used_pct)
    if mem is not None:
        out["mem_used_pct"] = mem
    try:
        if disk_path is None:
            from hermes_cli import kanban_db
            disk_path = kanban_db.kanban_home()
        usage = detail("collect.disk", shutil.disk_usage, disk_path)
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
    (``kanban_admission.hub_cpu_advisory``) and the locked final gate (``kanban_admission.sampler_sample``)."""
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
                 cpu_take_fn: Optional[Callable[[Any], Optional[dict]]] = None) -> None:
        self.send, self.collect_fn, self.clock = send, collect_fn, clock
        self.cpu_limit_fn = cpu_limit_fn
        # The sync loop sets this to its AdvisorySampler.take (machine_cpu_sampler): the CPU triple is measured on
        # the sampler's own thread, never on the loop, so a slow round cannot void it. None reports no CPU triple.
        self.cpu_take_fn = cpu_take_fn
        self.next_at = 0.0
        self.counters_off_until = 0.0  # a Board without throughput counters 400s them: skip until then
        self.claiming_off_until = 0.0  # a Board that rejects claiming: skip it so the next report can keep CPU
        # The sync loop (report_due) and the anchored sender thread (report_tick, machine_metrics_sender) share one
        # Reporter: one report at a time (non-blocking), and report starts at least REPORT_EVERY_S / 2 apart (the
        # loop's next_at is always a full period after the latest start; the sender checks started_at).
        self._lock = threading.Lock()
        self.started_at: Optional[float] = None
        # The sender path's bounded wait for a fresh sample (the sync loop sets AdvisorySampler.wait_fresh).
        self.cpu_wait_fn: Optional[Callable[[float], Any]] = None
        self.sent_at: Optional[float] = None  # the latest report actually sent (either path)
        self.suppressed = 0  # CPU-less reports withheld while the gate was on
        self.census_fn: Optional[Callable[..., Optional[int]]] = None  # the loop's kept-reader census, else fresh

    def _sample(self, sample: dict, measured: Optional[dict]) -> dict:
        from hermes_cli import kanban_sync_timing as timing
        from hermes_cli import machine_cpu_metrics as cpu
        timing.note_cpu_sample(measured)
        if measured and all(k in measured for k in cpu.FIELDS):
            sample.update({k: measured[k] for k in cpu.FIELDS})
        if "admission_version" in sample:
            from hermes_cli import machine_admission_metrics as admission
            current = admission.policy()
            if not current or any(sample.get(key) != value for key, value in current.items()):
                for key in admission.FIELDS:
                    sample.pop(key, None)
        return sample

    def _spaced(self, now: float) -> bool:
        return self.started_at is None or now - self.started_at >= REPORT_EVERY_S / 2

    def report_due(self, send: Optional[Callable[[dict], Any]] = None) -> bool:
        """The sync loop's report (through *send*, default the Reporter's own): True sent; False not due, failed, or
        withheld (a CPU-less report while the gate is on and a report was sent within KEEPALIVE_S).

        ``sync.detail/<step>.due`` is this not-due decision, including a lock miss.
        Always-on ``outer=False``; not an opt-in timing span.
        """
        from hermes_cli import kanban_sync_cpu as sync_cpu
        gate = {"held": False, "now": None}

        def decide() -> bool:
            if not self._lock.acquire(blocking=False):
                return False  # the sender thread is reporting right now
            gate["held"] = True
            now = self.clock()
            if now < self.next_at:
                return False
            self.next_at = now + REPORT_EVERY_S
            gate["now"] = now
            return True

        try:
            if not sync_cpu.detail("due", decide):
                return False
            return self._report(gate["now"], send or self.send) is True
        finally:
            if gate["held"]:
                self._lock.release()

    def report_tick(self, send: Callable[[dict], Any]) -> Optional[bool]:
        """The anchored sender's report through its own transport *send*: True sent, False failed, None skipped.

        Due rule: the caller's anchor itself, plus one guard: skip while another report is in flight, while a
        report (on either path) started less than REPORT_EVERY_S / 2 ago, or during the Board's 404/405 backoff.
        Unlike report_due it never compares against ``next_at = entry + period``, which would drop a tick that woke
        earlier relative to its anchor than the tick before it."""
        if not self._lock.acquire(blocking=False):
            return None
        try:
            now = self.clock()
            if self.next_at - now > REPORT_EVERY_S or not self._spaced(now):
                return None
            next_at = self.next_at
            self.next_at = now + REPORT_EVERY_S
            result = self._report(now, send, wait=True)
            if result is None:
                self.next_at = next_at  # nothing was sent: the loop's own due time is unchanged
            return result
        finally:
            self._lock.release()

    def _report(self, now: float, send: Callable[[dict], Any], wait: bool = False) -> Optional[bool]:
        """Collect, attach the CPU triple and send. None: withheld (CPU-less while the gate is on, see KEEPALIVE_S).

        ``sync.detail/<step>.collect`` times ``collect()`` only. ``.send`` times the
        send callable only (each attempt). Always-on ``outer=False``, not timing.span.
        """
        try:
            from hermes_cli import kanban_sync_cpu as sync_cpu
            from hermes_cli import kanban_sync_timing as timing
            from hermes_cli import machine_cpu_metrics as cpu
            from hermes_cli.machine_drain_metrics import send_compatible
            with timing.span("collect"):
                _REPORT.census = self.census_fn
                try:
                    sample = dict(sync_cpu.detail("collect", self.collect_fn or collect))
                finally:
                    _REPORT.census = None
            with timing.span("limit"):
                state, limit = (self.cpu_limit_fn or cpu.root_limit)()
            measured = None
            if state == cpu.ON and self.cpu_take_fn is not None:
                with timing.span("cpu"):  # nonblocking: the sampler's newest unreported sample
                    measured = self.cpu_take_fn(limit)
                if measured is None and wait and self.cpu_wait_fn is not None:
                    # [daveey] hermes-hub 2026-11-12 13:42:12 / 13:43:27: the tick ran ~0.3 s after a process-wide stall
                    # ended, before the sampler's restarted window closed, and sent the row CPU-less. Wait (bounded).
                    if self.cpu_wait_fn(CPU_WAIT_S):
                        measured = self.cpu_take_fn(limit)
            if state == cpu.ON and not (measured and all(k in measured for k in cpu.FIELDS)):
                if self.sent_at is not None and self.clock() - self.sent_at < KEEPALIVE_S:
                    self.suppressed += 1
                    timing.note_cpu_sample(measured)
                    return None  # withheld: it would erase the Board row's CPU triple
            sample = self._sample(sample, measured)
            if now < self.counters_off_until:  # this Board rejected them lately: the normal report only
                from hermes_cli.machine_throughput_metrics import FIELDS
                sample = {k: v for k, v in sample.items() if k not in FIELDS and k != "tokens_by_model"}
            if now < self.claiming_off_until:
                sample.pop("claiming", None)

            def counters_rejected() -> None:
                self.counters_off_until = now + UNSUPPORTED_RETRY_S

            def claiming_rejected() -> None:
                self.claiming_off_until = now + UNSUPPORTED_RETRY_S

            def send_once(payload):
                return sync_cpu.detail("send", send, payload)

            self.started_at = now
            with timing.span("send"):  # preserve the transport's existing hard 2 s budget
                send_compatible(send_once, sample, counters_rejected, claiming_rejected)
            self.sent_at = self.clock()  # a failed send left the row as it was: it is no keepalive
            return True
        except Exception as exc:  # health: allow BLE001 -- load reporting must never stop kanban sync
            status = getattr(exc, "status", None)
            if status in (404, 405):
                self.next_at = now + UNSUPPORTED_RETRY_S
            _log.debug("machine metrics not reported (%s: %s)", type(exc).__name__, exc)
            return False
