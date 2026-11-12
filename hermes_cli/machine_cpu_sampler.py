"""The hub's advisory CPU sampler: its own thread, independent of the sync loop's iteration time. [daveey]

The lock-free advisory (``kanban_admission.hub_cpu_advisory``) refuses every hub claim when
``cache/hub-cpu-sample.json`` is older than 15 s. Measuring inside the sync loop tied that freshness to the
loop: the split begin/finish sampler took snapshot A after one round and B after the next, and a hub round of
3-20 s (board pulls plus feed wakes) always put the window past ``MAX_WINDOW_MS``. Every sample was dropped, the
file aged, and the hub claimed nothing for minutes (2026-11-12).

This thread samples continuously with rolling ``/proc/stat`` snapshots while the root limit is on: every
``WINDOW_S`` (1 s) it reads one new snapshot, forms a sample from (previous, new) with
``machine_cpu_metrics.between`` (the same window bounds and arithmetic as ``sample``), and makes the new snapshot
the previous one. A window outside the bounds drops that sample and restarts from the new snapshot. The advisory
file is written at most every :func:`record_every_s` (5 s), always from the newest valid sample.

The metrics report takes the newest sample with :meth:`AdvisorySampler.take`: at most once per sample (a report is
never replayed), only under the same limit, and only while its end stamp is at most :data:`REPORT_MAX_AGE_MS`
before the take. The Board keeps a triple only when ``at - 2000 <= end <= at`` (hermes-hub ``CPU_MAX_DELAY_MS``);
the earlier one-sample-every-5-s cadence made the end up to 5 s old at send and the Board dropped most of them.
The sync loop starts the thread and closes it when the loop ends, so a stopped loop still leaves the advisory to go
stale (fail-closed).
"""
from __future__ import annotations

import logging
from threading import Condition, Event, Lock, Thread, current_thread  # bound now: loop tests stub threading.Thread
import time
from typing import Any, Callable, Optional

_log = logging.getLogger(__name__)

# The advisory file is rewritten every 5 windows (5 s), well inside its 15 s freshness limit.
RECORD_EVERY_WINDOWS = 5
# A reported triple ends at most 1.5 s before the take: the Board's 2 s window leaves 500 ms for the send.
REPORT_MAX_AGE_MS = 1500

# The last state of this process's sampler, for the dispatcher's stale-advisory card (kanban_hub_cpu_alert).
_STATUS: dict[str, Any] = {}


def status() -> dict[str, Any]:
    return dict(_STATUS)


def record_every_s() -> float:
    from hermes_cli import machine_cpu_metrics as cpu
    return RECORD_EVERY_WINDOWS * cpu.WINDOW_S


def _snapshot_now(read: Callable[[], str], clock: Callable[[], float],
                  wall: Callable[[], float]) -> Optional[tuple[tuple[int, int], float, int]]:
    """``(counters, monotonic s, wall end ms)`` for one ``/proc/stat`` read, or None when it is unreadable."""
    from hermes_cli import machine_cpu_metrics as cpu
    try:
        parsed = cpu.parse_proc_stat(read())
        taken, end_ms = clock(), int(wall() * 1000)
    except (OSError, UnicodeError, ValueError):
        return None
    return None if parsed is None else (parsed, taken, end_ms)


