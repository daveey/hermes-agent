"""Publication-local ownership of supersedable annotation clocks. [daveey]

Legacy clocks and settle confirmations remain conservative. Only a successful
source projection may replace all boundaries of its own scope-local owner.
"""
from __future__ import annotations


class Schedule(list):
    """List-compatible next wakes, retaining distinct clock provenance."""

    def __init__(self, plain, owners, confirmations=()):
        self.plain = tuple(sorted(set(plain)))
        self.owners = {owner: tuple(values) for owner, values in owners.items() if values}
        self.confirmations = tuple(confirmations)
        super().__init__(sorted({*self.plain, *self.confirmations,
                                 *(due for values in self.owners.values() for due in values)}))


class Plan(list):
    """Stage clock replacement; discard this object when projection fails."""

    def __init__(self, previous, confirmations, now):
        plain = (previous.plain if isinstance(previous, Schedule)
                 else [due for due in previous if due not in confirmations])
        owners = previous.owners if isinstance(previous, Schedule) else {}
        self.expired = any(due <= now for due in plain) or any(
            due <= now for values in owners.values() for due in values)
        super().__init__(due for due in plain if due > now)
        self.owners = {owner: tuple(due for due in values if due > now) for owner, values in owners.items()}

    def replace(self, owner, values, now):
        self.owners[owner] = tuple(sorted({due for due in values if due is not None and due > now}))

    def finish(self, confirmations=()):
        return Schedule(self, self.owners, confirmations)


def retry(previous, due, now):
    """A failed/busy scope keeps every unconsumed source clock and its identity."""
    plan = Plan(previous, (), now)
    plan.append(due)
    confirmations = getattr(previous, 'confirmations', ())
    return plan.finish(t for t in confirmations if t > now)
