"""Pure ordered pickup gate facts; bounded, opt-in partial observations. [daveey]

A complete decision covers _skip_reason ONLY, not quota, pacing, import or
claim. Missing records/facts are unknown. This module never certifies a trace.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import json
from typing import Mapping


STAGES = ('already_local', 'default_placement', 'review_disabled', 'placement_refusal',
          'missing_profile', 'capability_refusal', 'source_refusal')
Fact = bool | str | None
Reason = tuple[str, str | None]


@dataclass(frozen=True)
class Decision:
    complete: bool
    next_fact: str | None = None
    reason: Reason | None = None


def evaluate(facts: Mapping[str, Fact]) -> Decision:
    """Pure, short-circuit evaluator. Absent is never equivalent to null."""
    for index, name in enumerate(STAGES):
        if name not in facts:
            return Decision(False, next_fact=name)
        value = facts[name]
        if index < 3:
            if type(value) is not bool:
                raise ValueError(f'{name} must be boolean')
            if value:
                reason = ('other', 'review dispatch disabled') if index == 2 else ('', None)
                return Decision(True, reason=reason)
        else:
            if value is not None and type(value) is not str:
                raise ValueError(f'{name} must be string or null')
            if value is not None:
                category = 'profile_ineligible' if name == 'missing_profile' else 'capability_missing'
                if name == 'placement_refusal' and value == 'held':
                    category = 'hold'
                return Decision(True, reason=(category, value))
    return Decision(True)


@dataclass(frozen=True)
class Snapshot:
    task_id: str
    facts: tuple[tuple[str, Fact], ...]
    complete: bool
    next_fact: str | None
    reason: Reason | None


@dataclass
class Collector:
    max_records: int = 1000
    max_bytes: int = 1_000_000
    records: list[Snapshot] = field(default_factory=list)
    bytes_used: int = 0
    gap: str | None = None

    def add(self, task_id: str, facts: Mapping[str, Fact]) -> None:
        if self.gap is not None:
            return
        if len(self.records) >= self.max_records:
            self.gap = 'record_limit'
            return
        try:
            if type(task_id) is not str or not task_id:
                raise ValueError('invalid task id')
            if any(name not in STAGES for name in facts):
                raise ValueError('unknown fact')
            # Check even facts beyond a short-circuit: snapshots retain primitives only.
            for name, value in facts.items():
                if (name in STAGES[:3] and type(value) is not bool) or (
                        name in STAGES[3:] and value is not None and type(value) is not str):
                    raise ValueError('invalid fact')
            decision = evaluate(facts)
            pairs = tuple((name, facts[name]) for name in STAGES if name in facts)
            size = len(json.dumps([task_id, pairs, decision.complete, decision.next_fact,
                                   decision.reason], ensure_ascii=False).encode('utf-8'))
            if self.bytes_used + size > self.max_bytes:
                self.gap = 'byte_limit'
                return
            self.records.append(Snapshot(task_id, pairs, decision.complete, decision.next_fact, decision.reason))
            self.bytes_used += size
        except (ValueError, TypeError, UnicodeError):
            self.gap = 'invalid_facts'


_current: ContextVar[Collector | None] = ContextVar('pickup_gate_observer', default=None)


@contextmanager
def collect(*, max_records: int = 1000, max_bytes: int = 1_000_000):
    """Caller-owned in-memory scope. No file IO, activation or coverage claim."""
    if type(max_records) is not int or type(max_bytes) is not int or min(max_records, max_bytes) < 0:
        raise ValueError('collector limits must be nonnegative integers')
    collector = Collector(max_records, max_bytes)
    token = _current.set(collector)
    try:
        yield collector
    finally:
        _current.reset(token)


def record(task_id: str, facts: Mapping[str, Fact]) -> None:
    collector = _current.get()
    if collector is not None:
        collector.add(task_id, facts)


def decide(card, conn, own, machines, *, mode, review_enabled, profile_exists, placement):
    """Acquire precisely the facts the pure evaluator requests, once each."""
    facts: dict[str, Fact] = {}
    try:
        while True:
            decision = evaluate(facts)
            if decision.complete:
                return decision.reason
            name = decision.next_fact
            facts[name] = _read_fact(name, card, conn, own, machines, mode=mode,
                                     review_enabled=review_enabled, profile_exists=profile_exists,
                                     placement=placement)
    finally:
        record(card['id'], facts)


def _read_fact(name, card, conn, own, machines, *, mode, review_enabled, profile_exists, placement):
    if name == 'already_local':
        return bool(conn.execute('SELECT 1 FROM tasks WHERE id = ?', (card['id'],)).fetchone())
    if name == 'default_placement':
        return (mode == 'explicit' or card['status'] == 'review') and card['runs_on'] is None
    if name == 'review_disabled':
        return card['status'] == 'review' and not review_enabled
    if name == 'placement_refusal':
        return placement(dict(card), own, machines)
    if name == 'missing_profile':
        if profile_exists is not None and card['assignee'] and not profile_exists(card['assignee']):
            return f"no profile {card['assignee']!r} here"
        return None
    if name == 'capability_refusal':
        # A profile must satisfy the declared capabilities before pickup, not fail its handoff later.
        from hermes_cli.kanban_runs_on_capabilities import claim_refusal
        refusal = claim_refusal(dict(card), me=own)
        return None if refusal is None else str(refusal)
    if name == 'source_refusal':
        from hermes_cli.kanban_workspace_source import pickup_skip
        return pickup_skip(conn, card, own)
    raise ValueError(f'unknown fact {name}')
