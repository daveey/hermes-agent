"""Same-thread local/Board CPU partition for completed sync services. [daveey]

Only Hub._call is excluded: JSON encode/decode, HTTP/TLS and its retries. Local
row work and attachment preparation remain local. Detached transfer/feed/metrics
threads are outside this envelope, never subtracted from the caller's CPU.

The loop scope includes discovery, maintenance and wait_for_work CPU. Board scopes
are nested subsets, NOT additive. All three counters use the same completed-service
boundary; readers reconciling snapshots must require equal runs (record publishes
separately). The local counter alone is a directly measured exclusive CPU total.
"""
from contextlib import ExitStack, contextmanager
from functools import wraps
import os
import sys
import threading
import time

from hermes_cli import loop_accounting

_local = threading.local()

# Fixed, bounded vocabulary of nested diagnostic parts (see ``mark``). No paths, SQL or values.
MARK_PARTS = frozenset((
    "schema.header", "schema.cookie", "schema.rows",
    "full.acquire", "full.query", "full.close",
    "acq.lifecycle", "acq.recover", "acq.connect_tracked", "acq.prepare",
    "prepare.guard", "prepare.remember",
    "acq.wal", "acq.guard2", "acq.pragmas",
    "stamp.shape", "stamp.tables", "stamp.triggers", "stamp.invariant", "stamp.row",
))


def step(fn, *args, **kwargs):
    """Name a completed loop step; nested scopes book only their residual CPU."""
    outer = getattr(_local, "step", None)
    _local.step = getattr(fn, "__name__", "other")  # which step a kept-reader open is booked under
    try:
        if not loop_accounting.scopes_enabled():
            return fn(*args, **kwargs)
        return loop_accounting.scoped_call("sync.step", fn)(*args, **kwargs)
    finally:
        try:
            flush_marks()
        finally:
            _local.step = outer


def note_reader_open(cpu, reason):
    """Book one BoardWatch reader open under the loop step that triggered it, on any thread.

    Subsets of the enclosing step or board service, never added to the thread's accounted CPU:
    ``sync.reader_open.step/<step>`` says where the CPU went, ``sync.reader_open.reason/<reason>``
    why (``first``: no reader yet; ``replaced``: another file; ``changed``: the same file moved
    with no checkpoint proof). ``WaitSplit`` subtracts the running total from its outbox probe."""
    _local.opened = getattr(_local, "opened", 0.0) + cpu
    flush_marks()
    where = getattr(_local, "step", None) or ("board" if getattr(_local, "frames", ()) else "other")
    loop_accounting.record(f"sync.reader_open.step/{where}", cpu_s=cpu, outer=False)
    loop_accounting.record(f"sync.reader_open.reason/{reason}", cpu_s=cpu, outer=False)


def note_reader_keep(cpu):
    """A reader kept across a proven checkpoint (the CPU is the proof's own cost)."""
    loop_accounting.record("sync.reader_keep/checkpoint", cpu_s=cpu, outer=False)
    loop_accounting.record(f"sync.reader_keep.step/{_step_name()}", cpu_s=cpu, outer=False)
    flush_marks()


def _step_name():
    return getattr(_local, "step", None) or ("board" if getattr(_local, "frames", ()) else "other")


def detail(part, fn, *args):
    """Always-on inclusive diagnostic, not an accounted scope or a new loop step.

    ``sync.detail/<triggering step>.<part>`` overlaps its enclosing step, probe
    and successful keep. Failed proof attempts are included too. Never sum these
    with steps or subtract them from the collector's total-minus-board value.
    Only fixed call-site names are used; no board paths or unbounded identifiers.
    """
    where = _step_name()
    began = time.thread_time()
    try:
        return fn(*args)
    finally:
        try:
            loop_accounting.record(f"sync.detail/{where}.{part}",
                                   cpu_s=time.thread_time() - began, outer=False)
        finally:
            flush_marks()


def mark(part, fn, *args, **kwargs):
    """Run ``fn`` and, inside a sync step on this thread, add its thread CPU to ``part``.

    A nested, inclusive diagnostic finer than ``detail``: it only accumulates into a thread-local
    ``(step, part) -> [calls, cpu]`` table (step captured here, at call time) and takes no lock, so
    it is safe inside the connection-open critical sections. ``flush_marks`` publishes the table at
    lock-free points. Outside a sync step (``_local.step`` unset) it is a plain call. The part must
    be one of ``MARK_PARTS``. Never sum these with steps."""
    where = getattr(_local, "step", None)
    if where is None:
        return fn(*args, **kwargs)
    if part not in MARK_PARTS:
        raise ValueError(f"unknown sync mark part: {part!r}")
    began = time.thread_time()
    try:
        return fn(*args, **kwargs)
    finally:
        cpu = time.thread_time() - began
        pending = getattr(_local, "pending", None)
        if pending is None:
            pending = _local.pending = {}
        entry = pending.setdefault((where, part), [0, 0.0])
        entry[0] += 1
        entry[1] += cpu


