"""On-demand wall-stack samples and per-thread CPU deltas (no source or locals)."""
from __future__ import annotations

from collections import Counter
from functools import lru_cache
import ctypes
import inspect
import os
import json
import sys
import threading
import time
import traceback


# Darwin <sys/proc_info.h>. PROC_PIDTHREADINFO (5) matches its arg against the
# thread's TSD handle (machine.cthread_self, as listed by PROC_PIDLISTTHREADS);
# PROC_PIDTHREADID64INFO (15) matches the 64-bit kernel thread_id, which is what
# pthread_threadid_np() and so threading.native_id return. Same struct for both.
_PROC_PIDTHREADID64INFO = 15


class _ProcThreadInfo(ctypes.Structure):
    # struct proc_threadinfo (112 bytes). Times are nanoseconds.
    _fields_ = [('user_time', ctypes.c_uint64), ('system_time', ctypes.c_uint64),
                ('cpu_usage', ctypes.c_int32), ('policy', ctypes.c_int32),
                ('run_state', ctypes.c_int32), ('flags', ctypes.c_int32),
                ('sleep_time', ctypes.c_int32), ('curpri', ctypes.c_int32),
                ('priority', ctypes.c_int32), ('maxpriority', ctypes.c_int32),
                ('name', ctypes.c_char * 64)]


@lru_cache(maxsize=1)
def _darwin_proc_pidinfo():
    # PyDLL keeps the GIL across the call. proc_pidinfo(PROC_PIDTHREADID64INFO) is a
    # bounded, non-blocking kernel read of one thread's counters, so releasing and
    # re-acquiring the GIL around it (CDLL) only adds a GIL handoff per thread per
    # tick without letting other Python code run meaningfully in between.
    library = ctypes.PyDLL('/usr/lib/libproc.dylib')
    function = library.proc_pidinfo
    function.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
    function.restype = ctypes.c_int
    return function


_PROC_THREADINFO_SIZE = ctypes.sizeof(_ProcThreadInfo)


def _cpu_seconds(native_id):
    if sys.platform == 'darwin':
        info = _ProcThreadInfo()
        # Unique, never-reused thread_id: an exited thread fails (ESRCH) instead of
        # aliasing a later thread that reuses the same pthread/TSD address.
        if _darwin_proc_pidinfo()(os.getpid(), _PROC_PIDTHREADID64INFO, native_id,
                                  ctypes.addressof(info), _PROC_THREADINFO_SIZE) != _PROC_THREADINFO_SIZE:
            return None
        return (info.user_time + info.system_time) / 1e9
    if not sys.platform.startswith('linux'):
        return None
    # Linux MAKE_THREAD_CPUCLOCK(tid, CPUCLOCK_SCHED): kernel-resolved numeric TID,
    # not pthread_getcpuclockid's dereferenced pthread_t (unsafe after thread exit).
    # An exited TID yields EINVAL/ESRCH, never a userspace use-after-free.
    try:
        return time.clock_gettime((~native_id << 3) | 6)
    except OSError:
        return None


# Sampling frequency when a request omits ``hz``: 25 Hz (40 ms wall-stack
# resolution). 50 Hz exceeded the 3%-of-one-core budget on a live 166-thread
# gateway. An explicit ``hz`` in 10..200 still overrides it.
DEFAULT_HZ = 25
# Native CPU clocks are read at a thread's first observation, then on refresh
# ticks at most this often (monotonic seconds), and once at window end.
_CPU_REFRESH_S = 0.1
# Frames of these codes can be suspended and resumed under a different caller,
# so a chain containing one is never reused from a previous snapshot.
_SUSPENDABLE = inspect.CO_GENERATOR | inspect.CO_COROUTINE | inspect.CO_ASYNC_GENERATOR | inspect.CO_ITERABLE_COROUTINE


def _read_cpu(thread, native_id):
    cpu = _cpu_seconds(native_id)
    # Checked AFTER the read: a thread that is still alive still owns its kernel
    # TID, while a dead thread's value may come from a reused Linux TID.
    return cpu if cpu is not None and thread.is_alive() else None


def _new_row(thread, native_id, sample):
    return dict(name=thread.name, native_id=native_id, samples=0, first_seen_sample=sample,
                stack_counts={}, cpu_start=None, cpu_last=None, cpu_last_sample=None, cpu_missed=False)


def _record_cpu(row, cpu, sample):
    if cpu is None:
        if row['cpu_start'] is None:
            row['cpu_missed'] = True  # a later first success is still a lower bound
        return
    if row['cpu_start'] is None:
        row['cpu_start'] = cpu
    row['cpu_last'], row['cpu_last_sample'] = cpu, sample


