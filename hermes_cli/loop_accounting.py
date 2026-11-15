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

**Cost.** One pass is two ``thread_time`` and two ``perf_counter`` reads, a thread-local depth and one
uncontended lock: a few microseconds (``tests/hermes_cli/test_loop_accounting_bench.py`` pins <= 20 µs).
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


_lock = threading.Lock()
_stats: dict[str, _Stat] = {}
_thread_accounted: dict[int, float] = {}  # native thread id -> CPU of its outermost passes
_local = threading.local()
_exited_tids: collections.deque = collections.deque()  # tids whose thread ended; folded under _lock
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

    __slots__ = ("_c0", "_gen", "_outer", "_w0", "changed", "fell_back", "name", "nrows", "skipped")

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
        self._c0 = _thread_time()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
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
    return name, tid, _thread_time(), _perf(), acc0


def span_close(token: tuple, *, error: bool = False, error_kind: Optional[str] = None) -> bool:
    """Close a :func:`span_open` token. False (nothing recorded) when called on another thread. ``error_kind``
    names the exception class of an ``error`` (else it counts as ``unknown``)."""
    name, tid, c0, w0, acc0 = token
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
        return {name: st.as_dict() for name, st in _stats.items()}


def _clk_tck() -> float:
    try:
        return float(os.sysconf("SC_CLK_TCK"))
    except (AttributeError, OSError, ValueError):
        return 100.0


