"""Per-process pass accounting for background loops and polled routes (design t_01a1d563324d7e8e §2 P1). [daveey]

Every Kanban background loop and every polled Desktop route reports each pass here, so CPU can be attributed
to the loop that spent it instead of being inferred from py-spy samples (which left ~25% of the hub's
measured CPU unattributed). Per pass name it counts:

* ``runs``: passes started;
* ``skips``: passes that returned early because their key did not change;
* ``changes``: passes whose key moved (work was due);
* ``fallbacks``: passes that took the full / fallback path;
* ``rows``: rows the pass changed or processed, where the loop knows it;
* ``errors``: passes that raised;
* ``error_kinds``: those errors by exception class name (``{"OperationalError": 2}``), at most
  ``MAX_ERROR_KINDS`` names plus ``other`` for the rest; an error reported without a class (``record`` /
  ``span_close`` with ``error=True`` and no ``error_kind``) counts as ``unknown``. A designed fallback the
  caller handles must not escape the pass (call :meth:`Pass.fallback` instead): this map tells a live
  check reading ``errors`` what actually raised;
* ``cpu_s`` / ``cpu_max_s``: ``time.thread_time()`` spent inside the pass (sum / worst), and ``wall_s``.

Per process it also records every live thread's CPU (``/proc/self/task/<tid>/stat`` on Linux) next to the
CPU the outermost passes on that thread accounted for, so the remainder is attributable to a thread
(``process.unaccounted_live_cpu_s``; None without procfs, where thread CPU is not readable) rather than lost.
A thread's accounted CPU folds into the exited total when the thread ends, so a reused native id starts at 0.

Use::

    from hermes_cli import loop_accounting as la

    with la.pass_("gateway.notifier.collect_board") as p:
        if key == last_key:
            p.skip()
            return
        p.change()
        rows = recompute()
        p.rows(len(rows))

    result = la.timed("dispatch.release_stale_claims", release_stale_claims, conn)  # rows from an int/len

    @la.accounted("serve.route.board")
    def get_board(...): ...

``pass_`` also takes the outcome up front: ``pass_(name, skipped=..., changed=..., fallback=..., rows=...)``.
Nested passes count in their own names; only the outermost pass on a thread adds to that thread's
``accounted_cpu_s``, so totals never double count. Mods (chat-closeout, user-steps, throughput, runners) call
the same API; ``host_loop_lease.HostLoopLease`` records ``lease.<name>`` for every host pass between a true
``held()`` and its ``beat()`` without any change in the caller (a pass that raised before its ``beat()`` is
closed as an error by the next true ``held()``).

**Snapshot.** A long-lived process calls :func:`start` once (``shared_metrics_process.begin_process`` does it
for the gateway, ``serve`` and the ``tui`` backend); a daemon thread then writes
``<root home>/run/loop-accounting/<pid>.json`` every ``loop_accounting.interval_s`` (config.yaml, 60 s) with
an atomic replace: role, pid, start time, process and per-thread CPU and the cumulative counters. Files of
dead pids older than an hour are pruned. ``loop_accounting.enabled: false`` stops the writer (counters stay).

**Residual.** ``snapshot()["residual"]`` splits the CPU no pass accounted per thread-name family
(``families[f]``: ``cpu_s``, ``accounted_cpu_s``, ``unaccounted_cpu_s``; ``exited`` for threads gone since
start; their sum matches ``process.cpu_s - process.accounted_cpu_s``). Residual scopes name the callers:
``thread/<qualname>`` (every ``asyncio.to_thread`` / AnyIO submission), ``aux_provider/<operation>``,
``plugin/user-terminal.sweep_jobs``, ``plugin/loop-watch.check_once`` and ``loop_cb/<callback or task
coroutine>`` (main-loop CPU outside any pass; 64 names then ``loop_cb/other``). A scope books only the CPU
its nested passes did not. ``loop_accounting.scopes`` / ``loop_accounting.callbacks`` (config.yaml, both
default on) switch the instrumentation off; hub A/B cost of both on: about +0.09% of a core.
``families[f]`` also carries ``user_s`` / ``system_s`` (thread CPU split; a high system share is kernel time
in wakeups and locks, not Python). ``residual.targets`` names the same unaccounted CPU per plain
``threading.Thread(target=fn)`` (``module.qualname``, 64 names then ``other``; live threads at snapshot time,
ended threads at the end of ``run``). It is an overlay, not additive with ``families``/``exited``.
``loop_accounting.thread_targets`` (default on) switches it off; cost is one ``Thread.run`` wrapper: about
3-6 µs of CPU per thread lifecycle (``tests/hermes_cli/test_loop_accounting_thread_targets.py`` pins <= 50 µs).

**Cost.** One pass is two ``thread_time`` and two ``perf_counter`` reads, a thread-local depth and two
uncontended lock rounds: a few microseconds (``tests/hermes_cli/test_loop_accounting_bench.py`` pins <= 20 µs).
"""

from __future__ import annotations

import atexit
import collections
import functools
import json
import logging
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from hermes_cli import http_body_accounting, loop_callback_latency
from hermes_cli.loop_accounting_open import OpenIntervals

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
DEFAULT_INTERVAL_S = 60.0
MAX_NAMES = 2048  # distinct pass names; later names fold into OVERFLOW (a per-method name must not grow forever)
OVERFLOW = "_overflow"
MAX_ERROR_KINDS = 8  # distinct exception class names per pass name; later ones count under ERROR_KIND_OTHER
ERROR_KIND_OTHER = "other"
ERROR_KIND_UNKNOWN = "unknown"
PRUNE_AFTER_S = 3600.0

_thread_time = time.thread_time
_perf = time.perf_counter
_native_id = threading.get_native_id


class _Stat:
    __slots__ = (
        "changes",
        "cpu_max_s",
        "cpu_s",
        "error_kinds",
        "errors",
        "fallbacks",
        "last_at",
        "rows",
        "runs",
        "skips",
        "wall_s",
    )

    def __init__(self) -> None:
        self.runs = self.skips = self.changes = self.fallbacks = self.rows = self.errors = 0
        self.cpu_s = self.cpu_max_s = self.wall_s = 0.0
        self.last_at = 0.0
        self.error_kinds: Optional[dict[str, int]] = None  # created on the first error

    def error(self, kind: Optional[str]) -> None:
        """Count one error of exception class ``kind`` (caller holds ``_lock``); the map stays bounded."""
        self.errors += 1
        kinds = self.error_kinds
        if kinds is None:
            kinds = self.error_kinds = {}
        kind = kind or ERROR_KIND_UNKNOWN
        if kind not in kinds and len(kinds) - (ERROR_KIND_OTHER in kinds) >= MAX_ERROR_KINDS:
            kind = ERROR_KIND_OTHER
        kinds[kind] = kinds.get(kind, 0) + 1

    def as_dict(self) -> dict[str, Any]:
        return {"runs": self.runs, "skips": self.skips, "changes": self.changes, "fallbacks": self.fallbacks,
                "rows": self.rows, "errors": self.errors, "error_kinds": dict(self.error_kinds or {}),
                "cpu_s": round(self.cpu_s, 6),
                "cpu_max_s": round(self.cpu_max_s, 6), "wall_s": round(self.wall_s, 6),
                "last_at": round(self.last_at, 3)}


