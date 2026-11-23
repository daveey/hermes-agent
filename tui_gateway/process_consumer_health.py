"""Ephemeral proof of an admitted native process-completion delivery.

A fresh receipt is a point-in-time observation, not a future delivery guarantee.
No Kanban receipts, queue drains, adoption, or admission claims in readback.
Only the registered native poller thread may publish; shutdown removes it.
"""
from dataclasses import dataclass
from pathlib import Path
import json
import os
import threading
from tui_gateway.notification_loaded_executor import current_consumer
import time

MAX_AGE = 15.0
_lock = threading.Lock()
_consumers = {}


@dataclass
class _Consumer:
    sid: str
    session: dict
    stop: threading.Event
    thread: threading.Thread
    registry: object
    home: str
    key: str
    epoch: int = 0
    stamp: float | None = None
    lease: object = None


def register(sid, session, stop, registry):
    from tui_gateway import server
    home, key = server._session_home(session), session.get("session_key")
    if not home or not key or not hasattr(registry, "ownership_generation"):
        return
    entry = _Consumer(sid, session, stop, current_consumer(), registry,
                      str(Path(home).resolve()), key)
    with _lock:
        _consumers[entry.thread] = entry


def invalidate(session):
    """Stop/failure revokes receipts, even if a later user clears the cancel latch."""
    with _lock:
        for entry in _consumers.values():
            if entry.session is session:
                entry.stamp = None
                entry.epoch += 1


def unregister():
    with _lock:
        _consumers.pop(current_consumer(), None)


def begin_delivery(session):
    invalidate(session)
    with _lock:
        entry = _consumers.get(current_consumer())
        return (entry, entry.epoch) if entry is not None and entry.session is session else None


def delivered(sid, session, registry, events, accepted, token):
    """Native dispatch calls this only after its actual admission result."""
    with _lock:
        entry = _consumers.get(current_consumer())
        if entry is None or entry.registry is not registry or entry.session is not session or entry.sid != sid:
            return
        if token is None or token[0] is not entry or token[1] != entry.epoch:
            return
        entry.stamp = None
        if (accepted is not True or entry.stop.is_set() or not events
                or any(e.get("type", "completion") != "completion"
                       or e.get("session_key") != entry.key for e in events)):
            return
        entry.lease = session.get("active_session_lease")
        entry.stamp = time.monotonic()


def _admission_open():
    from hermes_cli.backend_retirement import retirement
    # Pure snapshot: acquire()/work()/active_count() are not guard probes.
    if not retirement._lock.acquire(timeout=0.05):
        return False
    try:
        return (not retirement._preparing and not retirement._committed
                and (retirement._token is None or retirement._now() >= retirement._expires_at))
    finally:
        retirement._lock.release()


def _lease_live(entry):
    lease = entry.lease
    if (lease is None or entry.session.get("active_session_lease") is not lease
            or not lease.enabled or lease.released or lease.session_id != entry.key
            or lease.state_path is None):
        return False
    # Native lease writers replace atomically; a missing/oversized/malformed read
    # fails closed, never prunes or repairs their registry.
    with open(lease.state_path, encoding="utf-8-sig") as stream:
        raw = stream.read(262145)
    if len(raw) > 262144:
        return False
    data = json.loads(raw)
    entries = data.get("entries") if isinstance(data, dict) else data
    if not isinstance(entries, list) or len(entries) > 1024:
        return False
    matches = [e for e in entries if isinstance(e, dict) and e.get("session_id") == entry.key]
    return (len(matches) == 1 and matches[0].get("lease_id") == lease.lease_id
            and matches[0].get("pid") == os.getpid()
            and (matches[0].get("metadata") or {}).get("live_session_id") == entry.sid)


def _notifications_enabled(home):
    # Through the config backend (check_config_yaml_readers); the parse is version-cached. [daveey]
    from hermes_cli.config_backend import read_config_doc_readonly
    try:
        cfg = read_config_doc_readonly(Path(home) / "config.yaml")
    except FileNotFoundError:
        return True
    if not isinstance(cfg, dict):
        cfg = {}
    value = (cfg.get("display") or {}).get("background_process_notifications")
    return value is not False and str(value or "").strip().lower() != "off"


def _live(entry):
    from tui_gateway import server
    session = entry.session
    if (not entry.thread.is_alive() or entry.stop.is_set()
            or any(session.get(flag) for flag in (
                "_finalized", "_closing", "_turn_cancel_requested", "_lease_taken_over"))
            or session.get("session_key") != entry.key
            or str(server._session_home(session).resolve()) != entry.home
            or getattr(session.get("agent"), "session_id", None) != entry.key):
        return False
    if not server._sessions_lock.acquire(timeout=0.05):
        return False
    try:
        if server._sessions.get(entry.sid) is not session:
            return False
    finally:
        server._sessions_lock.release()
    return _admission_open() and _lease_live(entry) and _notifications_enabled(entry.home)


def receipt_version(home, session_id, generation):
    """Capture revocation identity for a composed read; never itself proof of health."""
    home = str(Path(home).resolve())
    if not _lock.acquire(timeout=0.05):
        return None
    try:
        if len(_consumers) > 1024:
            return None
        matches = [(id(e), e.epoch, e.stamp) for e in _consumers.values()
                   if e.home == home and e.key == session_id
                   and e.registry.ownership_generation == generation and e.stamp is not None]
        return matches[0] if len(matches) == 1 else None
    finally:
        _lock.release()


def available(home, session_id, generation, *, expected=None):
    """Bounded non-mutating read of a successful native consumer receipt."""
    home = str(Path(home).resolve())
    if not _lock.acquire(timeout=0.05):
        return False
    try:
        # Never scan an unbounded registry, prune it, or refresh receipts here.
        if len(_consumers) > 1024:
            return False
        now = time.monotonic()
        entries = [(e, e.epoch, e.stamp) for e in _consumers.values() if e.home == home and e.key == session_id
                   and e.registry.ownership_generation == generation
                   and e.stamp is not None and 0 <= now - e.stamp <= MAX_AGE]
    finally:
        _lock.release()
    try:
        if len(entries) != 1:
            return False
        entry, epoch, stamp = entries[0]
        if expected is not None and (id(entry), epoch, stamp) != expected:
            return False
        if not _live(entry) or not _lock.acquire(timeout=0.05):
            return False
        try:
            return (_consumers.get(entry.thread) is entry and entry.epoch == epoch
                    and entry.stamp == stamp and not entry.stop.is_set()
                    and 0 <= time.monotonic() - stamp <= MAX_AGE)
        finally:
            _lock.release()
    except (OSError, ValueError, TypeError, AttributeError):
        return False
