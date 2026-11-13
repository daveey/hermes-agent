"""Memoized gateway liveness for polled per-profile status reads (design t_01a1d563324d7e8e §3, card J). [daveey]

``hermes_cli.profiles._check_gateway_running`` runs for every profile on every polled ``profiles.list`` /
``GET /api/profiles``; each call re-ran ``get_running_pid``'s flock probe and process command-line identity scan
(0.4% of a core on the hub serve for answers that almost never change). The answer is a function of the PID and
lock files plus the named process staying alive, so it is reused while:

- the PID file and its sibling lock file keep their identity (dev, ino, mtime_ns, size): a gateway that starts,
  stops cleanly or is replaced rewrites one of them;
- a cached live PID is still the same live process: on Linux one ``/proc/<pid>/stat`` read (not a zombie, same
  start ticks); elsewhere ``kill(pid, 0)`` plus the start-time PID-reuse guard plus its runtime lock still held
  (a zombie has released its lock). A gateway that died without touching its files is noticed on the next call;
- at most ``RECHECK_S`` has passed (backstop for anything the key cannot see, e.g. an identity matcher that
  would now adopt a record it rejected).

Only ``cleanup_stale=False`` probes are memoized: a cleaning probe unlinks files and must run every time.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Callable, Optional

RECHECK_S = 30.0
_lock = threading.Lock()
# (str(pid_path), probe) -> (signature, pid, start_time, checked_at)
_memo: dict[tuple[str, object], tuple[tuple, Optional[int], Optional[int], float]] = {}
_MAX_ENTRIES = 256


def _file_key(path: Path) -> tuple:
    try:
        st = path.stat()
    except OSError:
        return (None,)
    return (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size)


def _signature(pid_path: Path) -> tuple:
    from gateway.status import _get_gateway_lock_path
    return (_file_key(pid_path), _file_key(_get_gateway_lock_path(pid_path)))


def _pid_alive(pid: int) -> bool:
    """``kill(pid, 0)`` on POSIX (the full ``_pid_exists`` costs a psutil status read, ~35 µs); a zombie passes
    here but has released its runtime lock, which the caller checks next."""
    from gateway.status import _pid_exists
    if os.name == "nt":
        return _pid_exists(pid)
    try:
        os.kill(pid, 0)  # windows-footgun: ok — the nt branch above returns first
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return _pid_exists(pid)
    return True


def _proc_stat(pid: int) -> Optional[tuple[bytes, int]]:
    """``(state, start ticks)`` from one ``/proc/<pid>/stat`` read (Linux); None where there is no /proc."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_bytes().rsplit(b")", 1)[1].split()
        return fields[0], int(fields[19])
    except FileNotFoundError:
        if Path("/proc/self/stat").exists():
            return b"", -1  # procfs is there and the pid is gone
        return None
    except (IndexError, ValueError, OSError):
        return None


def _still_alive(pid_path: Path, pid: int, start_time: Optional[int]) -> bool:
    """The cached gateway is still the same live process. Linux: one ``/proc/<pid>/stat`` read (not a zombie,
    same start ticks), ~10 µs. Elsewhere: ``kill(pid, 0)`` + start time + the runtime lock still held (a zombie
    has released its lock)."""
    from gateway.status import _get_gateway_lock_path, _get_process_start_time, is_gateway_runtime_lock_active
    stat = _proc_stat(pid)
    if stat is not None:
        state, ticks = stat
        return state not in (b"", b"Z", b"X", b"x") and (start_time is None or ticks == start_time)
    if not _pid_alive(pid):
        return False
    if start_time is not None and _get_process_start_time(pid) != start_time:
        return False
    return is_gateway_runtime_lock_active(_get_gateway_lock_path(pid_path))


def _default_probe(path: Path) -> Optional[int]:
    from gateway.status import get_running_pid
    return get_running_pid(path, cleanup_stale=False)


def running_pid(pid_path: Path, *, probe: Optional[Callable[[Path], Optional[int]]] = None,
                now: Optional[float] = None) -> Optional[int]:
    """``get_running_pid(pid_path, cleanup_stale=False)``, memoized as described in the module docstring. The
    probe in effect is part of the key: an answer is never shared across probes (or a replaced ``get_running_pid``)."""
    from gateway import status
    key = (str(pid_path), probe or status.get_running_pid)
    probe = probe or _default_probe
    now = time.monotonic() if now is None else now
    signature = _signature(pid_path)
    with _lock:
        hit = _memo.get(key)
    if hit is not None and hit[0] == signature and now - hit[3] < RECHECK_S:
        pid, start_time = hit[1], hit[2]
        if pid is None or _still_alive(pid_path, pid, start_time):
            return pid
    pid = probe(pid_path)
    start_time = status._get_process_start_time(pid) if pid is not None else None
    with _lock:
        if _signature(pid_path) == signature:
            if len(_memo) >= _MAX_ENTRIES and key not in _memo:
                _memo.clear()
            _memo[key] = (signature, pid, start_time, now)
        else:  # the files moved during the probe: answer, but never memoize a racy read
            _memo.pop(key, None)
    return pid


def clear() -> None:
    with _lock:
        _memo.clear()
