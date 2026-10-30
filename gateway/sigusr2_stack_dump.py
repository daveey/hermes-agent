"""SIGUSR2 on-demand stack dump for the gateway: a Python-level signal handler, GIL-safe.

``kill -USR2 <gateway pid>`` appends every thread's Python stack to ``<log_dir>/gateway_faulthandler.log``
and the gateway keeps running.

Why not ``faulthandler.register(SIGUSR2, all_threads=True)``: faulthandler dumps from C signal context on
whichever thread caught the signal and walks every OTHER thread's frame chain while those threads keep
running, without the GIL. A thread that pops a frame mid-walk makes ``dump_frame`` read freed memory, so the
dump itself segfaults the gateway (David-MBP gateway 43117, 10-30: ``faulthandler_user ->
_Py_DumpTracebackThreads -> dump_frame -> _PyFrame_SafeGetCode -> SIGSEGV``).

This handler is installed with ``signal.signal``, so CPython runs it on the main thread between bytecodes,
holding the GIL. ``sys._current_frames()`` snapshots every thread's top frame atomically under the GIL and
returns refcounted frame objects; walking ``f_back`` from them reads Python objects, never raw interpreter
frames another thread can free. A frame that returns mid-dump keeps its frame object alive, so the worst
case is a slightly stale line, never a crash.

Trade-off: the dump runs when the main thread next executes bytecode. A main thread blocked in a syscall
is interrupted (EINTR) and runs it at once; one stuck inside a long C call that holds the GIL runs it only
after that call returns. Fatal-error dumps (``faulthandler.enable()``) and the shutdown/exit watchdog
dumps are unchanged.

Rate limit: a SIGUSR2 within ``SIGUSR2_DUMP_MIN_INTERVAL_S`` of the last dump is ignored, so a collector
that signals on every loop lag can't turn dumps into a CPU sink for a 150-thread gateway.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import traceback
from datetime import UTC, datetime
from typing import Callable, IO, Optional

SIGUSR2_DUMP_MIN_INTERVAL_S = 1.0
DUMP_HEADER = "--- SIGUSR2 stack dump "
DUMP_FOOTER = "--- end SIGUSR2 stack dump ---"


class ThreadStackDumper:
    """``signal.signal`` handler that appends all threads' Python stacks to ``fh``; never raises."""

    def __init__(
        self,
        fh: IO[str],
        *,
        min_interval_s: float = SIGUSR2_DUMP_MIN_INTERVAL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fh = fh  # keep the file object referenced so its fd stays open for the process lifetime
        self._min_interval_s = min_interval_s
        self._clock = clock
        self._last_dump: Optional[float] = None
        self.dumps = 0
        self.ignored = 0

    def __call__(self, signum, frame) -> None:
        now = self._clock()
        if self._last_dump is not None and now - self._last_dump < self._min_interval_s:
            self.ignored += 1
            return
        # Stamp before dumping: a second SIGUSR2 delivered while this handler runs re-enters it
        # between bytecodes and must hit the rate limit, not start a nested dump.
        self._last_dump = now
        try:
            text = self.render(signum, frame)
            data = text.encode("utf-8", "replace")
            fd = self._fh.fileno()
            # os.write, not fh.write: a buffered writer re-entered from a signal handler raises
            # "reentrant call"; a raw fd write is unbuffered, so the dump is on disk when we return.
            while data:
                written = os.write(fd, data)
                data = data[written:]
            self.dumps += 1
        except Exception:  # health: allow BLE001 S110 -- signal-handler boundary: SIGUSR2 must never kill the gateway, and logging here can deadlock on a lock the interrupted code holds
            # A closed fd or full disk loses this dump only; ``ignored``/``dumps`` stay readable for tests.
            pass

    @staticmethod
    def render(signum, frame) -> str:
        """Format every thread's stack. Snapshot first (no I/O), then look up source lines."""
        frames = sys._current_frames()
        by_ident = {t.ident: t for t in threading.enumerate()}
        current = threading.get_ident()
        snapshots = []
        for ident, top in frames.items():
            if ident == current and frame is not None:
                top = frame  # the interrupted frame, not this handler's own
            stack = traceback.StackSummary.extract(traceback.walk_stack(top), lookup_lines=False)
            stack.reverse()
            snapshots.append((ident, stack))
        stamp = datetime.now(UTC).isoformat(timespec="milliseconds")
        lines = [
            f"{DUMP_HEADER}{stamp} pid={os.getpid()} signal={int(signum)} threads={len(snapshots)}"
            " (Python-level handler on the main thread, GIL held) ---\n"
        ]
        # Main thread first, then by thread name, so dumps diff cleanly.
        def order(item):
            thread = by_ident.get(item[0])
            return (item[0] != threading.main_thread().ident, thread.name if thread else "", item[0])

        for ident, stack in sorted(snapshots, key=order):
            thread = by_ident.get(ident)
            name = thread.name if thread is not None else "<unknown>"
            flags = []
            if thread is not None and thread.daemon:
                flags.append("daemon")
            if ident == current:
                flags.append("current")
            suffix = f" ({', '.join(flags)})" if flags else ""
            lines.append(f'\nThread 0x{ident:x} "{name}"{suffix}, most recent call last:\n')
            lines.extend(stack.format())
        lines.append(f"{DUMP_FOOTER}\n\n")
        return "".join(lines)
