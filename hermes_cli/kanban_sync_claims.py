"""Claims wait for this process's first sync round at the feed head (synced primary boards). [daveey]

Incident (2026-11-13, autoscaled worker-a1, t_01a1d6c4e700958e): a fresh host's first sync round rebuilds the
replica from a full ``/snapshot`` (cursor 0: Resync) and resyncs the live board, which can take 15+ min on a small
worker. Its dispatcher leased the scaler's canary 7 s after it was created, the run ended rate-limited 14 s later,
and that run row sat in the outbox for 15 min: the push before a round's pulls holds every row from the first
``task_runs`` row on (``kanban_sync_backlog.sendable_before_head``), so it went out only with the push at the end of
that first round. The Board kept the lease meanwhile, the canary read ``running`` and the enrollment failed.

So a process that holds this host's sync lock claims nothing on a ``primary`` board until one
of its rounds on that board COMPLETED with the replica and live pulls at the feed head (``note_round``). The gate
fails closed: before the loop's first round, during it, and after a round that ended behind. Once open it stays open
for that lock tenure (a later catch-up, e.g. after a Mac sleeps, keeps claiming as before).
Watchers waiting for the lock use its current owner's record; without a live record they fail closed.

- A process with no sync loop (``hermes kanban claim``/``dispatch`` from a shell) is not gated here, unless another
  live process's loop recorded the board held in :func:`state_path` (a profile gateway can hold the dispatcher lock
  while the root gateway holds the sync lock): then it reads that record.
- ``hermes kanban sync status`` prints the gate (``claims:``) from the same record.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

_log = logging.getLogger(__name__)
_FILE = ".sync-claims.json"
HELD = "held (first round not at head)"
DEFERRAL = "sync_first_round"


class _State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.expected = False  # watcher may be waiting for another process's lock
        self.loop = False  # this process actually holds the host sync lock
        self.open: dict[str, str] = {}  # realpath -> slug: a completed round of this process reached the head
        self.logged: set[str] = set()  # boards whose hold was logged (one INFO line per board per hold)


_STATE = _State()


def _key(path: Any) -> str:
    return os.path.realpath(str(path))


def state_path() -> Path:
    """Beside the host's ``.sync.lock`` (one sync loop per host)."""
    from hermes_cli import kanban_db as kb
    return kb.kanban_home() / "kanban" / _FILE


def loop_expected() -> None:
    """This process will run the sync loop (the gateway's sync watcher, before it holds the host sync lock): its
    dispatcher holds claims from now on, so a tick before the loop's first round cannot slip a claim through."""
    with _STATE.lock:
        _STATE.expected = True


def loop_started() -> None:
    """The sync loop starts in this process: record that its boards hold claims until a round reaches the head."""
    loop_expected()
    with _STATE.lock:
        _STATE.loop = True
        _STATE.open.clear()
    _write()


def loop_running_here() -> bool:
    """This process's sync loop is running now (between :func:`loop_started` and :func:`loop_stopped`)."""
    with _STATE.lock:
        return _STATE.loop


def loop_stopped() -> None:
    """Release local readiness when a sync tenure ends, including normal return and crashes."""
    with _STATE.lock:
        _STATE.loop = False
        _STATE.open.clear()
    record = _read()
    if record and record.get("pid") == os.getpid():
        try:
            state_path().unlink(missing_ok=True)
        except OSError:
            _log.warning("kanban sync: cannot remove stopped claim gate", exc_info=True)


def loop_abandoned() -> None:
    """Supervision will not restart the sync loop. A crashed loop must not freeze claims."""
    with _STATE.lock:
        _STATE.expected = False
        _STATE.logged.clear()
    loop_stopped()


def claiming_bit() -> int:
    """1 when this process may claim, 0 while its sync gate is closed and no round is open."""
    with _STATE.lock:
        if _STATE.open:
            return 1
        if _STATE.expected or _STATE.loop:
            return 0
    return 1


def note_round(slug: str, path: Any, *, at_head: bool) -> None:
    """A round on this board completed; ``at_head``: its replica and live pulls reached the feed head."""
    if not at_head:
        return
    key = _key(path)
    with _STATE.lock:
        if key in _STATE.open:
            return
        _STATE.open[key] = slug
        held, loop = key in _STATE.logged, _STATE.loop
    if held:
        _log.info("kanban sync: %s reached the feed head; claims open", slug)
    if loop:  # a one-off round (``sync run``) never overwrites the running loop's record
        _write()


def held(conn: sqlite3.Connection) -> bool:
    """Whether a claim on this (primary) board must wait (the caller checks the mode). Logs once per board."""
    try:
        key = _key(next(f for _s, name, f in conn.execute("PRAGMA database_list") if name == "main"))
    except (StopIteration, sqlite3.Error):
        return False
    with _STATE.lock:
        if key in _STATE.open:
            return False
        local, expected = _STATE.loop, _STATE.expected
    elsewhere = None if local else _held_elsewhere(key)
    if not (local or (expected if elsewhere is None else elsewhere)):
        return False
    with _STATE.lock:
        first = key not in _STATE.logged
        _STATE.logged.add(key)
    if first:
        _log.info("kanban sync: claims held on %s until this host's sync round reaches the feed head "
                  "(a claim's run row would wait unsent through the catch-up)", key)
    return True


def _held_elsewhere(key: str) -> Optional[bool]:
    """Another live process's sync loop recorded this board held (no round of it at the head yet)."""
    record = _read()
    if not record or record.get("pid") == os.getpid() or not _alive(record.get("pid")):
        return None
    return key not in (record.get("open") or {})


def describe(path: Any, mode: str) -> str:
    """The gate for ``hermes kanban sync status``."""
    if mode != "primary":
        return f"not gated (mode {mode})"
    key = _key(path)
    with _STATE.lock:
        if key in _STATE.open:
            return "open"
        local, expected = _STATE.loop, _STATE.expected
    if local:
        return HELD
    record = _read()
    if not record or record.get("pid") == os.getpid() or not _alive(record.get("pid")):
        return HELD if expected else "no live sync loop recorded"
    return "open" if key in (record.get("open") or {}) else HELD


def _alive(pid: Any) -> bool:
    try:
        from hermes_cli.kanban_db_dispatch_pid import _pid_alive
        return _pid_alive(int(pid))
    except (TypeError, ValueError):
        return False


def _read() -> Optional[dict]:
    try:
        record = json.loads(state_path().read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def _write() -> None:
    with _STATE.lock:
        record = {"pid": os.getpid(), "since": time.time(), "open": dict(_STATE.open)}
    target = state_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(f"{_FILE}.{os.getpid()}.tmp")
        temp.write_text(json.dumps(record), encoding="utf-8")
        temp.replace(target)
    except OSError:
        _log.warning("kanban sync: cannot record the claim gate at %s", target, exc_info=True)


def reset() -> None:
    """Tests: a process with no sync loop."""
    global _STATE
    _STATE = _State()
