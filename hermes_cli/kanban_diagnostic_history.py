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

    def latest_ts(self, kinds):
        from hermes_cli.kanban_diagnostics import _latest_event_ts
        key = frozenset(kinds)
        cache = self.__dict__.setdefault("_latest_ts", {})
        if key not in cache:
            cache[key] = _latest_event_ts(tuple(self), kinds)
        return cache[key]

    def ready_floor(self, runs):
        from collections.abc import Mapping
        from hermes_cli.kanban_diagnostics import _last_ready_entry_ts
        runs = tuple(runs)
        if not all(isinstance(run, Mapping) for run in runs):
            return _last_ready_entry_ts(tuple(self), runs)
        # Keep only the latest run snapshot: bookkeeping changes (including
        # NULL -> 0) affect unused-grant classification without moving P2.
        # Types are part of the key: 100 == 100.0 but the floor would differ in type.
        key = tuple(tuple((name, type(value), value) for name, value in run.items()) for run in runs)
        if any(type(value) not in (str, int, float, bytes, type(None))
               for run in key for _name, _type, value in run):
            return _last_ready_entry_ts(tuple(self), runs)
        previous = self.__dict__.get("_ready_floor")
        if previous is None or previous[0] != key:
            value = _last_ready_entry_ts(tuple(self), runs)
            self.__dict__["_ready_floor"] = (key, value)
        return self.__dict__["_ready_floor"][1]

    @cached_property
    def terminal_provider(self):
        from hermes_cli.kanban_diagnostics import _latest_gave_up_is_terminal_provider
        return _latest_gave_up_is_terminal_provider(tuple(self))

    @cached_property
    def last_block(self):
        from hermes_cli.kanban_diagnostics import _event_kind
        return next((ev for ev in reversed(self) if _event_kind(ev) == "blocked"), None)

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
    def episode_skip(self):
        """``_episode_skip`` of this snapshot, read-only: its JSON payload is parsed once, not per rule."""
        from hermes_cli.kanban_diagnostics import _event_kind, _parse_payload
        from hermes_cli.kanban_dispatch_skip import EVENT
        if not self.quota_candidates or _event_kind(self.quota_candidates[0]) != EVENT:
            return MappingProxyType({})
        payload = _parse_payload(self.quota_candidates[0])
        return MappingProxyType(payload) if isinstance(payload, dict) else payload

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
