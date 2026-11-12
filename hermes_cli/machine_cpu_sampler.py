"""The hub's advisory CPU sampler: its own thread, independent of the sync loop's iteration time. [daveey]

The lock-free advisory (``kanban_admission.hub_cpu_advisory``) refuses every hub claim when
``cache/hub-cpu-sample.json`` is older than 15 s. Measuring inside the sync loop tied that freshness to the
loop: the split begin/finish sampler took snapshot A after one round and B after the next, and a hub round of
3-20 s (board pulls plus feed wakes) always put the window past ``MAX_WINDOW_MS``. Every sample was dropped, the
file aged, and the hub claimed nothing for minutes (2026-11-12).

This thread takes a full ``machine_cpu_metrics.sample`` (two snapshots ~1 s apart, blocking only itself) every
:func:`sample_every_s` (5 s) while the root limit is on, and writes each valid one to the advisory file. The sync loop
starts it and closes it when the loop ends, so a stopped loop still leaves the advisory to go stale (fail-closed).
The metrics report takes the newest sample with :meth:`AdvisorySampler.take`, at most once per sample (a report is
never replayed) and only while it is younger than :data:`REPORT_MAX_AGE_S`.
"""
from __future__ import annotations

import logging
from threading import Event, Lock, Thread, current_thread  # bound now: loop tests stub threading.Thread
import time
from typing import Any, Callable, Optional

_log = logging.getLogger(__name__)

# One ~1 s sample every 5 windows (5 s): a 20 % duty cycle of two /proc/stat reads, well inside the 15 s advisory.
SAMPLE_EVERY_WINDOWS = 5
REPORT_MAX_AGE_S = 10.0

# The last state of this process's sampler, for the dispatcher's stale-advisory card (kanban_hub_cpu_alert).
_STATUS: dict[str, Any] = {}


def status() -> dict[str, Any]:
    return dict(_STATUS)


def sample_every_s() -> float:
    from hermes_cli import machine_cpu_metrics as cpu
    return SAMPLE_EVERY_WINDOWS * cpu.WINDOW_S


class AdvisorySampler:
    def __init__(self, *, limit_fn: Optional[Callable[[], tuple]] = None,
                 sample_fn: Optional[Callable[[], Optional[dict]]] = None,
                 record_fn: Optional[Callable[[dict], None]] = None,
                 clock: Callable[[], float] = time.monotonic, every_s: Optional[float] = None) -> None:
        self.limit_fn, self.sample_fn, self.record_fn = limit_fn, sample_fn, record_fn
        self.clock = clock
        self.every_s = every_s
        self.drops = 0
        self.consecutive_drops = 0
        self.records = 0
        self._lock = Lock()
        self._latest: Optional[tuple[dict, Any, float]] = None  # (sample, limit, monotonic taken_at)
        self._taken: Optional[tuple] = None
        self._stop = Event()
        self._thread: Optional[Thread] = None

    def run_once(self) -> Optional[dict]:
        """One measurement round. Returns the recorded sample, or None (gate off, or the sample was dropped)."""
        from hermes_cli import machine_cpu_metrics as cpu
        from hermes_cli import machine_metrics as mm
        state, limit = (self.limit_fn or cpu.root_limit)()
        if state != cpu.ON:
            with self._lock:
                self._latest = None
            return None
        started = self.clock()
        measured = self.sample_fn() if self.sample_fn else cpu.sample(cpu.WINDOW_S)
        if not measured or not all(k in measured for k in cpu.FIELDS):
            self.drops += 1
            self.consecutive_drops += 1
            _STATUS.update(drops=self.drops, consecutive_drops=self.consecutive_drops)
            _log.info("machine metrics admission quality: cpu_sample_drops=%d (consecutive=%d, round %.0f ms)",
                      self.drops, self.consecutive_drops, (self.clock() - started) * 1000.0)
            return None
        self.consecutive_drops = 0
        try:
            (self.record_fn or mm.record_hub_cpu_sample)(measured)
        except OSError as exc:
            _log.debug("hub CPU advisory sample not recorded: %s", exc)
        with self._lock:
            self._latest = (dict(measured), limit, self.clock())
        self.records += 1
        _STATUS.update(records=self.records, consecutive_drops=0, last_end_ms=measured["cpu_busy_sample_end_ms"])
        return measured

    def take(self, limit: Any) -> Optional[dict]:
        """The newest sample for one metrics report: measured under *limit*, not yet reported, still fresh."""
        with self._lock:
            latest = self._latest
            if latest is None or latest is self._taken:
                return None
            sample, measured_limit, taken_at = latest
            if measured_limit != limit or self.clock() - taken_at > REPORT_MAX_AGE_S:
                return None
            self._taken = latest
            return dict(sample)

    def run(self) -> None:
        # Only close() ends it (the loop's finally): the loop owns keep_running, whose callers may count calls.
        while not self._stop.is_set():
            started = self.clock()
            try:
                self.run_once()
            except Exception as exc:  # health: allow BLE001 -- the advisory sampler must keep sampling
                _log.debug("hub CPU advisory sampler round failed (%s: %s)", type(exc).__name__, exc)
            every = sample_every_s() if self.every_s is None else self.every_s
            self._stop.wait(max(0.0, every - (self.clock() - started)))

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
