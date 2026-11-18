"""Bounded callback wall/inclusive CPU diagnostics; never added to residual CPU.

Records are emitted only by the existing periodic snapshot writer, never by a
callback logger. Every slow callback is counted; the latest 64 records survive
between writes, with explicit eviction/identity-overflow counts. No new poller.
"""
from __future__ import annotations

import asyncio
import collections
import functools
import hashlib
import os
import re
import threading
import types
from datetime import UTC, datetime
from pathlib import Path

from hermes_cli._startup_fast import project_root_str

THRESHOLD_S = 0.5
MAX_IDENTITIES = 64
MAX_RECORDS = 64
_ROOT = project_root_str() + os.sep
_SAFE_NAME = re.compile(r"[A-Za-z0-9_<>.]+\Z")
_lock = threading.Lock()
_recent: collections.deque = collections.deque(maxlen=MAX_RECORDS)
_identities: dict[str, int] = {}
_totals = {"slow_callbacks": 0, "records_dropped": 0, "identity_overflow": 0}


def identity(callback) -> str:
    """Code metadata only: no repr, task name, request args or mutable function name.

    Only source rooted in this checkout is named. Foreign/native/callable-object
    callbacks become other. Function globals and coroutine frames are never read.
    """
    for _ in range(6):
        if not isinstance(callback, functools.partial):
            break
        callback = callback.func
    owner = getattr(callback, "__self__", None) if isinstance(
        callback, (types.MethodType, types.BuiltinMethodType)
    ) or type(callback).__name__ == "TaskStepMethWrapper" else None
    if isinstance(owner, asyncio.Task):
        coro = owner.get_coro()
        code = coro.cr_code if isinstance(coro, types.CoroutineType) else None
    else:
        if isinstance(callback, types.MethodType):
            callback = callback.__func__
        code = callback.__code__ if isinstance(callback, types.FunctionType) else None
    if code is None:
        return "other"
    filename, name = code.co_filename, code.co_qualname
    if not filename.startswith(_ROOT) or len(name) > 160 or not _SAFE_NAME.fullmatch(name):
        return "other"
    relative = filename[len(_ROOT):]
    if len(relative) > 200 or ".." in relative.split(os.sep):
        return "other"
    return f"{relative}:{code.co_firstlineno}:{name}"


def record(callback, wall_s: float, cpu_s: float, ended_at: float) -> None:
    if wall_s < THRESHOLD_S:
        return
    key = identity(callback)
    occurred_at = datetime.fromtimestamp(ended_at - wall_s, UTC).isoformat()
    with _lock:
        if key not in _identities and key != "other" and len(_identities) - ("other" in _identities) >= MAX_IDENTITIES:
            key = "other"
            _totals["identity_overflow"] += 1
        _identities[key] = _identities.get(key, 0) + 1
        _totals["slow_callbacks"] += 1
        if len(_recent) == MAX_RECORDS:
            _totals["records_dropped"] += 1
        _recent.append({"identity": key, "occurred_at": occurred_at,
                        "wall_s": round(wall_s, 6), "inclusive_cpu_s": round(cpu_s, 6)})


def snapshot() -> dict:
    with _lock:
        return {"threshold_s": THRESHOLD_S, "emission": "existing snapshot cadence only",
                **_totals, "identities": dict(_identities), "recent": [dict(r) for r in _recent]}


def reset() -> None:
    with _lock:
        _recent.clear()
        _identities.clear()
        for key in _totals:
            _totals[key] = 0


def _after_fork_child() -> None:
    global _lock
    _lock = threading.Lock()
    reset()


@functools.lru_cache(maxsize=1)
def code_provenance() -> dict:
    """Content digest of the diagnostic code, sampled once, not Git/host state."""
    files = ("loop_accounting.py", "loop_callback_latency.py", "http_body_accounting.py", "web_server_transport.py")
    digest = hashlib.sha256()
    try:
        for name in files:
            digest.update(name.encode("ascii"))
            digest.update(Path(__file__).with_name(name).read_bytes())
    except OSError:
        return {"files": list(files), "sha256": None}
    return {"files": list(files), "sha256": digest.hexdigest()}


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_child)