_lock = threading.RLock()
_open_intervals = OpenIntervals()
_stats: dict[str, _Stat] = {}
_thread_accounted: dict[int, float] = {}  # native thread id -> CPU of its outermost passes
_local = threading.local()
_exited_tids: collections.deque = collections.deque()  # tids whose thread ended; folded under _lock
_thread_targets: dict[int, str] = {}  # native id -> target name of a live ``threading.Thread`` we wrapped
_target_exited: dict[str, dict[str, float]] = {}  # target name -> CPU/accounted CPU of its ended threads
_thread_split: dict[int, tuple[float, float]] = {}  # native id -> (user_s, system_s) of the last Darwin read
_started_at = time.time()
_state: dict[str, Any] = {"role": None, "thread": None, "stop": None, "pid": os.getpid()}
# Bumped by the at-fork hook in the child, with the child's thread and wall clocks at the fork: a pass opened
# before the fork books only the child's CPU and wall since the fork (the child's thread clock restarts at 0,
# so the parent's start mark would book negative CPU).
_fork: dict[str, Any] = {"gen": 0, "c0": 0.0, "w0": 0.0}


class _ThreadExit:
    """Held in ``_local`` by each thread that accounted CPU. CPython drops a thread's ``threading.local``
    values when the thread ends, before its native id can be reused, so this queues the tid to fold then:
    a later thread given the same id never inherits the exited thread's accounted CPU. Lock-free (a
    ``deque.append``), so it is safe wherever teardown runs it."""

    __slots__ = ("tid",)
    _queue = _exited_tids  # bound here, so __del__ still reaches it while module globals are torn down

    def __init__(self, tid: int) -> None:
        self.tid = tid

    def __del__(self) -> None:
        self._queue.append(self.tid)


def _own_tid() -> int:
    """This thread's native id, registering its exit fold on first use."""
    tid = _native_id()
    mark = getattr(_local, "exit_mark", None)
    if mark is None or mark.tid != tid:
        _local.exit_mark = _ThreadExit(tid)
    return tid


def _fold_exited() -> None:
    """Move the accounted CPU of ended threads into ``exited_accounted`` (caller holds ``_lock``)."""
    while _exited_tids:
        try:
            tid = _exited_tids.popleft()
        except IndexError:
            return
        gone = _thread_accounted.pop(tid, None)
        _open_intervals.discard(tid)
        if gone:
            _state["exited_accounted"] = _state.get("exited_accounted", 0.0) + gone


def _add_accounted(tid: int, cpu: float) -> None:
    """Add ``cpu`` to ``tid``'s outermost-pass CPU (caller holds ``_lock``)."""
    if _exited_tids:
        _fold_exited()
    _thread_accounted[tid] = _thread_accounted.get(tid, 0.0) + cpu


def _stat(name: str) -> _Stat:
    st = _stats.get(name)
    if st is None:
        if len(_stats) >= MAX_NAMES:
            name = OVERFLOW
            st = _stats.get(name)
        if st is None:
            st = _stats[name] = _Stat()
    return st


class Pass:
    """One accounted pass (a context manager); see the module docstring. Mark the outcome with
    :meth:`skip`, :meth:`change`, :meth:`fallback` and :meth:`rows` at any point before it exits."""

    __slots__ = ("_c0", "_gen", "_outer", "_tid", "_w0", "changed", "fell_back", "name", "nrows", "skipped")

    def __init__(self, name: str, skipped: bool = False, changed: Optional[bool] = None, fallback: bool = False,
                 rows: Optional[int] = None) -> None:
        self.name = name
        self.skipped = skipped
        self.changed = changed
        self.fell_back = fallback
        self.nrows = rows or 0

    def skip(self) -> None:
        self.skipped = True

    def change(self, changed: bool = True) -> None:
        self.changed = changed

    def fallback(self, fell_back: bool = True) -> None:
        self.fell_back = fell_back

    def rows(self, n: int) -> None:
        self.nrows += int(n or 0)

    def __enter__(self) -> Pass:
        depth = getattr(_local, "depth", 0)
        _local.depth = depth + 1
        self._outer = depth == 0
        self._gen = _fork["gen"]
        self._w0 = _perf()
        with _lock:
            self._tid = _own_tid()
            _fold_exited()
            self._c0 = _thread_time()
            _open_intervals.enter(self._tid, self, self.name, self._c0,
                                  _thread_accounted.get(self._tid, 0.0))
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        with _lock:
            self._close(exc_type)

    def _close(self, exc_type) -> None:
        if self._gen == _fork["gen"]:
            cpu = _thread_time() - self._c0
            wall = _perf() - self._w0
        else:  # opened before an os.fork, closing in the child: only the child's part, from the fork
            cpu = _thread_time() - _fork["c0"]
            wall = _perf() - _fork["w0"]
        # Defensive clamp for callers that cleared thread-local state while a pass was open.
        _local.depth = max(0, getattr(_local, "depth", 1) - 1)
        tid = _own_tid() if self._outer else 0
        with _lock:
            _open_intervals.close(self._tid, self)
            st = _stat(self.name)
            st.runs += 1
            if self.skipped:
                st.skips += 1
            if self.changed:
                st.changes += 1
            if self.fell_back:
                st.fallbacks += 1
            st.rows += self.nrows
            if exc_type is not None:
                st.error(getattr(exc_type, "__name__", None))
            st.cpu_s += cpu
            st.cpu_max_s = max(st.cpu_max_s, cpu)
            st.wall_s += wall
            st.last_at = time.time()
            if self._outer:
                _add_accounted(tid, cpu)


def pass_(name: str, *, skipped: bool = False, changed: Optional[bool] = None, fallback: bool = False,
          rows: Optional[int] = None) -> Pass:
    """A context manager accounting one pass of ``name``."""
    return Pass(name, skipped, changed, fallback, rows)


def record(name: str, *, cpu_s: float = 0.0, wall_s: float = 0.0, skipped: bool = False,
           changed: Optional[bool] = None, fallback: bool = False, rows: int = 0, error: bool = False,
           outer: bool = True, error_kind: Optional[str] = None) -> None:
    """Account a pass measured elsewhere (``cpu_s`` from the caller's own ``thread_time`` delta, on this thread).
    ``error_kind`` names the exception class of an ``error`` (else it counts as ``unknown``)."""
    outer = outer and getattr(_local, "depth", 0) <= 0
    tid = _own_tid() if outer else 0
    with _lock:
        st = _stat(name)
        st.runs += 1
        st.skips += bool(skipped)
        st.changes += bool(changed)
        st.fallbacks += bool(fallback)
        st.rows += int(rows or 0)
        if error:
            st.error(error_kind)
        st.cpu_s += cpu_s
        st.cpu_max_s = max(st.cpu_max_s, cpu_s)
        st.wall_s += wall_s
        st.last_at = time.time()
        if outer:
            _add_accounted(tid, cpu_s)


