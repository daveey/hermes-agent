"""Fact wake gates for existing notification executors. [daveey]

No action runs on the feed callback. The sole existing executor consumes a wake
and drains its authoritative stream, preserving its own cursor, lease and ACKs.
A reset means catch up, never seed to the fact revision. Bind once off the request
path using an authorized scope and an already canonical profile-home string.
"""
from __future__ import annotations

import threading
from collections.abc import Callable

KINDS = ("SourceState", "TaskFact", "SessionFact", "ChatWaitingFact")
_bindings = {}
_bindings_lock = threading.Lock()
_binding_watches = set()


class BindingWatch:
    """Install-wide lifecycle hint; read a fresh memory snapshot after every wake."""

    def __init__(self, wake):
        self.wake = wake
        self.lock = threading.RLock()
        self.closed = False

    def snapshot(self):
        with _bindings_lock:
            return tuple(_bindings.items())

    def signal(self):
        with self.lock:
            if not self.closed:
                self.wake()

    def close(self):
        with _bindings_lock:
            _binding_watches.discard(self)
        with self.lock:
            self.closed = True


def observe_bindings(wake):
    """Register before catch-up; callbacks carry no possibly stale snapshot."""
    watch = BindingWatch(wake)
    with _bindings_lock:
        _binding_watches.add(watch)
    watch.signal()
    return watch



def bind(v1, scope, profile_home, *, lifecycle=None):
    """Bind scoped reads and an explicit release authority at startup.

    Real replicas deliberately have no activation authority. The coordinator must
    supply its lifecycle; the combined fake facade remains supported in tests.
    This consumer never constructs a latch or reports itself ready on startup.
    """
    lifecycle = v1 if lifecycle is None else lifecycle
    if not callable(getattr(lifecycle, "bind_consumer", None)) or not callable(getattr(lifecycle, "report_ready", None)):
        raise ValueError("notification lifecycle authority required")
    with _bindings_lock:
        if profile_home in _bindings:
            raise ValueError("notification profile already bound")
        owner = NotificationFacts(v1, scope, lifecycle=lifecycle)
        owner.profile_home = profile_home
        _bindings[profile_home] = owner
        watches = tuple(_binding_watches)
    for watch in watches:
        watch.signal()
    return owner


def for_home(profile_home):
    """Memory-only lookup. Never resolve a path or infer a launch-profile fallback."""
    with _bindings_lock:
        owner = _bindings.get(profile_home)
        if owner is None and any(binding.mode == "facts" for binding in _bindings.values()):
            raise RuntimeError("notification facts binding unavailable for profile")
        return owner



class ActionWake:
    """An in-memory scheduling gate, NOT a delivery cursor or executor."""

    def __init__(self, owner, wake: Callable[[], None]):
        self.owner = owner
        self.wake = wake
        self.pending = True
        self.closed = False
        self.pending_sources = {'unknown'}
        self.diagnostic_sources = ()

    def due(self, *, legacy_due: bool) -> bool:
        with self.owner.lock:
            self.diagnostic_sources = ()
            if self.closed:
                return False
            if self.owner.mode == "legacy":
                return legacy_due
            pending, self.pending = self.pending, False
            if pending:
                self.diagnostic_sources = tuple(sorted(self.pending_sources))
                self.pending_sources.clear()
            return pending

    @property
    def facts(self) -> bool:
        with self.owner.lock:
            return self.owner.mode == "facts"

    def close(self) -> None:
        with self.owner.lock:
            self.closed = True
            self.owner.actions.discard(self)


class NotificationFacts:
    """One release-latch binding per profile; actions keep their original loops."""

    def __init__(self, v1, scope, *, lifecycle=None):
        self.v1 = v1
        self.lifecycle = v1 if lifecycle is None else lifecycle
        self.scope = scope
        self.profile_home = None
        self.lock = threading.Lock()
        self.transition = threading.Lock()
        self.mode = "legacy"
        self.generation = -1
        self.token = 0
        self.closed = False
        self.subscription = None
        self.actions: set[ActionWake] = set()
        self.registration = self.lifecycle.bind_consumer("notifications", scope, self._mode)

    def report_ready(self, *, ready=True) -> None:
        self.lifecycle.report_ready("notifications", self.scope, version=1, ready=ready)

    def add_action(self, wake: Callable[[], None]) -> ActionWake:
        action = ActionWake(self, wake)
        with self.lock:
            if self.closed:
                raise RuntimeError("notification binding closed")
            self.actions.add(action)
        return action

    def _signal(self, token, delta) -> None:
        if not (delta.reset or delta.upserts or delta.deletes or delta.health):
            return
        with self.lock:
            if self.closed or self.mode != "facts" or token != self.token:
                return
            actions = tuple(self.actions)
            for action in actions:
                action.pending = True
                kinds = {fact.key.kind for fact in delta.upserts} | {key.kind for key in delta.deletes}
                action.pending_sources.update(kind if kind in KINDS else 'unknown' for kind in kinds)
                if delta.reset:
                    action.pending_sources.add('reset')
                if delta.health:
                    action.pending_sources.add('health')
        from tui_gateway.notification_loaded_f import signal
        signal(self.profile_home, delta)
        for action in actions:
            action.wake()

    def _mode(self, mode, generation) -> None:
        if mode not in ("legacy", "facts"):
            raise ValueError("unknown notification mode")
        with self.transition:
            with self.lock:
                if self.closed or generation < self.generation:
                    return
                if mode == self.mode and generation == self.generation:
                    return
                self.token += 1
                token = self.token
                self.generation = generation
            # Close first: rollback cannot resume a poll while still subscribed.
            if self.subscription is not None:
                self.subscription.close()
                self.subscription = None
            with self.lock:
                self.mode = mode
                actions = tuple(self.actions)
                for action in actions:
                    action.pending = True
                    action.pending_sources.add('unknown')
            if mode == "facts":
                self.subscription = self.v1.subscribe(
                    self.scope, KINDS, None, None, lambda delta: self._signal(token, delta))
            for action in actions:
                action.wake()

    def close(self) -> None:
        with self.transition:
            with self.lock:
                self.closed = True
                self.token += 1
                actions = tuple(self.actions)
            if self.subscription is not None:
                self.subscription.close()
                self.subscription = None
            self.registration.close()
            for action in actions:
                action.close()
                action.wake()
        with _bindings_lock:
            watches = ()
            if _bindings.get(self.profile_home) is self:
                _bindings.pop(self.profile_home)
                watches = tuple(_binding_watches)
        for watch in watches:
            watch.signal()
