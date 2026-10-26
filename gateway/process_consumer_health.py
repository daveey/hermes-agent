"""Read-only, ephemeral evidence for native push-gateway process consumers.

Registration is not evidence: an actual admitted completion and an unrevoked live
watcher for the exact canonical request are both required. No recovery/adoption.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import asyncio
import threading
import time

MAX_AGE = 15.0
_lock = threading.Lock()
_watchers = {}


@dataclass
class _Watcher:
    runner: object
    task: object
    registry: object
    snapshot: object
    revoked: bool = False
    receipt: object = None


@dataclass(frozen=True)
class _Receipt:
    adapter: object
    event: object
    stamp: float
    source_signature: tuple
    origin_signature: tuple


@contextmanager
def watching(runner, watcher, registry):
    """Only the executing native watcher registers; finally removes its evidence."""
    read = getattr(registry, "ownership_snapshot", None)
    snap = read(watcher.get("session_id")) if callable(read) else None
    entry = None
    if (snap is not None and watcher.get("notify_on_complete") is True
            and watcher.get("session_key") == snap.session_key
            and watcher.get("parent_session_id") == snap.session_id):
        entry = _Watcher(runner, asyncio.current_task(), registry, snap)
        with _lock:
            _watchers[entry.task] = entry
    try:
        yield
    finally:
        if entry is not None:
            with _lock:
                _watchers.pop(entry.task, None)


def invalidate(runner, key):
    """A user boundary permanently revokes old watchers, including in-flight admissions."""
    from tools.process_registry import process_registry
    process_registry.revoke_route_ownership(key)
    with _lock:
        for entry in _watchers.values():
            if entry.runner is runner and entry.snapshot.session_key == key:
                entry.revoked = True
                entry.receipt = None


def inbound(adapter, event, key):
    # Conservative at adapter ingress: even a queued user must revoke BEFORE the
    # runner sees it. Rejected inputs can remove proof, never grant it.
    if not event.internal:
        invalidate(getattr(adapter, "gateway_runner", None), key)


def begin_delivery(runner, evt):
    """Clear old success before attempting another delivery; capture revocation identity."""
    if evt.get("type") != "completion":
        return ()
    key, sid = evt.get("session_key"), evt.get("parent_session_id")
    with _lock:
        entries = tuple(e for e in _watchers.values() if e.runner is runner
                        and e.snapshot.session_key == key and e.snapshot.session_id == sid
                        and not e.revoked)
        token = object()
        for entry in entries:
            entry.receipt = token
        return entries, token


def _source_signature(source):
    from gateway.session_identity import identity_of
    fields = ("platform", "chat_id", "chat_type", "thread_id", "user_id", "profile", "scope_id",
              "guild_id", "parent_chat_id", "delivered_via_upstream_relay")
    return tuple(getattr(source, name, None) for name in fields) + (identity_of(source),)


def prepare_delivery(token, adapter, source):
    """Freeze admission and authoritative origin before crossing the await boundary."""
    if not token:
        return ()
    entries, marker = token
    if not entries:
        return ()
    prepared = []
    adapter._canonicalize(source)
    route = adapter._source_session_key(source)
    signature = _source_signature(source)
    for entry in entries:
        if route != entry.snapshot.session_key:
            continue
        store = entry.runner.session_store
        if not store._lock.acquire(timeout=0.05):
            continue
        try:
            current = store._entries.get(entry.snapshot.session_key)
            if current is not None and current.session_id == entry.snapshot.session_id:
                prepared.append((entry, _source_signature(current.origin)))
        finally:
            store._lock.release()
    return prepared, marker, signature


def delivered(token, adapter, event):
    """Called ONLY after native admit_internal_event returns successfully."""
    if not token:
        return
    entries, marker, signature = token
    if _source_signature(event.source) != signature:
        return
    with _lock:
        for entry, origin_signature in entries:
            if (_watchers.get(entry.task) is entry and not entry.revoked
                    and entry.receipt is marker):
                entry.receipt = _Receipt(adapter, event, time.monotonic(), signature, origin_signature)


def _route_live(entry, receipt):
    runner, snap, adapter = entry.runner, entry.snapshot, receipt.adapter
    loop = getattr(runner, "_gateway_loop", None)
    if (getattr(runner, "_running", False) is not True or loop is None or not loop.is_running()
            or entry.task.done() or entry.task.cancelling()
            or getattr(runner, "_completion_notification_batches_stopping", False)
            or getattr(adapter, "_running", False) is not True
            or not getattr(adapter, "_message_handler", None)):
        return False
    # Re-read the admitted event: adapters may replace, not only mutate, its source.
    source = receipt.event.source
    if _source_signature(source) != receipt.source_signature:
        return False
    platform = source.platform.value if hasattr(source.platform, "value") else str(source.platform)
    if runner._resolve_injection_adapter(platform, source) is not adapter:
        return False
    if str(Path(runner._resolve_profile_home_for_source(source)).resolve()) != snap.home:
        return False
    store = runner.session_store
    if not store._lock.acquire(timeout=0.05):
        return False
    try:
        current = store._entries.get(snap.session_key)
        return (current is not None and current.session_id == snap.session_id
                and _source_signature(current.origin) == receipt.origin_signature)
    finally:
        store._lock.release()


def receipt_version(snapshot, *, expected=None):
    """Bounded pure snapshot; identity returned only while the native consumer is live.

    The caller must separately compare the latest canonical user row, then repeat
    this read with expected=version after its other independent observations.
    """
    if snapshot is None or not _lock.acquire(timeout=0.05):
        return None
    try:
        if len(_watchers) > 1024:
            return None
        matches = [(e, e.receipt) for e in _watchers.values()
                   if e.snapshot == snapshot and not e.revoked and isinstance(e.receipt, _Receipt)]
    finally:
        _lock.release()
    if len(matches) != 1:
        return None
    entry, receipt = matches[0]
    version = (id(entry), id(receipt), receipt.stamp)
    if expected is not None and expected != version:
        return None
    try:
        if not 0 <= time.monotonic() - receipt.stamp <= MAX_AGE or not _route_live(entry, receipt):
            return None
        # Configuration is a bounded read, not a loader that creates or repairs files.
        from tui_gateway.process_consumer_health import _notifications_enabled
        if not _notifications_enabled(snapshot.home) or not _lock.acquire(timeout=0.05):
            return None
        try:
            if (_watchers.get(entry.task) is entry and not entry.revoked and entry.receipt is receipt
                    and not entry.task.done() and not entry.task.cancelling()
                    and 0 <= time.monotonic() - receipt.stamp <= MAX_AGE):
                return version
        finally:
            _lock.release()
    except (OSError, ValueError, TypeError, AttributeError, RuntimeError):
        return None
    return None