class _Window:
    """Per-window sampler state. Retains only the last walked frame snapshot."""

    def __init__(self):
        self.records = {}
        self.code_keys = {}  # id(code) -> (code, frame key, suspendable); keeps ids valid
        self.previous_frames = {}
        self.previous = {}  # ident -> (row, stack_key, reusable)
        self.previous_threads = {}
        self.cpu_rows = []  # active (Thread, native_id, row), rebuilt on lifetime/guard changes
        self.batchable = False
        self.run = 0  # ticks with deferred histogram updates (per-thread runs may differ)
        self.run_last = 0
        self.pending_since = {}  # ident -> first unrecorded sample in its stack run

    def flush_thread(self, ident, last):
        count = last - self.pending_since[ident] + 1
        if count:
            row, stack_key, _ = self.previous[ident]
            row['samples'] += count
            row['last_seen_sample'] = last
            counts = row['stack_counts']
            counts[stack_key] = counts.get(stack_key, 0) + count
        self.pending_since[ident] = last + 1

    def flush(self):
        if self.run:
            for ident in self.previous:
                self.flush_thread(ident, self.run_last)
            self.run = 0

    def changed_tick(self, sample, frames):
        # The registry sandwich and frame-key set were checked by slow_tick.
        # A retained plain leaf pins its entire caller chain; only changed leaves
        # need a new walk/histogram run, even when another thread wakes each tick.
        for ident, frame in frames.items():
            if frame is self.previous_frames[ident]:
                continue
            self.flush_thread(ident, sample - 1)
            row = self.previous[ident][0]
            stack_key, reusable = self.walk(frame, True)
            self.previous[ident] = (row, stack_key, reusable)
            self.batchable = self.batchable and reusable
        self.previous_frames = frames
        self.run += 1
        self.run_last = sample

    def walk(self, frame, reusable):
        stack = []
        code_keys = self.code_keys
        while frame is not None:
            code = frame.f_code
            code_id = id(code)
            entry = code_keys.get(code_id)
            if entry is None:
                # Avoid expensive structural code-object hashing on every tick.
                entry = code_keys[code_id] = (
                    code, f'{code.co_filename}:{code.co_firstlineno}:{code.co_name}',
                    bool(code.co_flags & _SUSPENDABLE))
            if entry[2]:
                reusable = False
            stack.append(code_id)
            frame = frame.f_back
        return tuple(stack), reusable

    def slow_tick(self, sample, cpu_due, no_greenlet, before, frames, after):
        if (self.batchable and no_greenlet
                and before == after == self.previous_threads
                and frames.keys() == self.previous.keys()):
            # CPU refresh and unrelated stack changes do not invalidate a stable
            # thread's run. Retain the full registry sandwich for lifetime safety.
            if cpu_due:
                for thread, native_id, row in self.cpu_rows:
                    _record_cpu(row, _read_cpu(thread, native_id), sample)
            if frames == self.previous_frames:
                self.run += 1
                self.run_last = sample
            else:
                self.changed_tick(sample, frames)
            return
        self.flush()
        current = {}
        cpu_rows = []
        batchable = no_greenlet
        for ident, frame in frames.items():
            thread = after.get(ident)
            # The same Thread object on both sides owned this ident at snapshot time.
            if thread is None or before.get(ident) is not thread or thread.native_id is None:
                if thread is not None or ident in before:
                    batchable = False  # transient: re-check next tick
                continue
            native_id = thread.native_id
            row = self.records.get((thread, native_id))
            if row is None:
                row = self.records[thread, native_id] = _new_row(thread, native_id, sample)
                _record_cpu(row, _read_cpu(thread, native_id), sample)
            elif cpu_due:
                _record_cpu(row, _read_cpu(thread, native_id), sample)
            prior = self.previous.get(ident)
            if (prior is not None and prior[2] and prior[0] is row and no_greenlet
                    and self.previous_frames.get(ident) is frame):
                stack_key, reusable = prior[1], True
            else:
                stack_key, reusable = self.walk(frame, no_greenlet)
            row['samples'] += 1
            row['last_seen_sample'] = sample
            counts = row['stack_counts']
            counts[stack_key] = counts.get(stack_key, 0) + 1
            current[ident] = (row, stack_key, reusable)
            cpu_rows.append((thread, native_id, row))
            batchable = batchable and reusable
        self.previous_frames, self.previous, self.batchable = frames, current, batchable
        self.previous_threads, self.cpu_rows = after, cpu_rows
        self.pending_since = dict.fromkeys(current, sample + 1)

    def finish(self, samples, top):
        output = []
        code_keys = self.code_keys
        for (thread, native_id), row in self.records.items():
            end = _read_cpu(thread, native_id) if thread.is_alive() else None
            if end is not None:
                if row['cpu_start'] is None:
                    row['cpu_missed'] = True
                _record_cpu(row, end, samples)
            start_cpu, last_cpu, missed = row.pop('cpu_start'), row.pop('cpu_last'), row.pop('cpu_missed')
            row['cpu_s'] = max(0, last_cpu - start_cpu) if start_cpu is not None else None
            # Lower bound when the first or the end read is missing (e.g. exited thread).
            row['cpu_partial'] = end is None or missed or start_cpu is None
            self_counts, inclusive_counts = Counter(), Counter()
            for stack, count in row.pop('stack_counts').items():
                self_counts[code_keys[stack[0]][1]] += count
                for frame_key in {code_keys[c][1] for c in stack}:
                    inclusive_counts[frame_key] += count
            row['top_self'] = self_counts.most_common(top)
            row['top_inclusive'] = inclusive_counts.most_common(top)
            output.append(row)
        output.sort(key=lambda r: r['cpu_s'] or 0, reverse=True)
        return output


