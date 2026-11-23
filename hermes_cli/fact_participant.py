"""Process-local participant control, not a distributed release latch. [daveey]

Startup supplies the complete authorized roster. An external release owner must
fence requests across all processes BEFORE calling transition. Readiness never
auto-activates; failed drains retain callback custody for explicit rollback.
"""
from collections.abc import Callable
from contextvars import copy_context
from dataclasses import dataclass
from functools import partial
import threading


@dataclass
class Registration:
    remove: Callable[[], None]
    closed: bool = False

    def close(self):
        if not self.closed:
            self.remove()
            self.closed = True


class ProcessLifecycle:
    def __init__(self, expected):
        self.expected = frozenset(expected)
        if not self.expected:
            raise ValueError('empty participant roster')
        self._lock = threading.RLock()
        self._operation = threading.RLock()
        self._callbacks = {}
        self._ready = {}
        self._retired = set()
        self.mode = 'legacy'
        self.generation = 0

    def bind_consumer(self, name, scope, callback):
        key = (scope, name)
        callback = partial(copy_context().run, callback)
        with self._operation:
            with self._lock:
                if key not in self.expected:
                    raise ValueError('unexpected consumer')
                if key in self._retired:
                    raise ValueError('retired consumer requires a new participant capability')
                if key in self._callbacks or self.mode != 'legacy':
                    raise RuntimeError('consumer already owned or participant not legacy')
                self._callbacks[key] = callback
                self.mode = 'transition'
            try:
                callback('legacy', self.generation)
            except BaseException:
                with self._lock:
                    self.mode = 'failed'
                raise
            with self._lock:
                self.mode = 'legacy'
        return Registration(lambda: self.unbind_consumer(name, scope))

    def unbind_consumer(self, name, scope):
        """Also recovers retained slots whose initial bind callback failed."""
        with self._operation, self._lock:
            if self.mode != 'legacy':
                raise RuntimeError('rollback required before unregister')
            self._callbacks.pop((scope, name), None)
            self._ready.pop((scope, name), None)
            self._retired.add((scope, name))

    def has_bindings(self, scope):
        with self._lock:
            return any(key[0] == scope for key in self._callbacks)

    def report_ready(self, name, scope, *, version=1, ready=True):
        with self._lock:
            key = (scope, name)
            if key not in self._callbacks:
                raise ValueError('consumer not bound')
            self._ready[key] = type(version) is int and version == 1 and ready is True

    def _is_ready(self):
        return all(k in self._callbacks and self._ready.get(k) for k in self.expected)

    def _call_all(self, callbacks, mode, generation):
        errors = []
        for callback in callbacks:
            try:
                callback(mode, generation)
            except Exception as exc:  # health: allow BLE001 -- aggregate failures and retain every participant for rollback
                errors.append(exc)
                if mode == 'facts':
                    break
            except BaseException:
                with self._lock:
                    self.mode = 'failed'
                    self._ready.clear()
                raise
        return errors

    def transition(self, mode, generation):
        """Synchronous control-plane call; callers keep it off request threads."""
        if mode not in ('legacy', 'facts'):
            raise ValueError('invalid participant mode')
        with self._operation:
            with self._lock:
                if self.mode == 'transition':
                    raise RuntimeError('reentrant participant transition')
                if type(generation) is not int or generation <= self.generation:
                    raise ValueError('non-increasing release generation')
                if mode == 'facts' and self.mode != 'legacy':
                    raise RuntimeError('rollback required before activation')
                if mode == 'facts' and not self._is_ready():
                    raise RuntimeError('participant not ready')
                self.generation = generation
                self.mode = 'transition'
                callbacks = tuple(self._callbacks.values())
            errors = self._call_all(callbacks, mode, generation)
            with self._lock:
                if mode == 'facts' and not self._is_ready():
                    errors.append(RuntimeError('readiness withdrawn during transition'))
            if errors and mode == 'facts':
                # Rollback gets a NEW generation. A consumer may reject replay of
                # facts at the generation it already rolled back.
                generation += 1
                rollback_errors = self._call_all(reversed(callbacks), 'legacy', generation)
                errors.extend(rollback_errors)
                resulting_mode = 'failed' if rollback_errors else 'legacy'
            else:
                resulting_mode = 'failed' if errors else mode
            with self._lock:
                self.generation = generation
                self.mode = resulting_mode
                if errors or mode == 'legacy':
                    self._ready.clear()
            if errors:
                raise ExceptionGroup('participant transition incomplete', errors)