def _proc_stat_cpu(path: str, hz: float) -> Optional[tuple[str, float]]:
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
    return comm, (int(fields[11]) + int(fields[12])) / hz


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
        libc = ctypes.CDLL(None)

        class _ThreadInfo(ctypes.Structure):
            _fields_ = [("user", ctypes.c_uint64), ("system", ctypes.c_uint64), ("cpu_usage", ctypes.c_int32),
                        ("policy", ctypes.c_int32), ("run_state", ctypes.c_int32), ("flags", ctypes.c_int32),
                        ("sleep_time", ctypes.c_int32), ("curpri", ctypes.c_int32), ("priority", ctypes.c_int32),
                        ("maxpriority", ctypes.c_int32), ("name", ctypes.c_char * 64)]

        class _Timebase(ctypes.Structure):
            _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]

        tb = _Timebase()
        libc.mach_timebase_info(ctypes.byref(tb))
        scale = (tb.numer / tb.denom if tb.denom else 1.0) / 1e9
        pid = os.getpid()
        ids = (ctypes.c_uint64 * 1024)()
        got = lib.proc_pidinfo(pid, 6, 0, ids, ctypes.sizeof(ids))  # PROC_PIDLISTTHREADS
        if got <= 0:
            return []
        out = []
        for i in range(got // 8):
            info = _ThreadInfo()
            if lib.proc_pidinfo(pid, 15, ids[i], ctypes.byref(info), ctypes.sizeof(info)) <= 0:  # THREADID64INFO
                continue
            out.append((int(ids[i]), info.name.decode("utf-8", "replace"), (info.user + info.system) * scale))
        total = sum(c for _, _, c in out)
        if total > (os.times().user + os.times().system) * 1.25 + 1.0:
            return []
        return out
    except Exception:  # health: allow BLE001 -- best effort; no per-thread counters is a documented state
        return []


def thread_cpu() -> list[dict[str, Any]]:
    """Every live thread of this process: native id, name and CPU seconds (user + system), plus the CPU its
    outermost accounted passes took. Linux reads ``/proc/self/task``; elsewhere only live accounted threads are
    listed (their ``cpu_s`` is None)."""
    names = {t.native_id: t.name for t in threading.enumerate() if t.native_id is not None}
    with _lock:
        _fold_exited()
        accounted_by = dict(_thread_accounted)
    out: list[dict[str, Any]] = []
    hz = _clk_tck()
    task_dir = "/proc/self/task"
    try:
        tids = [int(t) for t in os.listdir(task_dir) if t.isdigit()]
    except OSError:
        tids = []
    for tid in sorted(tids):
        got = _proc_stat_cpu(f"{task_dir}/{tid}/stat", hz)
        if got is None:
            continue
        comm, cpu = got
        out.append({"tid": tid, "name": names.get(tid, comm), "cpu_s": round(cpu, 3),
                    "accounted_cpu_s": round(accounted_by.get(tid, 0.0), 6)})
    if not tids and sys.platform == "darwin":
        for tid, name, cpu in _darwin_thread_cpu():
            out.append({"tid": tid, "name": names.get(tid) or name or "?", "cpu_s": round(cpu, 3),
                        "accounted_cpu_s": round(accounted_by.get(tid, 0.0), 6)})
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
        if live_tids:
            for tid in [tid for tid in _thread_accounted if tid not in live_tids]:
                _state["exited_accounted"] = _state.get("exited_accounted", 0.0) + _thread_accounted.pop(tid)
        accounted_total = sum(_thread_accounted.values()) + _state.get("exited_accounted", 0.0)
        accounted_live = sum(_thread_accounted.get(t["tid"], 0.0) for t in threads)
    process_cpu = times.user + times.system
    return {
        "version": SCHEMA_VERSION,
        "role": _state["role"] or _role_guess(),
        "pid": os.getpid(),
        "started_at": round(_started_at, 3),
        "process_start_time": _process_start_time(),
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
        "residual": _residual(threads, process_cpu, accounted_total, procfs),
        "loops": counters(),
    }


def _residual(threads: list[dict[str, Any]], process_cpu: float, accounted_total: float,
              have_cpu: bool) -> dict[str, Any]:
    """CPU no accounting scope owns, per thread-name family. ``families[f].unaccounted_cpu_s`` is that family's
    thread CPU minus the CPU its outermost scopes accounted (clamped at 0, so never negative); ``exited`` holds
    the threads gone since start (process CPU not on a live thread, against the accounted CPU they folded in)."""
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
    with _lock:
        exited_acc = _state.get("exited_accounted", 0.0)
    exited = {"cpu_s": max(0.0, process_cpu - live), "accounted_cpu_s": exited_acc}
    total = 0.0
    for f in list(fams.values()) + [exited]:
        f["unaccounted_cpu_s"] = max(0.0, f["cpu_s"] - f["accounted_cpu_s"])
        total += f["unaccounted_cpu_s"]
        for k in ("cpu_s", "accounted_cpu_s", "unaccounted_cpu_s"):
            f[k] = round(f[k], 6)
    out.update(families=fams, exited=exited, unaccounted_cpu_s=round(total, 6))
    return out


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


def _writer(stop: threading.Event, period: float) -> None:
    writes = 0
    while not stop.wait(period):
        try:
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
    with _lock:
        _stats.clear()
        _thread_accounted.clear()
        _exited_tids.clear()
        _cb_keys.clear()
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
        self._tid = _own_tid()
        self._acc0 = _thread_accounted.get(self._tid, 0.0)
        self._gen = _fork["gen"]
        self._w0 = _perf()
        self._c0 = _thread_time()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._gen == _fork["gen"]:
            cpu = _thread_time() - self._c0
            wall = _perf() - self._w0
        else:
            cpu = _thread_time() - _fork["c0"]
            wall = _perf() - _fork["w0"]
        tid = self._tid
        with _lock:
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


def _callable_name(fn: Any) -> str:
    """``module.qualname`` of what a thread pool will run (``partial`` / ``Context.run`` unwrapped)."""
    for _ in range(6):
        if isinstance(fn, functools.partial):
            inner = fn.func
            is_ctx_run = (getattr(inner, "__name__", None) == "run"
                          and type(getattr(inner, "__self__", None)).__name__ == "Context")
            fn = fn.args[0] if is_ctx_run and fn.args else inner
            continue
        break
    qual = getattr(fn, "__qualname__", None) or type(fn).__name__
    mod = getattr(fn, "__module__", None) or ""
    return f"{mod}.{qual}" if mod else str(qual)


def _run_scoped(name: str, fn: Callable, *args: Any, **kwargs: Any) -> Any:
    with Scope(name):
        return fn(*args, **kwargs)


def scoped_call(prefix: str, fn: Callable) -> Callable:
    """``fn`` wrapped so each call is one :class:`Scope` named ``<prefix>/<fn qualname>``."""
    return functools.partial(_run_scoped, f"{prefix}/{_callable_name(fn)}", fn)


def _callback_key(cb: Any) -> str:
    owner = getattr(cb, "__self__", None)
    get_coro = getattr(owner, "get_coro", None)
    if get_coro is not None:
        try:
            coro = get_coro()
            return f"loop_cb/{getattr(coro, '__qualname__', None) or type(coro).__name__}"
        except Exception:  # health: allow BLE001 -- a label only
            logger.debug("loop accounting: callback label unavailable", exc_info=True)
    return "loop_cb/" + _callable_name(cb)


_cb_keys: set = set()


def _book_callback(key: str, cpu: float, wall: float, tid: int, acc0: float) -> None:
    with _lock:
        own = max(0.0, cpu - max(0.0, _thread_accounted.get(tid, 0.0) - acc0))
        if key not in _cb_keys:
            if len(_cb_keys) >= MAX_CALLBACK_KEYS:
                key = CALLBACK_OTHER
            else:
                _cb_keys.add(key)
        st = _stat(key)
        st.runs += 1
        st.cpu_s += own
        st.cpu_max_s = max(st.cpu_max_s, own)
        st.wall_s += wall
        st.last_at = time.time()
        if own > 0.0 and getattr(_local, "depth", 0) <= 0:
            _add_accounted(tid, own)


_instrumented: dict[str, Any] = {}


def instrument_executors() -> bool:
    """Name the submitted function of every ``loop.run_in_executor`` (so ``asyncio.to_thread``) and
    ``anyio.to_thread.run_sync`` call as a residual scope ``thread/<qualname>``. Idempotent."""
    if _instrumented.get("executors"):
        return False
    _instrumented["executors"] = True
    import asyncio.base_events as be
    orig = be.BaseEventLoop.run_in_executor

    def run_in_executor(self, executor, func, *args):
        return orig(self, executor, scoped_call("thread", func), *args)

    run_in_executor._la_orig = orig  # type: ignore[attr-defined]
    be.BaseEventLoop.run_in_executor = run_in_executor  # type: ignore[method-assign]
    try:
        import anyio.to_thread as at
        a_orig = at.run_sync

        async def run_sync(func, *args, **kwargs):
            return await a_orig(scoped_call("thread", func), *args, **kwargs)

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
        w0 = _perf()
        c0 = _thread_time()
        try:
            return orig(self)
        finally:
            cpu = _thread_time() - c0
            try:
                _book_callback(_callback_key(self._callback), cpu, _perf() - w0, tid, acc0)
            except Exception:  # health: allow BLE001 -- accounting must never break a loop callback
                logger.debug("loop accounting: callback booking failed", exc_info=True)

    _run._la_orig = orig  # type: ignore[attr-defined]
    ev.Handle._run = _run  # type: ignore[method-assign]
    return True


def uninstrument() -> None:
    """Undo :func:`instrument_executors` / :func:`instrument_callbacks` (tests)."""
    if _instrumented.pop("executors", None):
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
    if _setting("scopes", True):
        instrument_executors()
        instrument_plugins()
    if _setting("callbacks", CALLBACKS_DEFAULT):
        instrument_callbacks()


CALLBACKS_DEFAULT = True


def _after_fork_child() -> None:
    global _lock, _started_at
    _lock = threading.Lock()
    _stats.clear()
    _thread_accounted.clear()
    _cb_keys.clear()
    _started_at = time.time()
    _state.pop("exited_accounted", None)
    _state.update(thread=None, stop=None, pid=os.getpid(), role=None)
    depth = getattr(_local, "depth", 0)  # this thread's open passes survive on the child's stack
    _local.__dict__.clear()  # drops this thread's _ThreadExit, queueing the parent's tid: cleared next
    _local.depth = depth  # child-opened passes remain nested until the inherited outer pass exits
    _exited_tids.clear()
    _fork.update(gen=_fork["gen"] + 1, c0=_thread_time(), w0=_perf())


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_child)