def _collect(seconds, hz, top):
    window = _Window()
    samples = 0
    start = time.monotonic()
    deadline = start + seconds
    next_tick = next_cpu = start
    frames = None
    try:
        while True:
            now = time.monotonic()
            if now >= deadline:
                break
            cpu_due = now >= next_cpu
            # A refresh always takes the registry-sandwiched snapshot below; do
            # not first collect a redundant snapshot just to discard it.
            frames = None if cpu_due else sys._current_frames()
            no_greenlet = sys.modules.get('greenlet') is None
            # Batch a whole snapshot whose every chain was safe to reuse; dict ==
            # compares the frame objects by identity, in C.
            if window.batchable and no_greenlet and not cpu_due and frames == window.previous_frames:
                window.run += 1
                window.run_last = samples
            else:
                if cpu_due:
                    next_cpu += _CPU_REFRESH_S
                    if next_cpu <= now:  # late tick: skip missed refreshes, no burst
                        next_cpu = now + _CPU_REFRESH_S
                # Snapshot here, not in slow_tick: the sampler's own leaf frame
                # must be the same object as in fast-path snapshots.
                before = {t.ident: t for t in threading.enumerate()}
                frames = sys._current_frames()
                after = {t.ident: t for t in threading.enumerate()}
                window.slow_tick(samples, cpu_due, no_greenlet, before, frames, after)
                before = after = None
            frames = None
            samples += 1
            next_tick += 1 / hz
            time.sleep(max(0, min(next_tick, deadline) - time.monotonic()))
        window.flush()
    except BaseException as exc:
        # Finished callee frames in the traceback (slow_tick, walk) hold frame refs.
        traceback.clear_frames(exc.__traceback__)
        raise
    finally:
        # No frame reference outlives the window, even via an error traceback.
        frames = window.previous_frames = None
    return dict(seconds=seconds, hz=hz, cpu_refresh_s=_CPU_REFRESH_S, samples=samples,
                threads=window.finish(samples, top))


def trim_response(response, limit):
    """Keep whole identities/frame keys or omit them; never silently rewrite them.

    Reserve the complete envelope (including an arbitrary JSON request id) and
    count JSON's ASCII escaping, separators and final newline in the wire budget.
    """
    result = response['result']
    rows = result['threads']
    result['threads'] = []
    result['truncated'] = True
    result['omitted_threads'] = len(rows)
    result['omitted_frames'] = sum(len(r['top_self']) + len(r['top_inclusive']) for r in rows)
    budget = limit - len(json.dumps(response, default=str).encode()) - 2
    for row in rows:
        kept = {**row, 'top_self': [], 'top_inclusive': []}
        cost = len(json.dumps(kept, default=str).encode()) + 2
        if cost > budget:
            continue
        budget -= cost
        result['threads'].append(kept)
        result['omitted_threads'] -= 1
        for field in ('top_self', 'top_inclusive'):
            for pair in row[field]:
                cost = len(json.dumps(pair).encode()) + 2
                if cost <= budget:
                    kept[field].append(pair)
                    budget -= cost
                    result['omitted_frames'] -= 1
    return response


def validate_params(params):
    seconds, hz, top = (params.get('seconds', 20), params.get('hz', DEFAULT_HZ), params.get('top', 40))
    for name, value, low, high in [('seconds', seconds, 1, 60), ('hz', hz, 10, 200)]:
        if type(value) not in (int, float) or not low <= value <= high:
            raise ValueError(f'{name} must be a finite number in {low}..{high}')
    if type(top) is not int or top <= 0:
        raise ValueError('top must be a positive integer')
    return seconds, hz, top


_PROFILE_LOCK = threading.Lock()


def profile(params):
    seconds, hz, top = validate_params(params)
    if not _PROFILE_LOCK.acquire(blocking=False):
        raise RuntimeError('a profile is already running')
    result = []
    errors = []

    def sample():
        try:
            result.append(_collect(seconds, hz, top))
        except BaseException as exc:  # health: allow BLE001 -- transported to and re-raised by the waiting control handler
            errors.append(exc)
        finally:
            _PROFILE_LOCK.release()

    worker = threading.Thread(target=sample, name='gateway-profiler', daemon=True)  # health: allow HX012 -- process-wide diagnostic reads no profile state
    try:
        worker.start()
    except BaseException:
        _PROFILE_LOCK.release()
        raise
    worker.join()
    if errors:
        raise errors[0]
    return result[0]
