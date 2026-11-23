"""In-process, scoped evidence that a native Kanban result consumer is healthy.

A successful poll is not a delivery guarantee. Consumers use this point-in-time
receipt together with authoritative subscription/worker state. No probes here
start a poller, claim events, open databases or send messages.
"""
from pathlib import Path
import threading
from tui_gateway.notification_loaded_executor import current_consumer
import time

_lock = threading.RLock()
_consumers: dict = {}
_MAX_AGE = 15.0
# Monotonic generation of the consumer set and its receipts. Any change that can
# invalidate an earlier health read (replacement, Stop, disconnect, refusal,
# finalize, scope change) advances it, so a reader can detect "same answer,
# different consumer" across two reads.
_epoch = 0
_next_incarnation = 0


def register(session: dict, stop: threading.Event, thread: threading.Thread) -> None:
    """Called by the native poller launcher, before the thread starts."""
    global _epoch, _next_incarnation
    with _lock:
        _prune()
        _next_incarnation += 1
        _epoch += 1
        _consumers[thread] = (session, stop, {}, _next_incarnation)


def _prune() -> None:
    global _epoch
    for thread, (_, stop, _, _) in list(_consumers.items()):
        if stop.is_set() or thread.ident is not None and not thread.is_alive():
            del _consumers[thread]
            _epoch += 1


def _scope(session: dict, home: Path, session_key: str) -> bool:
    return (session.get("session_key") == session_key
            and bool(session.get("profile_home"))
            and Path(session["profile_home"]).resolve() == home.resolve()
            and not session.get("_finalized")
            and session.get("_kanban_refused_until", 0) <= time.monotonic())


def polled(home: Path, session_key: str, board_path: Path) -> None:
    """Only the registered native thread can publish its successful board read."""
    with _lock:
        entry = _consumers.get(current_consumer())
        if entry is None:
            return
        session, stop, receipts, _ = entry
        if not stop.is_set() and _scope(session, Path(home), session_key):
            receipts[(str(Path(home).resolve()), session_key, str(Path(board_path).resolve()))] = time.monotonic()


def available(home: Path, session_key: str, board_path: Path) -> bool:
    """Read-only current consumer proof; missing/stale/wrong-scope is false."""
    with _lock:
        _prune()
        now = time.monotonic()
        for thread, (session, stop, receipts, _) in _consumers.items():
            stamp = receipts.get((str(Path(home).resolve()), session_key, str(Path(board_path).resolve())))
            if (thread.is_alive() and not stop.is_set()
                    and _scope(session, Path(home), session_key)
                    and stamp is not None and 0 <= now - stamp <= _MAX_AGE):
                return True
    return False


def snapshot(home: Path, session_key: str, board_path: Path) -> dict:
    """Nonconsuming, scoped consumer health with an invalidation epoch.

    ``available`` matches :func:`available`. ``epoch`` advances on every
    registration, replacement, Stop/disconnect (stopped or dead poller) and on a
    scope loss (refusal backoff, finalize, rotated session or home) that discards
    the consumer's receipt, so a receipt minted before an invalidation is never
    reported again after recovery: it needs a fresh successful poll.
    ``incarnation`` identifies the registered consumer that holds the receipt.
    """
    key = (str(Path(home).resolve()), session_key, str(Path(board_path).resolve()))
    with _lock:
        _prune()
        now = time.monotonic()
        for thread, (session, stop, receipts, incarnation) in sorted(
                _consumers.items(), key=lambda item: item[1][3], reverse=True):  # newest first
            if session.get("session_key") != session_key:
                continue
            if not _scope(session, Path(home), session_key):
                continue
            stamp = receipts.get(key)
            if thread.is_alive() and not stop.is_set() and stamp is not None and 0 <= now - stamp <= _MAX_AGE:
                return {"available": True, "epoch": _epoch, "incarnation": incarnation, "age": now - stamp}
        return {"available": False, "epoch": _epoch, "incarnation": None, "age": None}


def invalidate(session: dict) -> None:
    """Drop the session's receipts the moment its scope is lost.

    Refusal backoff and finalize call this, so a receipt minted before them is never
    reported again after the scope recovers (it needs a fresh poll), and a reader
    holding the earlier epoch sees the change even if it never read during the outage.
    """
    global _epoch
    with _lock:
        for entry in _consumers.values():
            if entry[0] is session:
                entry[2].clear()
        _epoch += 1
