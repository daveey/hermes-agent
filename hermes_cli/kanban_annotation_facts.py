"""Scoped annotation reads from the shared owner, never a local producer. [daveey]

Authorized startup owns handles and installs ``use`` at the request boundary.
The release coordinator alone calls the mode callback. No request loads config,
acquires IPC, reads a legacy source, or certifies an expired projection.
"""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from functools import wraps
import threading
import time

FIELDS = {
    'kanban': frozenset(('kanban_active', 'kanban_blocked', 'kanban_unseen',
        'kanban_attention', 'kanban_remote_stale', 'kanban_parked', 'kanban_held',
        'kanban_stalled', 'kanban_observed_at', 'kanban_unknown', 'kanban_waiting_on',
        'kanban_blocked_on', 'needs_you', 'kanban_external', 'kanban_external_unknown',
        'kanban_external_down', 'kanban_external_cards')),
    'goal': frozenset(('goal_active',)),
    'cron': frozenset(('cron_active',)),
    'background': frozenset(('background_active', 'background_processes')),
    'turn': frozenset(('turn_live',)),
    'subagent': frozenset(),
    'review': frozenset(('review_wait', 'review_wait_pr')),
}
SOURCES = {
    'kanban': frozenset(('kanban.card', 'kanban.counts')),
    'background': frozenset(('process.registry', 'process.checkpoint')),
    'turn': frozenset(('turn.lease',)),
    'subagent': frozenset(('subagent.activity', 'subagent.registry')),
}
_current = ContextVar('annotation_facts', default=None)
_active_lock = threading.Condition()
_active_bindings = set()
_legacy_inflight = 0


def _claim_facts(binding, *, timeout=5.0):
    """Fence unbound legacy requests before draining already-admitted scans."""
    with _active_lock:
        _active_bindings.add(binding)
        if not _active_lock.wait_for(lambda: _legacy_inflight == 0, timeout):
            _active_bindings.discard(binding)
            raise TimeoutError('legacy annotation requests did not drain')


class AnnotationUnavailable(RuntimeError):
    """Retryable missing/expired projection; never permission to run legacy I/O."""


def _thaw(value):
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


@contextmanager
def use(binding):
    """Bind an already-authorized profile replica, not a caller-supplied path."""
    token = _current.set(binding)
    try:
        yield
    finally:
        _current.reset(token)