class AdvisorySampler:
    def __init__(self, *, limit_fn: Optional[Callable[[], tuple]] = None,
                 read_fn: Optional[Callable[[], str]] = None,
                 record_fn: Optional[Callable[[dict], None]] = None,
                 clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time,
                 wait_fn: Optional[Callable[[float], Any]] = None, linux: Optional[bool] = None) -> None:
        import sys
        self.limit_fn, self.read_fn, self.record_fn = limit_fn, read_fn, record_fn
        self.clock, self.wall = clock, wall
        self.linux = sys.platform.startswith("linux") if linux is None else linux
        self.drops = 0
        self.consecutive_drops = 0
        self.records = 0
        self._lock = Lock()
        self._published = Condition(self._lock)  # notified when a new sample is published
        self._prev: Optional[tuple[tuple[int, int], float, int]] = None  # (counters, monotonic, wall end ms)
        self._latest: Optional[tuple[dict, Any]] = None  # (sample, limit it was measured under)
        self._taken: Optional[tuple] = None
        self._recorded_at: Optional[float] = None  # monotonic time of the last advisory-file write
        self._stop = Event()
        self._wait = wait_fn or self._stop.wait
        self._thread: Optional[Thread] = None

    def _drop(self, why: str) -> None:
        self.drops += 1
        self.consecutive_drops += 1
        _STATUS.update(drops=self.drops, consecutive_drops=self.consecutive_drops)
        _log.info("machine metrics admission quality: cpu_sample_drops=%d (consecutive=%d, %s)",
                  self.drops, self.consecutive_drops, why)

    def run_once(self) -> Optional[dict]:
        """One rolling step: read a new snapshot and form a sample with the previous one.

        Returns the new valid sample, or None (gate off, first snapshot, or the sample was dropped)."""
        from hermes_cli import machine_cpu_metrics as cpu
        from hermes_cli import machine_metrics as mm
        state, limit = (self.limit_fn or cpu.root_limit)()
        if state != cpu.ON:
            with self._lock:
                self._prev = self._latest = None
            return None
        read = self.read_fn or (cpu._read_proc_stat if self.linux else None)
        snap = _snapshot_now(read, self.clock, self.wall) if read is not None else None
        prev, self._prev = self._prev, snap  # the new snapshot is the next window's start, valid or not
        if snap is None:
            self._drop("unreadable /proc/stat" if read is not None else "not Linux")
            return None
        if prev is None:
            return None  # the first snapshot of a run: the next step forms the first sample
        window_ms = (snap[1] - prev[1]) * 1000.0
        measured = cpu.between(prev[0], snap[0], window_ms, snap[2])
        if measured is None:
            self._drop(f"window {window_ms:.0f} ms")
            return None
        self.consecutive_drops = 0
        with self._lock:
            self._latest = (dict(measured), limit)
            self._published.notify_all()
        self.records += 1
        _STATUS.update(records=self.records, consecutive_drops=0, last_end_ms=measured["cpu_busy_sample_end_ms"])
        now = self.clock()
        if self._recorded_at is None or now - self._recorded_at >= record_every_s():
            try:
                (self.record_fn or mm.record_hub_cpu_sample)(measured)
                self._recorded_at = now
            except OSError as exc:
                _log.debug("hub CPU advisory sample not recorded: %s", exc)
        return measured

    def _takeable(self) -> bool:
        latest = self._latest
        if latest is None or latest is self._taken:
            return False
        return 0 <= int(self.wall() * 1000) - latest[0]["cpu_busy_sample_end_ms"] <= REPORT_MAX_AGE_MS

    def wait_fresh(self, timeout: float) -> bool:
        """Block at most *timeout* s until a sample is ready for :meth:`take` (not yet reported, fresh enough).

        For the anchored metrics sender only (never the sync loop thread): after a process-wide stall the sampler drops
        the over-long window and restarts it, so its next sample is ~1 s away when the sender's late tick runs."""
        with self._published:
            return self._published.wait_for(self._takeable, timeout)

    def take(self, limit: Any) -> Optional[dict]:
        """The newest sample for one metrics report: measured under *limit*, not yet reported, and ending at most
        REPORT_MAX_AGE_MS before now (wall clock, like the Board's check). Otherwise None: no triple is sent."""
        with self._lock:
            latest = self._latest
            if latest is None or latest is self._taken:
                return None
            sample, measured_limit = latest
            age_ms = int(self.wall() * 1000) - sample["cpu_busy_sample_end_ms"]
            if measured_limit != limit or not 0 <= age_ms <= REPORT_MAX_AGE_MS:
                return None
            self._taken = latest
            return dict(sample)

    def run(self) -> None:
        # Only close() ends it (the loop's finally): the loop owns keep_running, whose callers may count calls.
        from hermes_cli import machine_cpu_metrics as cpu
        while not self._stop.is_set():
            started = self.clock()
            try:
                self.run_once()
            except Exception as exc:  # health: allow BLE001 -- the advisory sampler must keep sampling
                _log.debug("hub CPU advisory sampler round failed (%s: %s)", type(exc).__name__, exc)
            # The next snapshot no earlier than one window after this one (its own read time, not the step's
            # start): a wait that wakes early is resumed, so a window is never shorter than MIN_WINDOW_MS.
            prev = self._prev
            due = (prev[1] if prev is not None else started) + cpu.WINDOW_S
            while not self._stop.is_set() and self.clock() < due:
                self._wait(due - self.clock())

    def start(self) -> Thread:
        # health: allow HX012 -- reads only the installation-root config: there is no profile scope to carry
        self._thread = Thread(target=self.run, daemon=True, name="hub-cpu-advisory-sampler")
        self._thread.start()
        return self._thread

    def close(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not current_thread():
            thread.join(timeout)
