"""Pure reductions owned by one immutable, arrival-ordered event snapshot.

No global cache: replacing a dashboard's retained history drops its reductions.
Build lazily inside each rule's exception boundary. Clock, config, replica and task
predicates are deliberately absent; callers must evaluate those on every read.
"""
from functools import cached_property
from types import MappingProxyType


class EventHistory(tuple):
    """SQLite scalar event values, plus six lazily built historical reductions."""

    def __new__(cls, rows):
        return super().__new__(cls, (MappingProxyType(dict(row)) for row in rows))

    def __setattr__(self, name, value):
        raise AttributeError("event snapshots cannot be reassigned")

    def __delattr__(self, name):
        raise AttributeError("event snapshots cannot be reassigned")

    @cached_property
    def active_cards(self):
        from hermes_cli.kanban_diagnostics import _active_hallucination_events
        return tuple(_active_hallucination_events(tuple(self), "completion_blocked_hallucination"))

    @cached_property
    def active_refs(self):
        from hermes_cli.kanban_diagnostics import _active_hallucination_events
        return tuple(_active_hallucination_events(tuple(self), "suspected_hallucinated_references"))

    @cached_property
    def ready_latest(self):
        from hermes_cli.kanban_diagnostics import _event_kind, _event_ts, _task_field
        events = [ev for ev in self if _event_kind(ev) != "dispatch_skipped"]
        return max(events, key=lambda ev: (_event_ts(ev), _task_field(ev, "id", 0) or 0)) if events else None

    @cached_property
    def quota_candidates(self):
        from hermes_cli.kanban_diagnostics import _event_kind, _event_ts, _task_field
        from hermes_cli.kanban_dispatch_skip import EVENT, _episode_kinds
        bounds = _episode_kinds() | {EVENT}
        # Sort before filtering: malformed timestamps on irrelevant kinds must
        # retain the original per-rule failure semantics. Stable ties win first.
        ordered = sorted(self, key=lambda ev: (_event_ts(ev), _task_field(ev, "id", 0) or 0), reverse=True)
        return next(((ev,) for ev in ordered if _event_kind(ev) in bounds), ())

    @cached_property
    def latest_parking(self):
        from hermes_cli.kanban_diagnostics import _latest_parking_event
        return _latest_parking_event(tuple(self))

    @cached_property
    def cycle_inputs(self):
        from hermes_cli.kanban_diagnostics import _event_kind, _event_ts
        result = []
        for ev in self:
            # Parse ALL timestamps, including irrelevant kinds, just as the old
            # rule did. A malformed heartbeat must still suppress only this rule.
            ts = _event_ts(ev)
            kind = _event_kind(ev)
            if kind in {"blocked", "unblocked"}:
                result.append((ts, kind))
        return tuple(result)
