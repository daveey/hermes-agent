"""Bound remote-notice structural probes across pollers, never their claims. [daveey]"""
from __future__ import annotations

import contextlib
import os
import threading
import time
from pathlib import Path
from collections import OrderedDict
from dataclasses import dataclass

_SECONDS = 2.0
_MAX_SCOPES = 32
_clock = time.monotonic
_lock = threading.RLock()
_entries: OrderedDict = OrderedDict()


@dataclass
class Window:
    started: float
    stamp: object
    computed: tuple | None = None


@contextlib.contextmanager
def borrow(path, ledger, *, enabled=True):
    """Hold the structural lane only; release before any per-chat ledger/delivery work.

    Scope includes the replica AND observation ledger. A failed probe is not cached.
    Expiry starts before I/O, so slow builds cannot extend the two-second window.
    A bounded process-wide lane avoids duplicate builds even during concurrent cold polls.
    The emergency opt-out performs the original probes/builds on every call.
    """
    from tui_gateway.kanban_replica_observer import fingerprint

    if not enabled:
        yield Window(_clock(), fingerprint(path))
        return
    if os.environ.get("HERMES_NOTIFICATION_SHARED_WINDOW") == "0":  # health: allow HX002 -- explicitly requested reversible emergency env flag
        with _lock:
            _entries.clear()  # re-enabling must not resurrect an old window
        yield Window(_clock(), fingerprint(path))
        return
    # Preserve path spelling (including symlink/.. semantics) used by the readers.
    key = (str(Path(path).absolute()), str(Path(ledger).absolute()))
    with _lock:
        now = _clock()
        entry = _entries.pop(key, None)
        if entry is None or not 0 <= now - entry.started < _SECONDS:
            entry = Window(now, fingerprint(path))
        # Unknown fingerprints must retry literally, not certify a quiet replica.
        if entry.stamp is not None:
            _entries[key] = entry
            while len(_entries) > _MAX_SCOPES:
                _entries.popitem(last=False)
        try:
            yield entry
        except BaseException:
            _entries.pop(key, None)
            raise


def reset():
    with _lock:
        _entries.clear()
