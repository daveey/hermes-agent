"""Native annotation adapters on the existing ChangeFeed, not a second cache. [daveey]"""
import hashlib
from pathlib import Path
import threading
import time

from hermes_cli.fact_annotation_membership import AnnotationMembershipSource, MembershipCapture
from hermes_cli.fact_capture import CaptureBudgetExceeded
from hermes_cli.fact_content_owner import ContentOwner
from hermes_cli.fact_contract import Fact, FactKey, freeze
from hermes_cli.fact_source_watch import NativeSourceWatch
from hermes_cli.fact_wire import encode

# [daveey] Hub 2026-11-24: every annotation family watches state.db, processes, config and the kanban directory, and
# the kanban family's capture is a complete multi-board read plus links publication. Unpaced, each unrelated write
# (every turn message, every board write) ran it again on the shared ``fact-change-feed`` thread (~35% of a core;
# 309 of 321 py-spy samples). A paced source coalesces a burst of hints into one trailing pass, exactly as the
# clock-only content sources do. Other families are cheap and stay on the leading edge; review demand must be prompt.
KANBAN_CAPTURE_INTERVAL = 5.0


class NativeAnnotationOwner(ContentOwner):
    def __init__(self, engines, bindings, *, budget, operational=()):
        for source in operational:
            if isinstance(source, NativeAnnotationSource):
                source.max_keys = budget.max_keys
        super().__init__(engines, bindings, budget=budget, operational=operational, lazy_sources=True)

    def demand(self, scope, view, selector):
        from hermes_cli.fact_annotation_views import selector_for
        if scope not in self._demands:
            raise ValueError('unauthorized annotation scope')
        try:
            valid = view.startswith('annotations.') and selector == selector_for(
                scope, selector['row'], display=selector['display'])
        except (AttributeError, KeyError, TypeError):
            valid = False
        if not valid:
            raise ValueError('invalid annotation demand')
        admit = lambda: super(NativeAnnotationOwner, self).demand(scope, view, selector)
        if view == 'annotations.review':
            for source, publisher in self._by_source.items():
                if source.scope == scope and source.kind == view:
                    publisher.demand(selector, admit=admit)
                    return
        admit()


class NativeAnnotationSource(AnnotationMembershipSource):
    """Capture off-request, then publish one immutable input + its derived views.

    Review lineage is bounded demand membership, not a query on the caller. All
    deadlines are monotonic; wall-clock evidence is recorded before the read.
    """
    def __init__(self, engine, source, paths, capture):
        family = source.kind.removeprefix('annotations.')
        if source.scope != engine.scope or family not in {'kanban', 'background', 'turn', 'subagent', 'review'}:
            raise ValueError('invalid native annotation source')
        if 'AnnotationInput' in engine._specs:
            raise ValueError('annotation input cannot be a derived kind')
        self.engine, self.source, self.family = engine, source, family
        self.key = FactKey(engine.scope, 'AnnotationInput', family)
        self.paths = tuple(Path(p).absolute() for p in paths)
        self._capture = capture
        self._watch = None
        self._wake = None
        self._ids = set()
        self.max_keys = 2000
        self._lock = threading.Lock()
        self._unsubscribe = None
        self._next_wall = 0
        # Paced by the shared feed (``ChangeFeed.register`` reads it); 0 = every wake is a pass.
        self.min_interval = KANBAN_CAPTURE_INTERVAL if family == 'kanban' else 0

    def watch(self, wake):
        if self._watch is not None:
            raise RuntimeError('annotation source already watched')
        self._wake = wake
        self._watch = NativeSourceWatch(self.paths, wake)
        if self.family == 'review':
            from hermes_cli.review_session_activity import subscribe_refresh
            self._unsubscribe = subscribe_refresh(wake)
        elif self.family == 'background':
            from tools.process_registry import process_registry
            from tools.process_registry_events import subscribe
            self._unsubscribe = subscribe(process_registry, wake)
        return self

    def close(self):
        if self._watch is not None:
            self._watch.close()
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    def demand(self, selector, *, admit=lambda: None):
        if self.family != 'review':
            raise ValueError('review demand sent to another annotation source')
        row = selector['row']
        ids = {row['id'], *(row.get('_lineage_ids') or ())}
        if row.get('_lineage_root_id'):
            ids.add(row['_lineage_root_id'])
        with self._lock:
            combined = self._ids | ids
            if len(combined) > self.max_keys:
                raise CaptureBudgetExceeded('review demand inventory exceeds key budget')
            # The mailbox must accept before capture membership changes. Keep
            # bounded rejected demand from poisoning all existing review views.
            admit()
            changed = combined != self._ids
            self._ids = combined
        if changed and self._wake is not None:
            self._wake()

    def capture(self, cursor, budget):
        started, wall = time.monotonic(), time.time()
        if self._watch is not None:
            self._watch.check()
        with self._lock:
            ids = frozenset(self._ids)
        value, due = self._capture(budget, wall, ids)
        value = freeze(value)
        raw = encode(value)
        if len(raw) > budget.max_bytes or time.monotonic() - started >= budget.max_seconds:
            raise CaptureBudgetExceeded('native annotation capture exceeds time/byte budget')
        token = hashlib.sha256(raw).hexdigest()
        if self._watch is not None:
            self._watch.check()
        if self._next_wall > wall:
            due = min(due, self._next_wall)
        return MembershipCapture(value, token, wall, started + min(30, max(0.001, due - wall)))

    def commit(self, capture):
        if capture.observed_at >= self._next_wall:
            self.engine.publish([Fact(self.key, capture.value,
                source_vector=((self.source, capture.after),), observed_at=capture.observed_at,
                valid_until=capture.observed_at + 90, health='fresh')], now=capture.observed_at)
            self._next_wall = capture.observed_at + max(0.001, capture.next_deadline - time.monotonic())
        else:
            super().commit(capture)
            self._next_wall = min(self._next_wall, capture.observed_at + max(
                0.001, capture.next_deadline - time.monotonic()))

