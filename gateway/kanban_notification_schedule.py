"""Scheduling evidence for the existing notification executors.

A delivery list is not an idle certificate. Collectors explicitly retain recovery
when enumeration fails or work remains; only a complete empty pass may sleep
until a fact wake. This object never owns a claim, cursor or delivery receipt.
"""
from dataclasses import dataclass


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
        return self.native_action.due(legacy_due=legacy_due) or now >= self.retry_at

    def due(self, now: float, *, legacy_due: bool) -> bool:
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
        return any(changed) or health_changed or now >= self.retry_at

    def settled(self, report, now: float, interval: float) -> None:
        self.retry_at = report.next_due(now, interval)
        self.retry_floor = report.pending

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

    def retry(self) -> None:
        self.pending = True

    def due_at(self, deadline: float) -> None:
        self.deadline = min(self.deadline, deadline)

    def next_due(self, now: float, interval: float) -> float:
        return min(self.deadline, now + interval if self.pending else float("inf"))
