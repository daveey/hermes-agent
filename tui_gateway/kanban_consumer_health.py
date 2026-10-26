"""In-process, scoped evidence that a native Kanban result consumer is healthy.

A successful poll is not a delivery guarantee. Consumers use this point-in-time
receipt together with authoritative subscription/worker state. No probes here
start a poller, claim events, open databases or send messages.
"""
from pathlib import Path
import threading
import time

_lock = threading.RLock()
_consumers: dict = {}
_MAX_AGE = 15.0


def register(session: dict, stop: threading.Event, thread: threading.Thread) -> None:
    """Called by the native poller launcher, before the thread starts."""
    with _lock:
        _prune()
        _consumers[thread] = (session, stop, {})


def _prune() -> None:
    for thread, (_, stop, _) in list(_consumers.items()):
        if stop.is_set() or thread.ident is not None and not thread.is_alive():
            del _consumers[thread]


def _scope(session: dict, home: Path, session_key: str) -> bool:
    return (session.get("session_key") == session_key
            and bool(session.get("profile_home"))
            and Path(session["profile_home"]).resolve() == home.resolve()
            and not session.get("_finalized")
            and session.get("_kanban_refused_until", 0) <= time.monotonic())


def polled(home: Path, session_key: str, board_path: Path) -> None:
    """Only the registered native thread can publish its successful board read."""
    with _lock:
        entry = _consumers.get(threading.current_thread())
        if entry is None:
            return
        session, stop, receipts = entry
        if not stop.is_set() and _scope(session, Path(home), session_key):
            receipts[(str(Path(home).resolve()), session_key, str(Path(board_path).resolve()))] = time.monotonic()


def available(home: Path, session_key: str, board_path: Path) -> bool:
    """Read-only current consumer proof; missing/stale/wrong-scope is false."""
    with _lock:
        _prune()
        now = time.monotonic()
        for thread, (session, stop, receipts) in _consumers.items():
            stamp = receipts.get((str(Path(home).resolve()), session_key, str(Path(board_path).resolve())))
            if (thread.is_alive() and not stop.is_set()
                    and _scope(session, Path(home), session_key)
                    and stamp is not None and 0 <= now - stamp <= _MAX_AGE):
                return True
    return False