def span_open(name: str) -> tuple:
    """Open a pass whose end is in another call (``host_loop_lease``: a true ``held()`` to its ``beat()``).
    Close it with :func:`span_close` on the same thread. Passes run inside it keep their own names; the
    thread's ``accounted_cpu_s`` gets only the span's CPU those passes did not already account."""
    tid = _own_tid()
    with _lock:
        _fold_exited()
        acc0 = _thread_accounted.get(tid, 0.0)
        token = name, tid, _thread_time(), _perf(), acc0
        _open_intervals.enter(tid, id(token), name, token[2], acc0)
        return token


def span_discard(token: tuple) -> None:
    """Retire an abandoned span on any thread without recording a completed pass."""
    with _lock:
        _open_intervals.close(token[1], id(token))


def span_close(token: tuple, *, error: bool = False, error_kind: Optional[str] = None) -> bool:
    with _lock:
        return _span_close_locked(token, error=error, error_kind=error_kind)


def _span_close_locked(token: tuple, *, error: bool = False, error_kind: Optional[str] = None) -> bool:
    """Close a :func:`span_open` token. False (nothing recorded) when called on another thread. ``error_kind``
    names the exception class of an ``error`` (else it counts as ``unknown``)."""
    name, tid, c0, w0, acc0 = token
    _open_intervals.close(tid, id(token))
    if tid != _native_id():
        return False
    cpu = _thread_time() - c0
    wall = _perf() - w0
    with _lock:
        st = _stat(name)
        st.runs += 1
        if error:
            st.error(error_kind)
        st.cpu_s += cpu
        st.cpu_max_s = max(st.cpu_max_s, cpu)
        st.wall_s += wall
        st.last_at = time.time()
        if getattr(_local, "depth", 0) <= 0:
            inner = _thread_accounted.get(tid, 0.0) - acc0
            _add_accounted(tid, max(0.0, cpu - inner))
    return True


def _rows_of(result: Any) -> int:
    if isinstance(result, bool) or result is None:
        return 0
    if isinstance(result, int):
        return result
    if isinstance(result, (list, tuple, set, frozenset, dict)):
        return len(result)
    return 0


def timed(name: str, fn: Callable, /, *args: Any, **kwargs: Any) -> Any:
    """Call ``fn(*args, **kwargs)`` as one pass of ``name``; ``rows`` is the result when it is an int, or its
    length when it is a collection. Returns the result unchanged."""
    with Pass(name) as p:
        result = fn(*args, **kwargs)
        p.nrows = _rows_of(result)
        return result


