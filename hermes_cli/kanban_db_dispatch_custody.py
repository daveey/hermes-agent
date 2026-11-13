"""Local-work custody for kanban worker children spawned by ``kanban_db_dispatch._default_spawn``.

Reserve BEFORE the Popen; the token stays until the dispatcher's reap observes that exact child exit
(``Popen.poll()`` returned a code). Custody is keyed by ``(pid, birth)`` and released only for the same
handle, so a reused pid can never release another child's token. A handle that is dropped without a
verified exit latches the registry unknown (sticky) and keeps the token. No registry installed => no-ops.
"""
import threading
from typing import Any, Optional

_lock = threading.Lock()
_custody: dict[tuple[int, Any], tuple[Any, Any]] = {}  # (pid, birth) -> (proc, held)

KIND = "kanban_worker"
SOURCE = "kanban_db_dispatch._default_spawn"


def reserve():
    """Take the reservation before the child exists; None when no registry is installed."""
    from agent import local_work_agent
    return local_work_agent.reserve(KIND, SOURCE)


def abort(held) -> None:
    """Spawn raised: no child was created, so the token is released."""
    if held is not None:
        held.release()


def adopt(proc, held, birth: Optional[str]) -> None:
    """Attach custody of ``held`` to the spawned handle until its verified exit."""
    if held is None:
        return
    try:
        with _lock:
            _custody[(int(proc.pid), birth)] = (proc, held)
    except Exception:  # health: allow BLE001 -- spawn succeeded; unattachable custody must latch unknown, never leak a child
        held.mark_unknown("kanban-worker-custody-unattachable")


def _take(pid: int, proc) -> list:
    with _lock:
        keys = [k for k, (p, _h) in _custody.items() if k[0] == int(pid) and p is proc]
        return [_custody.pop(k)[1] for k in keys]


def on_reaped(pid: int, proc) -> None:
    """The dispatcher's poll proved ``proc`` exited: release its token (same handle only)."""
    for held in _take(pid, proc):
        held.release()


def on_dropped(pid: int, proc) -> None:
    """The handle was discarded without a verified exit: death is unprovable, keep the token + latch."""
    for held in _take(pid, proc):
        held.mark_unknown("kanban-worker-exit-unverified")
