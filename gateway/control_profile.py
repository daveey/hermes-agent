"""On-demand wall-stack samples and per-thread CPU deltas (no source or locals)."""
from __future__ import annotations

from collections import Counter
from functools import lru_cache
import ctypes
import os
import json
import sys
import threading
import time


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


def _collect(seconds, hz, top):
    records = {}
    samples = 0
    deadline = time.monotonic() + seconds
    next_tick = time.monotonic()
    code_keys = {}
    previous_frames = {}
    frames = {}
    previous_stacks = {}
    while time.monotonic() < deadline:
        threads = {t.ident: t for t in threading.enumerate()}
        frames = sys._current_frames()
        current_stacks = {}
        for ident, frame in frames.items():
            thread = threads.get(ident)
            if thread is None or thread.native_id is None:
                continue
            key = (thread, thread.native_id)
            cpu = _cpu_seconds(thread.native_id)
            if key not in records:
                records[key] = dict(name=thread.name, native_id=thread.native_id,
                                    samples=0, first_seen_sample=samples,
                                    stack_counts={},
                                    cpu_start=cpu, cpu_last=cpu)
            row = records[key]
            row['samples'] += 1
            row['last_seen_sample'] = samples
            unchanged = cpu is not None and cpu == row['cpu_last'] and previous_frames.get(ident) is frame
            if cpu is not None:
                row['cpu_last'] = cpu
            if unchanged and ident in previous_stacks:
                stack_key = previous_stacks[ident]
            else:
                stack = []
                while frame is not None:
                    code = frame.f_code
                    code_id = id(code)
                    if code_id not in code_keys:
                        # Avoid expensive structural code-object hashing on every tick.
                        code_keys[code_id] = (code, f'{code.co_filename}:{code.co_firstlineno}:{code.co_name}')
                    stack.append(code_id)
                    frame = frame.f_back
                stack_key = tuple(stack)
            current_stacks[ident] = stack_key
            counts = row['stack_counts']
            counts[stack_key] = counts.get(stack_key, 0) + 1
        # Retain only the preceding snapshot to compare real frame identity (not
        # reusable ids). Reuse its stack only if the native CPU clock has not moved:
        # a resumed generator can keep its leaf frame but acquire a different caller.
        # No locals/source are read; all frame references are cleared at window end.
        previous_frames = frames
        previous_stacks = current_stacks
        samples += 1
        next_tick += 1 / hz
        time.sleep(max(0, min(next_tick, deadline) - time.monotonic()))
    previous_frames.clear()
    frames.clear()
    frame = None
    output = []
    for (thread, native_id), row in records.items():
        end = _cpu_seconds(native_id) if thread.is_alive() else None
        start = row.pop('cpu_start')
        last = row.pop('cpu_last') if end is None else end
        row.pop('cpu_last', None)
        row['cpu_s'] = max(0, last - start) if start is not None and last is not None else None
        row['cpu_partial'] = end is None or start is None
        self_counts, inclusive_counts = Counter(), Counter()
        for stack, count in row.pop('stack_counts').items():
            self_counts[code_keys[stack[0]][1]] += count
            for frame_key in {code_keys[c][1] for c in stack}:
                inclusive_counts[frame_key] += count
        row['top_self'] = self_counts.most_common(top)
        row['top_inclusive'] = inclusive_counts.most_common(top)
        output.append(row)
    output.sort(key=lambda r: r['cpu_s'] or 0, reverse=True)
    return dict(seconds=seconds, hz=hz, samples=samples, threads=output)


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
    seconds, hz, top = (params.get('seconds', 20), params.get('hz', 100), params.get('top', 40))
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