def accounted(name: str, *, rows: bool = False) -> Callable[[Callable], Callable]:
    """Decorator: every call of the function is one pass of ``name`` (``functools.wraps`` keeps the signature,
    so FastAPI and ``coalesced_read`` see the original parameters). ``rows=True`` counts the result as
    :func:`timed` does."""
    def wrap(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            with Pass(name) as p:
                result = fn(*args, **kwargs)
                if rows:
                    p.nrows = _rows_of(result)
                return result
        return wrapped
    return wrap


# -- snapshot ------------------------------------------------------------------------------------------------

def counters() -> dict[str, dict[str, Any]]:
    """A copy of the cumulative counters, by pass name."""
    with _lock:
        out = {name: st.as_dict() for name, st in _stats.items()}
        out.update({name: st.as_dict() for name, st in _cb_stats.items()})
        return out


def _clk_tck() -> float:
    try:
        return float(os.sysconf("SC_CLK_TCK"))
    except (AttributeError, OSError, ValueError):
        return 100.0


def _proc_stat_cpu(path: str, hz: float) -> Optional[tuple[str, float]]:
    got = _proc_stat_split(path, hz)
    return None if got is None else (got[0], got[1] + got[2])


def _proc_stat_split(path: str, hz: float) -> Optional[tuple[str, float, float]]:
    """(comm, user seconds, system seconds) from a ``/proc/.../stat`` file."""
    try:
        with open(path, encoding="ascii", errors="replace") as fh:
            raw = fh.read()
    except OSError:
        return None
    head, _, tail = raw.rpartition(")")
    fields = tail.split()
    if len(fields) < 13:
        return None
    comm = head.partition("(")[2]
    return comm, int(fields[11]) / hz, int(fields[12]) / hz


_FAMILY_SUFFIX = re.compile(r"([_\-\s]\d+|[_\-\s]?0x[0-9a-fA-F]+|[_\-\s][0-9a-fA-F]{8,})+$")
_FAMILY_TARGET = re.compile(r"\s*\(.*\)\s*$")


def family_of(name: Optional[str]) -> str:
    """Thread-name family: trailing ``_N`` / ``-N`` / `` N`` / hex handle suffixes and a ``(target)`` tail are
    stripped (``asyncio_3`` -> ``asyncio``, ``Thread-7 (run)`` -> ``Thread``, ``AnyIO worker thread`` stays)."""
    name = (name or "?").strip() or "?"
    name = _FAMILY_TARGET.sub("", name) or name
    return _FAMILY_SUFFIX.sub("", name) or name


def _darwin_thread_cpu() -> list[tuple[int, str, float]]:
    """(native id, name, CPU seconds) of every thread of this process via ``proc_pidinfo`` (libproc). [] on any
    failure; a total above the process CPU (a time-unit surprise) is discarded rather than reported."""
    try:
        import ctypes
        import ctypes.util
        lib = ctypes.CDLL(ctypes.util.find_library("proc") or "/usr/lib/libproc.dylib", use_errno=True)

        class _ThreadInfo(ctypes.Structure):
            _fields_ = [("user", ctypes.c_uint64), ("system", ctypes.c_uint64), ("cpu_usage", ctypes.c_int32),
                        ("policy", ctypes.c_int32), ("run_state", ctypes.c_int32), ("flags", ctypes.c_int32),
                        ("sleep_time", ctypes.c_int32), ("curpri", ctypes.c_int32), ("priority", ctypes.c_int32),
                        ("maxpriority", ctypes.c_int32), ("name", ctypes.c_char * 64)]

        # Explicit prototype: without it ctypes passes Python ints as 32-bit C int and truncates 64-bit thread ids.
        lib.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
        lib.proc_pidinfo.restype = ctypes.c_int
        pid = os.getpid()
        ids = (ctypes.c_uint64 * 1024)()
        # PROC_PIDLISTTHREADIDS (28) returns thread ids (== threading.native_id), which is what
        # PROC_PIDTHREADID64INFO (15) looks up; PROC_PIDLISTTHREADS (6) would return pthread handles.
        got = lib.proc_pidinfo(pid, 28, 0, ctypes.addressof(ids), ctypes.sizeof(ids))
        if got <= 0:
            return []
        out = []
        split: dict[int, tuple[float, float]] = {}
        for i in range(got // 8):
            info = _ThreadInfo()
            if lib.proc_pidinfo(pid, 15, ids[i], ctypes.addressof(info), ctypes.sizeof(info)) <= 0:  # THREADID64INFO
                continue
            out.append((int(ids[i]), info.name.decode("utf-8", "replace"), (info.user + info.system) / 1e9))  # pth_*_time are already ns
            split[int(ids[i])] = (info.user / 1e9, info.system / 1e9)
        total = sum(c for _, _, c in out)
        if total > (os.times().user + os.times().system) * 1.25 + 1.0:
            return []
        _thread_split.clear()
        _thread_split.update(split)  # user/system split of the same read, for thread_cpu()
        return out
    except Exception:  # health: allow BLE001 -- best effort; no per-thread counters is a documented state
        return []


def thread_cpu() -> list[dict[str, Any]]:
    with _lock:
        rows = _thread_cpu_locked()
        for row in rows:
            _open_intervals.apply(row)
        return rows


def _thread_cpu_locked() -> list[dict[str, Any]]:
    """Every live thread of this process: native id, name and CPU seconds (user + system), plus the CPU its
    outermost accounted passes took. Linux reads ``/proc/self/task``; elsewhere only live accounted threads are
    listed (their ``cpu_s`` is None)."""
    names = {t.native_id: t.name for t in threading.enumerate() if t.native_id is not None}
    with _lock:
        _fold_exited()
        accounted_by = _copy_accounted()
    out: list[dict[str, Any]] = []
    hz = _clk_tck()
    task_dir = "/proc/self/task"
    try:
        tids = [int(t) for t in os.listdir(task_dir) if t.isdigit()]
    except OSError:
        tids = []
    for tid in sorted(tids):
        got = _proc_stat_split(f"{task_dir}/{tid}/stat", hz)
        if got is None:
            continue
        comm, user, system = got
        precise = True
        try:
            cpu = time.clock_gettime((~tid << 3) | 6)  # Linux numeric TID CPUCLOCK_SCHED, not a pthread pointer
        except (OSError, ValueError):
            cpu = user + system
            precise = False
        out.append({"tid": tid, "name": names.get(tid, comm), "cpu_s": round(cpu, 6),
                    "cpu_stat_s": round(user + system, 3),
                    "cpu_sample_precise": precise,
                    "user_s": round(user, 3), "system_s": round(system, 3),
                    "accounted_cpu_s": round(accounted_by.get(tid, 0.0), 6)})
    if not tids and sys.platform == "darwin":
        for tid, name, cpu in _darwin_thread_cpu():
            row = {"tid": tid, "name": names.get(tid) or name or "?", "cpu_s": round(cpu, 6),
                   "accounted_cpu_s": round(accounted_by.get(tid, 0.0), 6)}
            if tid in _thread_split:
                row["user_s"], row["system_s"] = (round(v, 3) for v in _thread_split[tid])
            out.append(row)
        tids = [t["tid"] for t in out]
    if not tids:  # no per-thread counters: list live accounted threads only (exited ones fold in snapshot())
        for tid, acc in sorted(accounted_by.items()):
            if tid not in names:
                continue
            out.append({"tid": tid, "name": names.get(tid, "?"), "cpu_s": None, "accounted_cpu_s": round(acc, 6)})
    return out


def _process_start_time() -> Optional[float]:
    try:
        from gateway.status import get_process_start_time
        return get_process_start_time(os.getpid())
    except Exception:  # health: allow BLE001 -- start time is informational
        return None


def snapshot() -> dict[str, Any]:
    """Sample native CPU and open Pass/Scope/span intervals under the booking lock.

    Thread totals include the unbooked part of the oldest open interval (its
    nested completed work is subtracted). ``open_accounting`` names that part;
    completed loop counters are unchanged. ``open_accounting_complete`` is false
    if an open interval lacks a precise native reading or its pending CPU would
    be negative; consumers must reject that boundary, not infer savings from it.
    Callback hooks remain best-effort.
    Linux cpu_s uses the numeric-TID high-resolution clock; cpu_stat_s retains
    the tick-granular user/system sum. Diagnostic metadata is read afterwards.
    """
    with _lock:
        result = _snapshot_locked()
    result.update(process_start_time=_process_start_time(),
                  slow_callbacks=loop_callback_latency.snapshot(), http=http_body_accounting.snapshot(),
                  diagnostic_code=loop_callback_latency.code_provenance())
    return result


def _snapshot_locked() -> dict[str, Any]:
    """The JSON-ready snapshot this process writes (see the module docstring)."""
    times = os.times()
    threads = thread_cpu()
    procfs = bool(threads) and threads[0]["cpu_s"] is not None
    live = sum(t["cpu_s"] for t in threads if t["cpu_s"] is not None)
    # Fold exited threads, so pool churn cannot grow the map (a thread that accounted CPU already queued its
    # fold at exit via _ThreadExit; this catches the rest). Without procfs (macOS, whose native thread
    # ids are never reused) the live set comes from threading.enumerate().
    if procfs:
        live_tids = {t["tid"] for t in threads}
    else:
        live_tids = {t.native_id for t in threading.enumerate() if t.native_id is not None}
    with _lock:
        _fold_exited()
        by_tid = _copy_accounted()
        by_tid.update({t["tid"]: t["accounted_cpu_s"] for t in threads
                       if t.get("open_accounting") and t["tid"] in _open_intervals.by_tid})
        if live_tids:
            for tid in [tid for tid in by_tid if tid not in live_tids]:
                _state["exited_accounted"] = _state.get("exited_accounted", 0.0) + by_tid.pop(tid)
                _thread_accounted.pop(tid, None)
        accounted_total = sum(by_tid.values()) + _state.get("exited_accounted", 0.0)
        accounted_live = sum(by_tid.get(t["tid"], 0.0) for t in threads)
    process_cpu = times.user + times.system
    return {
        "version": SCHEMA_VERSION,
        "role": _state["role"] or _role_guess(),
        "pid": os.getpid(),
        "started_at": round(_started_at, 3),
        "written_at": round(time.time(), 3),
        "uptime_s": round(time.time() - _started_at, 3),
        "process": {
            "cpu_s": round(process_cpu, 3), "user_s": round(times.user, 3), "system_s": round(times.system, 3),
            "live_threads_cpu_s": round(live, 3),
            # CPU of threads that already exited (in the process total, gone from /proc/self/task).
            "exited_threads_cpu_s": round(max(0.0, process_cpu - live), 3) if procfs else None,
            "accounted_cpu_s": round(accounted_total, 6),
            "unaccounted_live_cpu_s": round(max(0.0, live - accounted_live), 3) if procfs else None,
        },
        "threads": threads,
        "open_accounting_complete": _open_intervals.complete(threads),
        "residual": _residual(threads, process_cpu, accounted_total, procfs),
        "loops": counters(),
    }


def _residual(threads: list[dict[str, Any]], process_cpu: float, accounted_total: float,
              have_cpu: bool) -> dict[str, Any]:
    """CPU no accounting scope owns, per thread-name family. ``families[f].unaccounted_cpu_s`` is that family's
    thread CPU minus the CPU its outermost scopes accounted (clamped at 0, so never negative); ``exited`` holds
    the threads gone since start (process CPU not on a live thread, against the accounted CPU they folded in).
    Note: ``process.accounted_cpu_s`` now also counts the thread/aux/plugin/loop_cb scopes, so
    ``process.unaccounted_*`` is not like-for-like with snapshots written before the residual scopes existed."""
    out: dict[str, Any] = {"available": have_cpu, "families": {}, "exited": None,
                           "process_unaccounted_cpu_s": round(max(0.0, process_cpu - accounted_total), 3),
                           "unaccounted_cpu_s": None}
    if not have_cpu:
        return out
    fams: dict[str, dict[str, Any]] = {}
    live = 0.0
    for t in threads:
        cpu = t["cpu_s"] or 0.0
        live += cpu
        f = fams.setdefault(family_of(t["name"]), {"threads": 0, "cpu_s": 0.0, "accounted_cpu_s": 0.0})
        f["threads"] += 1
        f["cpu_s"] += cpu
        f["accounted_cpu_s"] += t["accounted_cpu_s"]
        if t.get("user_s") is not None:
            f["user_s"] = f.get("user_s", 0.0) + t["user_s"]
            f["system_s"] = f.get("system_s", 0.0) + (t.get("system_s") or 0.0)
    with _lock:
        exited_acc = _state.get("exited_accounted", 0.0)
        live_targets = dict(_thread_targets)
        gone_targets = {k: dict(v) for k, v in _target_exited.items()}
    exited = {"cpu_s": max(0.0, process_cpu - live), "accounted_cpu_s": exited_acc}
    total = 0.0
    for f in list(fams.values()) + [exited]:
        f["unaccounted_cpu_s"] = max(0.0, f["cpu_s"] - f["accounted_cpu_s"])
        total += f["unaccounted_cpu_s"]
        for k in ("cpu_s", "accounted_cpu_s", "unaccounted_cpu_s"):
            f[k] = round(f[k], 6)
        for k in ("user_s", "system_s"):
            if k in f:
                f[k] = round(f[k], 6)
    out.update(families=fams, exited=exited, unaccounted_cpu_s=round(total, 6),
               targets=_targets_residual(threads, live_targets, gone_targets))
    return out


def _targets_residual(threads: list[dict[str, Any]], live: dict[int, str],
                      gone: dict[str, dict[str, float]]) -> dict[str, dict[str, Any]]:
    """The same unaccounted CPU seen per ``threading.Thread`` target (``instrument_thread_targets``): live
    threads from this snapshot's per-thread CPU, ended ones from their final reading. An overlay on
    ``families`` / ``exited``, NOT additive with them (a target's threads are also in their family or in
    ``exited``); each thread's unaccounted CPU is clamped at 0 on its own."""
    rows: dict[str, dict[str, float]] = {}

    def row(key: str) -> dict[str, float]:
        return rows.setdefault(key, {"live_threads": 0, "exited_threads": 0, "cpu_s": 0.0,
                                     "accounted_cpu_s": 0.0, "unaccounted_cpu_s": 0.0})

    for t in threads:
        key = live.get(t["tid"])
        if key is None:
            continue
        r = row(key)
        cpu = t["cpu_s"] or 0.0
        r["live_threads"] += 1
        r["cpu_s"] += cpu
        r["accounted_cpu_s"] += t["accounted_cpu_s"]
        r["unaccounted_cpu_s"] += max(0.0, cpu - t["accounted_cpu_s"])
    for key, g in gone.items():
        r = row(key)
        r["exited_threads"] += g["threads"]
        r["cpu_s"] += g["cpu_s"]
        r["accounted_cpu_s"] += g["accounted_cpu_s"]
        r["unaccounted_cpu_s"] += g["unaccounted_cpu_s"]
    return {k: {n: (round(v, 6) if isinstance(v, float) else v) for n, v in r.items()} for k, r in rows.items()}


def _role_guess() -> str:
    try:
        from hermes_cli.observability.shared_metrics_process import current_process_kind
        return current_process_kind()
    except Exception:  # health: allow BLE001 -- the role is a label
        return "other"


def snapshot_dir() -> Path:
    from hermes_constants import get_default_hermes_root
    return get_default_hermes_root() / "run" / "loop-accounting"


def snapshot_path(pid: Optional[int] = None) -> Path:
    return snapshot_dir() / f"{pid or os.getpid()}.json"


def write_snapshot(path: Optional[Path] = None) -> Path:
    """Write :func:`snapshot` atomically (temp file in the same directory + ``os.replace``). Returns the path."""
    target = Path(path) if path is not None else snapshot_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(snapshot(), fh, separators=(",", ":"), sort_keys=True)
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    return target


def _pid_alive(pid: int) -> bool:
    from gateway.status import _pid_exists  # never os.kill(pid, 0): a CTRL_C_EVENT on Windows
    return _pid_exists(pid)


def prune(directory: Optional[Path] = None, *, now: Optional[float] = None) -> int:
    """Remove snapshots of dead pids not written for ``PRUNE_AFTER_S``. Returns how many were removed."""
    directory = Path(directory) if directory is not None else snapshot_dir()
    now = time.time() if now is None else now
    removed = 0
    try:
        entries = list(directory.glob("*.json"))
    except OSError:
        return 0
    for entry in entries:
        stem = entry.stem
        if not stem.isdigit() or int(stem) == os.getpid():
            continue
        try:
            if now - entry.stat().st_mtime < PRUNE_AFTER_S or _pid_alive(int(stem)):
                continue
            entry.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def _setting(key: str, default: Any) -> Any:
    """``loop_accounting.<key>`` from config.yaml (read once per :func:`start`), else ``default``."""
    try:
        from hermes_cli.config import load_config
        from hermes_cli.config_backend import ConfigBackendUnavailable
    except Exception:  # health: allow BLE001 -- no config module: the defaults
        logger.debug("loop accounting: config unavailable; using defaults", exc_info=True)
        return default
    try:
        section = load_config().get("loop_accounting") or {}
        return section.get(key, default) if isinstance(section, dict) else default
    except (Exception, ConfigBackendUnavailable):  # health: allow BLE001 -- an unreadable config keeps the defaults
        # ConfigBackendUnavailable is a SystemExit, caught by name: accounting must never be what ends a boot.
        logger.debug("loop accounting: config unreadable; using defaults", exc_info=True)
        return default


def enabled() -> bool:
    """``loop_accounting.enabled`` (default on). Off stops the snapshot writer; the counters stay."""
    if _state.get("enabled") is None:
        _state["enabled"] = bool(_setting("enabled", True))
    return bool(_state["enabled"])


def interval_s() -> float:
    try:
        value = float(_setting("interval_s", DEFAULT_INTERVAL_S))
    except (TypeError, ValueError):
        value = DEFAULT_INTERVAL_S
    return max(value, 0.05)


def scopes_enabled() -> bool:
    """``loop_accounting.scopes`` as read by :func:`instrument` (default on until instrumented)."""
    return bool(_state.get("scopes", True))


def _copy_accounted() -> dict[int, float]:
    """Copy of ``_thread_accounted``; callback hooks insert into it lock-free, so retry a racing resize."""
    for _ in range(5):
        try:
            return dict(_thread_accounted)
        except RuntimeError:  # dictionary changed size during iteration
            continue
    return {}


def _writer(stop: threading.Event, period: float) -> None:
    writes = 0
    while not stop.wait(period):
        try:
            if scopes_enabled():
                instrument_plugins()
            write_snapshot()
            if writes % 10 == 0:
                prune()
            writes += 1
        except Exception:  # health: allow BLE001 -- accounting must never take a loop down
            logger.debug("loop accounting: snapshot write failed", exc_info=True)


def start(role: str) -> bool:
    """Name this process's role and start the snapshot writer (once per process; no-op when disabled).
    Returns True when a writer is running after the call."""
    _state["role"] = role
    _state["enabled"] = None  # re-read config.yaml
    if not enabled():
        return False
    period = interval_s()  # a config read: never under _lock, which every pass exit on every thread takes
    try:
        instrument()
    except Exception:  # health: allow BLE001 -- accounting must never be what ends a boot
        logger.debug("loop accounting: instrumentation failed", exc_info=True)
    with _lock:
        thread = _state.get("thread")
        if thread is not None and thread.is_alive() and _state.get("pid") == os.getpid():
            return True
        stop = threading.Event()
        thread = threading.Thread(target=_writer, args=(stop, period), name="hermes-loop-accounting",
                                  daemon=True)
        _state.update(thread=thread, stop=stop, pid=os.getpid())
    thread.start()
    if not _state.get("atexit"):
        _state["atexit"] = True
        atexit.register(_final_write)
    return True


def stop(*, write: bool = False) -> None:
    """Stop the writer (tests, shutdown); ``write`` flushes one last snapshot."""
    ev = _state.get("stop")
    thread = _state.get("thread")
    if ev is not None:
        ev.set()
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout=5)
    _state.update(thread=None, stop=None)
    if write:
        _final_write()


def _final_write() -> None:
    if _state.get("role") is None or not enabled():
        return
    try:
        write_snapshot()
    except Exception:  # health: allow BLE001 -- interpreter shutdown; the periodic file stands
        logger.debug("loop accounting: final snapshot failed", exc_info=True)


def reset() -> None:
    """Clear every counter (tests)."""
    loop_callback_latency.reset()
    http_body_accounting.reset()
    with _lock:
        _stats.clear()
        _thread_accounted.clear()
        _open_intervals.clear()
        _exited_tids.clear()
        _cb_stats.clear()
        _cb_cache.clear()
        _thread_targets.clear()
        _target_exited.clear()
        _state.pop("exited_accounted", None)


# -- residual scopes and instrumentation ----------------------------------------------------------------------

MAX_CALLBACK_KEYS = 64  # distinct main-loop callback names; later ones fold into ``loop_cb/other``
CALLBACK_OTHER = "loop_cb/other"


class Scope:
    """A residual scope (a context manager): books the CPU this thread spent inside it that no pass or scope
    nested in it already accounted, under ``name``. Unlike :class:`Pass` it never hides a nested pass: the
    nested pass keeps its own name and CPU, this scope gets only the rest, and the thread's ``accounted_cpu_s``
    gets the sum once (so a route running inside an AnyIO worker scope is not counted twice)."""

    __slots__ = ("_acc0", "_c0", "_gen", "_tid", "_w0", "name", "skipped")

    def __init__(self, name: str) -> None:
        self.name = name
        self.skipped = False

    def skip(self) -> None:
        self.skipped = True

    def __enter__(self) -> Scope:
        with _lock:
            self._tid = _own_tid()
            _fold_exited()
            self._acc0 = _thread_accounted.get(self._tid, 0.0)
            self._gen = _fork["gen"]
            self._w0 = _perf()
            self._c0 = _thread_time()
            _open_intervals.enter(self._tid, self, self.name, self._c0, self._acc0)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        with _lock:
            self._close(exc_type)

    def _close(self, exc_type) -> None:
        if self._gen == _fork["gen"]:
            cpu = _thread_time() - self._c0
            wall = _perf() - self._w0
        else:
            cpu = _thread_time() - _fork["c0"]
            wall = _perf() - _fork["w0"]
        tid = self._tid
        with _lock:
            _open_intervals.close(tid, self)
            inner = max(0.0, _thread_accounted.get(tid, 0.0) - self._acc0)
            own = max(0.0, cpu - inner)
            st = _stat(self.name)
            st.runs += 1
            st.skips += bool(self.skipped)
            if exc_type is not None:
                st.error(getattr(exc_type, "__name__", None))
            st.cpu_s += own
            st.cpu_max_s = max(st.cpu_max_s, own)
            st.wall_s += wall
            st.last_at = time.time()
            if getattr(_local, "depth", 0) <= 0:
                _add_accounted(tid, own)


def scope(name: str) -> Scope:
    """A residual scope of ``name``; see :class:`Scope`."""
    return Scope(name)


def _callable_target(fn: Any, args: tuple = ()) -> Any:
    """Inspect carriers without changing the invocation's context or custody."""
    for _ in range(12):
        if isinstance(fn, functools.partial):
            if getattr(fn, "_la_thread_scope", False) is True:
                break
            target = getattr(fn, "_la_target", None)
            if target is not None:
                fn, args = target
                continue
            args = fn.args + args
            fn = fn.func
            continue
        if (getattr(fn, "__name__", None) == "run" and type(getattr(fn, "__self__", None)).__name__ == "Context"
                and args):
            fn, args = args[0], args[1:]
            continue
        break
    return fn


def _callable_name(fn: Any, args: tuple = ()) -> str:
    """Logical function behind partial, context and physical-custody carriers."""
    fn = _callable_target(fn, args)
    if getattr(fn, "_la_thread_scope", False) is True:
        return fn._la_target_name
    qual = getattr(fn, "__qualname__", None) or type(fn).__name__
    mod = getattr(fn, "__module__", None) or ""
    return f"{mod}.{qual}" if mod else str(qual)


def _run_scoped(name: str, fn: Callable, *args: Any, **kwargs: Any) -> Any:
    try:
        observed = Scope(name)
        observed.__enter__()
    except Exception:  # health: allow BLE001 -- diagnostics cannot prevent the operation
        return fn(*args, **kwargs)
    outcome = (None, None, None)
    try:
        return fn(*args, **kwargs)
    except BaseException:
        outcome = sys.exc_info()
        raise
    finally:
        try:
            observed.__exit__(*outcome)
        except Exception:  # health: allow BLE001 -- never replace results/errors or retry the operation
            logger.debug("loop accounting: scope booking failed", exc_info=True)


def scoped_call(prefix: str, fn: Callable, args: tuple = ()) -> Callable:
    """``fn`` wrapped so each call is one :class:`Scope` named ``<prefix>/<fn qualname>`` (``args`` are only
    read, to name a ``Context.run(func, ...)`` submission by ``func``)."""
    if prefix == "thread" and getattr(_callable_target(fn, args), "_la_thread_scope", False) is True:
        return fn
    name = _callable_name(fn, args)
    wrapped: Any = functools.partial(_run_scoped, f"{prefix}/{name}", fn)
    wrapped._la_thread_scope = prefix == "thread"
    wrapped._la_target_name = name
    return wrapped


_cb_stats: dict[str, _Stat] = {}  # callback name -> counters (at most MAX_CALLBACK_KEYS + the other bucket)
_cb_cache: dict[Any, _Stat] = {}  # code object -> its counters: the hot path never builds a name twice
_CB_CACHE_MAX = 4096


def _callback_stat(cb: Any) -> _Stat:
    """The counters of the callback ``cb`` (a task step is named by its coroutine, anything else by its
    qualname). Hot path: one dict lookup keyed by the code object."""
    owner = getattr(cb, "__self__", None)
    get_coro = getattr(owner, "get_coro", None)
    name = None
    if get_coro is not None:
        coro = get_coro()
        code = getattr(coro, "cr_code", None) or getattr(coro, "gi_code", None)
        name = getattr(coro, "__qualname__", None)
    else:
        code = getattr(getattr(cb, "__func__", cb), "__code__", None)
    st = _cb_cache.get(code) if code is not None else None
    if st is not None:
        return st
    key = "loop_cb/" + (name or _callable_name(cb))
    with _lock:
        st = _cb_stats.get(key)
        if st is None:
            if len(_cb_stats) >= MAX_CALLBACK_KEYS:
                key = CALLBACK_OTHER
            st = _cb_stats.get(key)
            if st is None:
                st = _cb_stats[key] = _Stat()
        if code is not None and len(_cb_cache) < _CB_CACHE_MAX:
            _cb_cache[code] = st
    return st


_instrumented: dict[str, Any] = {}


def instrument_executors() -> bool:
    """Name direct pool, asyncio and AnyIO submissions; nested hooks share one residual scope."""
    if _instrumented.get("executors"):
        return False
    _instrumented["executors"] = True
    from concurrent.futures import ThreadPoolExecutor
    from concurrent.futures.thread import _WorkItem
    item_orig = _WorkItem.run

    def run_item(self, *args, **kwargs):
        if not scopes_enabled():
            return item_orig(self, *args, **kwargs)
        # 3.14 stores the invocation as a task triple; older CPython uses fn/args.
        try:
            if hasattr(self, "task"):
                func, call_args, _ = self.task
            else:
                func, call_args = self.fn, self.args
            name = "executor_item/" + _callable_name(func, call_args)
        except Exception:  # health: allow BLE001 -- diagnostics must not break future completion
            return item_orig(self, *args, **kwargs)
        # The submitted scope has ended when Future invokes completion callbacks.
        # Scope subtracts inner accounting, leaving only item/custody/completion CPU.
        return _run_scoped(name, item_orig, self, *args, **kwargs)

    run_item._la_orig = item_orig  # type: ignore[attr-defined]
    _WorkItem.run = run_item  # type: ignore[method-assign]
    pool_orig = ThreadPoolExecutor.submit

    def submit(self, func, /, *args, **kwargs):
        if scopes_enabled():
            func = scoped_call("thread", func, args)
        return pool_orig(self, func, *args, **kwargs)

    submit._la_orig = pool_orig  # type: ignore[attr-defined]
    ThreadPoolExecutor.submit = submit  # type: ignore[method-assign]
    import asyncio.base_events as be
    orig = be.BaseEventLoop.run_in_executor

    def run_in_executor(self, executor, func, *args):
        return orig(self, executor, scoped_call("thread", func, args), *args)

    run_in_executor._la_orig = orig  # type: ignore[attr-defined]
    be.BaseEventLoop.run_in_executor = run_in_executor  # type: ignore[method-assign]
    try:
        import anyio.to_thread as at
        a_orig = at.run_sync

        async def run_sync(func, *args, **kwargs):
            return await a_orig(scoped_call("thread", func, args), *args, **kwargs)

        run_sync._la_orig = a_orig  # type: ignore[attr-defined]
        at.run_sync = run_sync
    except Exception:  # health: allow BLE001 -- no anyio: only the asyncio executors are named
        logger.debug("loop accounting: anyio not instrumented", exc_info=True)
    return True


def instrument_callbacks() -> bool:
    """Attribute event-loop callback CPU outside any pass/scope to the callback or task coroutine that burned
    it (``loop_cb/<qualname>``, at most ``MAX_CALLBACK_KEYS`` names then ``loop_cb/other``). Idempotent."""
    if _instrumented.get("callbacks"):
        return False
    _instrumented["callbacks"] = True
    import asyncio.events as ev
    orig = ev.Handle._run

    def _run(self):
        tid = _native_id()
        acc0 = _thread_accounted.get(tid, 0.0)
        callback = self._callback  # cancellation may clear the handle during execution
        w0 = _perf()
        c0 = _thread_time()
        try:
            return orig(self)
        finally:
            cpu = _thread_time() - c0
            wall = _perf() - w0
            try:
                if wall >= loop_callback_latency.THRESHOLD_S:
                    loop_callback_latency.record(callback, wall, cpu, time.time())
                st = _callback_stat(self._callback)
                own = cpu - (_thread_accounted.get(tid, 0.0) - acc0)
                st.runs += 1
                if own > 0.0:
                    # Lock-free: only this thread writes its own entry (the snapshot only pops dead tids).
                    st.cpu_s += own
                    st.cpu_max_s = max(st.cpu_max_s, own)
                    if getattr(_local, "depth", 0) <= 0:
                        _thread_accounted[tid] = _thread_accounted.get(tid, 0.0) + own
            except Exception:  # health: allow BLE001 -- accounting must never break a loop callback
                logger.debug("loop accounting: callback booking failed", exc_info=True)

    _run._la_orig = orig  # type: ignore[attr-defined]
    ev.Handle._run = _run  # type: ignore[method-assign]
    return True


MAX_TARGET_KEYS = 256  # distinct thread targets; later ones fold into TARGET_OTHER
TARGET_OTHER = "other"


def _target_key(target: Any, args: Any = ()) -> str:
    """Name of a ``Thread`` target (``module.qualname``; a lambda also gets its line number)."""
    # Inspection must not consume one-shot iterables before Thread.run uses them.
    inspect_args = tuple(args) if isinstance(args, (tuple, list)) else ()
    target = _callable_target(target, inspect_args)
    name = _callable_name(target)
    code = getattr(getattr(target, "__func__", target), "__code__", None)
    if name.endswith("<lambda>") and code is not None:
        name = f"{name}:{code.co_firstlineno}"
    return name


def instrument_thread_targets() -> bool:
    """Name the CPU of every plain ``threading.Thread(target=fn)`` that ends up in no scope, per ``fn``
    (``residual.targets``). Wraps ``Thread.run`` once; a thread costs one lock round at its start and one at
    its end and nothing in between: a live thread's CPU is read at snapshot time and its final CPU when ``run``
    returns, so a daemon loop that never exits is still named. Subclasses that override ``run`` (AnyIO
    workers) and threads without a ``target`` are not wrapped. At most ``MAX_TARGET_KEYS`` names, then
    ``other``. Idempotent."""
    if _instrumented.get("threads"):
        return False
    _instrumented["threads"] = True
    orig = threading.Thread.run

    def run(self):
        target = getattr(self, "_target", None)
        if target is None:
            return orig(self)
        tid = _native_id()
        key: Optional[str]
        try:
            key = _target_key(target, getattr(self, "_args", ()))
            with _lock:
                if _exited_tids:
                    _fold_exited()
                if key not in _target_exited and key not in _thread_targets.values():
                    if len({*_thread_targets.values(), *_target_exited}) >= MAX_TARGET_KEYS:
                        key = TARGET_OTHER
                _thread_targets[tid] = key
        except Exception:  # health: allow BLE001 -- naming a thread must never stop it
            key = None
            logger.debug("loop accounting: thread target not named", exc_info=True)
        try:
            return orig(self)
        finally:
            if key is not None:
                try:
                    with _lock:
                        cpu = _thread_time()
                        if _exited_tids:
                            _fold_exited()
                        acc = _thread_accounted.get(tid, 0.0)
                        if _thread_targets.get(tid) == key:
                            del _thread_targets[tid]
                        g = _target_exited.setdefault(key, {"threads": 0, "cpu_s": 0.0, "accounted_cpu_s": 0.0,
                                                            "unaccounted_cpu_s": 0.0})
                        g["threads"] += 1
                        g["cpu_s"] += cpu
                        g["accounted_cpu_s"] += min(acc, cpu)
                        g["unaccounted_cpu_s"] += max(0.0, cpu - acc)
                except Exception:  # health: allow BLE001 -- accounting must never break a thread's exit
                    logger.debug("loop accounting: thread exit booking failed", exc_info=True)

    run._la_orig = orig  # type: ignore[attr-defined]
    threading.Thread.run = run  # type: ignore[method-assign]
    return True


def uninstrument() -> None:
    """Undo :func:`instrument_executors` / :func:`instrument_callbacks` / :func:`instrument_thread_targets` (tests)."""
    if _instrumented.pop("threads", None):
        threading.Thread.run = threading.Thread.run._la_orig  # type: ignore[attr-defined,method-assign]
    if _instrumented.pop("executors", None):
        from concurrent.futures import ThreadPoolExecutor
        from concurrent.futures.thread import _WorkItem
        _WorkItem.run = _WorkItem.run._la_orig  # type: ignore[attr-defined,method-assign]
        ThreadPoolExecutor.submit = ThreadPoolExecutor.submit._la_orig  # type: ignore[method-assign]
        import asyncio.base_events as be
        be.BaseEventLoop.run_in_executor = be.BaseEventLoop.run_in_executor._la_orig  # type: ignore[attr-defined]
        try:
            import anyio.to_thread as at
            at.run_sync = at.run_sync._la_orig  # type: ignore[attr-defined]
        except ImportError:  # no anyio: nothing to restore
            logger.debug("loop accounting: anyio not restored", exc_info=True)
    if _instrumented.pop("callbacks", None):
        import asyncio.events as ev
        ev.Handle._run = ev.Handle._run._la_orig  # type: ignore[attr-defined]


# Plugin call sites (hermes-mods plugins; core cannot import them): found in ``sys.modules`` by file suffix and
# wrapped in place. (file suffix, class or None, function, scope name)
_PLUGIN_SITES = (
    ("plugins/user-terminal/__init__.py", None, "sweep_jobs", "plugin/user-terminal.sweep_jobs"),
    ("plugins/loop-watch/watch.py", "LoopWatch", "check_once", "plugin/loop-watch.check_once"),
)


def wrap_function(owner: Any, attr: str, name: str) -> bool:
    """Replace ``owner.attr`` (module or class) with a wrapper running each call in :class:`Scope` ``name``.
    False when already wrapped or missing."""
    fn = getattr(owner, attr, None)
    if fn is None or getattr(fn, "_la_scope", None) == name:
        return False

    @functools.wraps(fn)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        with Scope(name):
            return fn(*args, **kwargs)

    wrapped._la_scope = name  # type: ignore[attr-defined]
    setattr(owner, attr, wrapped)
    return True


def instrument_plugins() -> int:
    """Wrap the known plugin call sites in every loaded plugin module (re-run by the snapshot writer, so a
    plugin loaded after boot is picked up). Returns how many were wrapped now."""
    n = 0
    for mod in list(sys.modules.values()):
        path = (getattr(mod, "__file__", None) or "").replace(os.sep, "/")
        if not path:
            continue
        for suffix, cls, fn, name in _PLUGIN_SITES:
            if path.endswith(suffix):
                try:
                    n += wrap_function(getattr(mod, cls) if cls else mod, fn, name)
                except Exception:  # health: allow BLE001 -- a plugin without the symbol is simply not named
                    logger.debug("loop accounting: plugin site %s not wrapped", name, exc_info=True)
    return n


def instrument() -> None:
    """Install the residual instrumentation: ``loop_accounting.scopes`` (default on: executor and plugin
    scopes) and ``loop_accounting.callbacks`` (main-loop callback attribution; default per the hub A/B)."""
    _state["scopes"] = bool(_setting("scopes", True))
    if _state["scopes"]:
        instrument_executors()
        instrument_plugins()
    if _setting("callbacks", CALLBACKS_DEFAULT):
        instrument_callbacks()
    if _setting("thread_targets", THREAD_TARGETS_DEFAULT):
        instrument_thread_targets()


CALLBACKS_DEFAULT = True
THREAD_TARGETS_DEFAULT = True  # per-target thread naming: two lock rounds per thread lifetime, see the docstring


def _after_fork_child() -> None:
    global _lock, _started_at
    _lock = threading.RLock()
    old_tid = getattr(getattr(_local, "exit_mark", None), "tid", 0)
    _stats.clear()
    _thread_accounted.clear()
    _cb_stats.clear()
    _cb_cache.clear()
    _thread_targets.clear()
    _target_exited.clear()
    _started_at = time.time()
    _state.pop("exited_accounted", None)
    _state.update(thread=None, stop=None, pid=os.getpid(), role=None)
    depth = getattr(_local, "depth", 0)  # this thread's open passes survive on the child's stack
    _local.__dict__.clear()  # drops this thread's _ThreadExit, queueing the parent's tid: cleared next
    _local.depth = depth  # child-opened passes remain nested until the inherited outer pass exits
    _exited_tids.clear()
    _fork.update(gen=_fork["gen"] + 1, c0=_thread_time(), w0=_perf())
    _open_intervals.after_fork(old_tid, _native_id(), _fork["c0"])


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_child)