def flush_marks():
    """Publish and clear this thread's pending marks as ``sync.detail/<step>.<part>``.

    ``rows`` is the exact number of marks, ``cpu_s`` their exact sum, ``outer=False`` (never part
    of ``sync.cpu.total/loop``). ``runs`` counts flushes."""
    pending = getattr(_local, "pending", None)
    if not pending:
        return
    _local.pending = {}
    for (where, part), (count, cpu) in pending.items():
        loop_accounting.record(f"sync.detail/{where}.{part}", cpu_s=cpu, rows=count, outer=False)


@contextmanager
def marked_cm(cm, enter_part, exit_part=None):
    """Enter an existing context manager, timing its entry (and, if given, its exit) as marks."""
    value = mark(enter_part, cm.__enter__)
    try:
        yield value
    except BaseException:
        exit_ = cm.__exit__(*sys.exc_info()) if exit_part is None else mark(exit_part, cm.__exit__, *sys.exc_info())
        if not exit_:
            raise
    else:
        if exit_part is None:
            cm.__exit__(None, None, None)
        else:
            mark(exit_part, cm.__exit__, None, None, None)


WAIT_PARTS = ("event_wait", "outbox_probe", "reader_open")  # sync.wait/boundary: TimingDiag.wait


def wait_boundary(fn):
    """Run the wait step's config check and flush as ``sync.wait/boundary``."""
    start = time.thread_time()
    try:
        return fn()
    finally:
        loop_accounting.record("sync.wait/boundary", cpu_s=time.thread_time() - start, outer=False)


class WaitSplit:
    """Thread-CPU partition of one wait for work: ``sync.wait/<part>`` for each of ``WAIT_PARTS``.

    Marks are chained (the end of one part starts the next), so with the boundary the parts
    leave out only the step's own bookkeeping. They are subsets of ``sync.step/...wait``:
    published with ``outer=False``, they change neither that step nor ``sync.cpu.total/loop``."""

    __slots__ = ("_last", "_opened", "parts", "ticks")

    def __init__(self):
        self.parts = dict.fromkeys(WAIT_PARTS, 0.0)
        self.ticks = 0
        self._opened = getattr(_local, "opened", 0.0)
        self._last = time.thread_time()

    def mark(self, part):
        now = time.thread_time()
        opened = getattr(_local, "opened", 0.0)
        opens = min(now - self._last, opened - self._opened)
        self.parts[part] += now - self._last - opens
        self.parts["reader_open"] += opens
        self.ticks += part == "event_wait"
        self._last, self._opened = now, opened

    def publish(self):
        for part, cpu in self.parts.items():
            loop_accounting.record(f"sync.wait/{part}", cpu_s=cpu, outer=False,
                                   rows=self.ticks if part == "event_wait" else 0)


@contextmanager
def service(name):
    """Publish one completed service, including exceptional exits, on this thread."""
    frames = getattr(_local, "frames", ())
    frame = {"board": 0.0}
    _local.frames = (*frames, frame)
    pid = os.getpid()
    start = time.thread_time()
    error = None
    try:
        yield
    except BaseException as exc:
        error = type(exc).__name__
        raise
    finally:
        total = time.thread_time() - start
        _local.frames = frames
        # Never combine pre-fork thread CPU with the child's clock.
        if os.getpid() == pid:
            excluded = min(total, frame["board"])
            for kind, cpu in (("local", total - excluded), ("board_call", excluded), ("total", total)):
                loop_accounting.record(f"sync.cpu.{kind}/{name}", cpu_s=cpu,
                                       error=error is not None, error_kind=error, outer=False)


def board_call(fn):
    """Exclude one outermost synchronous Board call from active services only."""
    @wraps(fn)
    def wrapped(*args, **kwargs):
        frames = getattr(_local, "frames", ())
        if not frames or getattr(_local, "in_call", False):
            return fn(*args, **kwargs)
        _local.in_call = True
        pid = os.getpid()
        start = time.thread_time()
        try:
            return fn(*args, **kwargs)
        finally:
            cpu = time.thread_time() - start
            _local.in_call = False
            if os.getpid() == pid:
                for frame in frames:
                    frame["board"] += cpu
    return wrapped


@contextmanager
def iterations(keep_running):
    """Own iteration services in the caller's scope, including its bounded wait.

    The stack sees the caller's real exit, not GeneratorExit from closing a
    suspended generator. Completed iterations publish before the next predicate.
    """
    with ExitStack() as scope:
        def rounds():
            while keep_running():
                if loop_accounting.scopes_enabled():
                    scope.enter_context(loop_accounting.Scope("sync.round/loop"))
                scope.enter_context(service("loop"))
                yield
                scope.close()
        yield rounds()
