"""Scheduling evidence for the existing notification executors.

A delivery list is not an idle certificate. Collectors explicitly retain recovery
when enumeration fails or work remains; only a complete empty pass may sleep
until a fact wake. This object never owns a claim, cursor or delivery receipt.
"""
from dataclasses import dataclass
from gateway.notification_retry_provenance import earliest, origin


class BoundActions:
    """Memory-only membership for a host executor; callbacks only wake it.

    The executor remains single-threaded and owns this object. A binding removed
    after facts activation is not permission to silently resume source polling.
    """

    def __init__(self, wake, *, native=False):
        from tui_gateway.notification_facts import observe_bindings
        self.wake = wake
        self.actions = {}
        self.was_bound = False
        self.facts_bound = False
        self.retry_at = float("inf")
        self.retry_floor = False
        self.watch = observe_bindings(wake)
        from hermes_cli.fact_links_health import HealthWatch
        self.links_health = HealthWatch(wake)
        self.native = native
        self.native_action = None
        self.admission_sources = ()
        self.deadline_sources = ()

    @property
    def legacy(self):
        return not self.was_bound or (self.native and not self.facts_bound and
                                      (self.native_action is None or not self.native_action.owner.policy.enabled))

    def _native_due(self, now, legacy_due):
        from tui_gateway.notification_gateway_sources import enabled
        if not self.native or not enabled():
            if self.native_action is not None:
                self.native_action.close()
                self.native_action = None
            if self.retry_floor and now < self.retry_at:
                return False
            return legacy_due or now >= self.retry_at
        if self.native_action is None:
            from tui_gateway.notification_loaded_f import register_gateway
            self.native_action = register_gateway(self.wake)
        self.was_bound = True
        if self.retry_floor and now < self.retry_at:
            return False
        if not self.native_action.owner.policy.enabled:
            return legacy_due or now >= self.retry_at
        return self.native_action.due(legacy_due=legacy_due) or now >= self.retry_at

    def due(self, now: float, *, legacy_due: bool) -> bool:
        self.admission_sources = ()
        bindings = self.watch.snapshot()
        owners = {owner for _home, owner in bindings}
        self.was_bound = self.was_bound or bool(owners)
        self.facts_bound = self.facts_bound or bool(owners)
        for owner in self.actions.keys() - owners:
            self.actions.pop(owner).close()
        for owner in owners - self.actions.keys():
            try:
                self.actions[owner] = owner.add_action(self.wake)
            except RuntimeError:
                # Close raced the snapshot. Do not execute with partial scope;
                # the close notification wakes a fresh membership reconciliation.
                return False
        if not self.actions:
            if self.native and not self.facts_bound:
                return self._native_due(now, legacy_due)
            # No executor can service this deadline until a binding returns.
            # Rebinding starts dirty and catches up from durable action state.
            self.retry_at = float("inf")
            self.retry_floor = False
            return legacy_due and not self.was_bound
        if self.native_action is not None:
            self.native_action.close()
            self.native_action = None
        if self.retry_floor and now < self.retry_at:
            return False  # bursts must not spend refusal attempts faster than legacy
        # Consume every pending bit, not just the first true action.
        changed = [action.due(legacy_due=legacy_due) for action in self.actions.values()]
        health_changed = self.links_health.due({str(home) for home, _owner in bindings})
        self.admission_sources = tuple(source for action, due in zip(self.actions.values(), changed) if due
                                       for source in getattr(action, 'diagnostic_sources', ('unknown',)))
        if health_changed:
            self.admission_sources += ('health',)
        return any(changed) or health_changed or now >= self.retry_at

    def settled(self, report, now: float, interval: float) -> None:
        self.retry_at = report.next_due(now, interval)
        self.retry_floor = report.pending
        sources = []
        if report.pending:
            sources.append(('retry', now + interval, *(getattr(report, 'retry_origin', None) or ('unknown', 'unknown'))))
        for category, entry in (('retry', getattr(report, 'retry_deadline', None)),
                                ('due', getattr(report, 'due_deadline', None))):
            if entry is not None:
                sources.append((category, *entry))
        self.deadline_sources = tuple(sources)

    def close(self) -> None:
        if self.native_action is not None:
            self.native_action.close()
        self.watch.close()
        self.links_health.close()
        for action in self.actions.values():
            action.close()
        self.actions.clear()


@dataclass
class PassReport:
    """Conservative, pass-local evidence; deadlines use monotonic time."""

    pending: bool = False
    deadline: float = float("inf")
    retry_origin: tuple | None = None
    retry_deadline: tuple | None = None
    due_deadline: tuple | None = None

    def retry(self, subpass='unknown', reason='unknown') -> None:
        self.pending = True
        source = origin(subpass, reason)
        self.retry_origin = source if self.retry_origin is None else min(self.retry_origin, source)

    def due_at(self, deadline: float, subpass='unknown', reason='unknown', *, cause='due') -> None:
        self.deadline = min(self.deadline, deadline)
        if cause == 'retry':
            self.retry_deadline = earliest(self.retry_deadline, deadline, subpass, reason)
        else:
            self.due_deadline = earliest(self.due_deadline, deadline, subpass, reason)

    def next_due(self, now: float, interval: float) -> float:
        return min(self.deadline, now + interval if self.pending else float("inf"))