class AnnotationFacts:
    def __init__(self, handle, scope, *, stop_legacy, start_legacy, view_facts=False):
        self.handle, self.scope = handle, scope
        self._view_facts = view_facts
        self.v1 = handle.v1
        self._stop, self._start = stop_legacy, start_legacy
        self._lock = threading.RLock()
        self._mode = 'legacy'
        self._generation = -1
        self._rollback_generation = -1
        self._subscription = None
        self._needs_restart = False
        self._closed = False
        self.registration = self.v1.bind_consumer('annotations', scope, self.on_mode)

    def on_mode(self, mode, generation):
        with self._lock:
            if self._closed or generation < self._generation:
                return
            if mode not in ('legacy', 'facts'):
                raise ValueError('unknown annotation mode')
            if mode == 'facts' and generation <= self._rollback_generation:
                return
            # Receipt fences older callbacks even when drain/restart fails.
            self._generation = generation
            if mode == 'legacy':
                self._rollback_generation = generation
            if mode == self._mode:
                if mode == 'legacy' and self._needs_restart:
                    self._restore_legacy()
                self._generation = generation
                return
            if mode == 'facts':
                _claim_facts(self)
                self._needs_restart = True
                try:
                    self._stop()
                    self._subscription = self.v1.subscribe(
                        self.scope, ('ViewFact',) if self._view_facts else ('SessionFact', 'ChatWaitingFact'),
                        None, None, self._changed)
                except BaseException:
                    self._rollback_generation = generation
                    self._restore_legacy()
                    raise
            else:
                if self._subscription is not None:
                    self._subscription.close()
                    self._subscription = None
                self._mode = 'legacy'
                self._restore_legacy()
            self._mode, self._generation = mode, generation
            with _active_lock:
                if mode == 'facts':
                    _active_bindings.add(self)
                else:
                    _active_bindings.discard(self)

    def _restore_legacy(self):
        # A failed stop can already have fenced prewarm/holder admission. Keep
        # request custody until rollback succeeds; same-mode rollback retries it.
        self._start()
        self._needs_restart = False
        with _active_lock:
            _active_bindings.discard(self)

    def _changed(self, delta):
        # The shared replica already publishes atomic snapshots. Never maintain
        # a competing per-consumer cache or re-run providers from this callback.
        return None

    def annotate(self, family, rows, legacy, **kwargs):
        with self._lock:
            if self._closed:
                raise AnnotationUnavailable('annotation consumer closed')
            if self._mode == 'legacy':
                if self._needs_restart:
                    raise AnnotationUnavailable('legacy annotation rollback incomplete')
                return _legacy_call(rows, legacy, **kwargs)
            return self._apply(family, rows, **kwargs)

    def _apply(self, family, rows, **kwargs):
        from hermes_cli.fact_annotation_views import selector_for
        from hermes_cli.fact_store import view_key

        keys = []
        selectors = []
        for row in rows:
            if row.get('profile', self.scope.profile_id) != self.scope.profile_id:
                raise AnnotationUnavailable('annotation profile mismatch')
            if self._view_facts:
                try:
                    selector = selector_for(self.scope, row, **kwargs)
                except (ValueError, TypeError, KeyError):
                    raise AnnotationUnavailable('invalid annotation selector') from None
                selectors.append(selector)
                keys.append(view_key(self.scope, 'annotations.' + family, selector))
            else:
                selectors.append({'session_id': row['id']})
                keys.append(self.v1.key(self.scope, 'SessionFact', row['id']))
        snapshot = self.v1.snapshot(self.scope)
        facts = snapshot.get_many(keys)
        projected = []
        now = time.time()
        for selector, key, fact in zip(selectors, keys, facts):
            if (fact is None or fact.key != key or fact.health != 'fresh'
                    or (fact.valid_until is not None and fact.valid_until <= now)):
                try:
                    self.handle.demand('annotations.' + family if self._view_facts else 'annotations', selector)
                except (ValueError, RuntimeError):
                    raise AnnotationUnavailable('annotation demand unavailable') from None
                raise AnnotationUnavailable('annotation projection unavailable')
            if self._view_facts:
                patch = fact.value.get('payload')
            else:
                annotations = fact.value.get('annotations', {})
                if family not in annotations:
                    raise AnnotationUnavailable('annotation family unavailable')
                patch = annotations[family]
            if not isinstance(patch, Mapping) or set(patch) - FIELDS[family] - {'execution_observations'}:
                raise AnnotationUnavailable('annotation projection contract mismatch')
            if 'execution_observations' in patch:
                allowed = SOURCES.get(family, frozenset())
                if family == 'kanban':
                    allowed = allowed | {'kanban.replica_run'}
                observations = patch['execution_observations']
                if (not allowed or not isinstance(observations, (tuple, list))
                        or any(not isinstance(item, Mapping) or not isinstance(item.get('source'), str)
                               or item['source'] not in allowed for item in observations)):
                    raise AnnotationUnavailable('annotation observation contribution mismatch')
            projected.append(_thaw(patch))
        # Validate the entire batch first: an absent later row must not leave a
        # half-mutated response. Each caller owns its decoded mutable output.
        for row, patch in zip(rows, projected):
            for field in FIELDS[family]:
                row.pop(field, None)
            if family in SOURCES:
                kept = [deepcopy(item) for item in row.get('execution_observations', ())
                        if not isinstance(item, dict) or item.get('source') not in SOURCES[family]]
                had_observations = 'execution_observations' in patch
                added = patch.pop('execution_observations', [])
                if kept or added or had_observations:
                    row['execution_observations'] = kept + added
                else:
                    row.pop('execution_observations', None)
            row.update(patch)
        return rows

    def close(self):
        with self._lock:
            if self._closed:
                return
            self.on_mode('legacy', self._generation + 1)
            self.registration.close()
            self._closed = True


def _legacy_call(rows, legacy, **kwargs):
    """One process-wide admission for bound and unbound legacy requests."""
    global _legacy_inflight
    with _active_lock:
        if _active_bindings:
            raise AnnotationUnavailable('annotation owner transfer active or binding missing')
        _legacy_inflight += 1
    try:
        return legacy(rows, **kwargs)
    finally:
        with _active_lock:
            _legacy_inflight -= 1
            _active_lock.notify_all()


def annotate(family, rows, legacy, **kwargs):
    """Request seam; binding is installed by the authorized host lifecycle."""
    binding = _current.get()
    if binding is None:
        return _legacy_call(rows, legacy, **kwargs)
    return binding.annotate(family, rows, legacy, **kwargs)


def consumer(family):
    """Keep canonical legacy serializers as the rollback implementation."""
    def decorate(legacy):
        @wraps(legacy)
        def wrapped(rows, **kwargs):
            return annotate(family, rows, legacy, **kwargs)
        return wrapped
    return decorate
