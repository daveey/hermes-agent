"""Atomic immutable generations for projection readers. [daveey]"""

from __future__ import annotations

import json
import math
import threading
import uuid
from collections import deque
from dataclasses import dataclass, replace
import logging
from collections.abc import Iterable, Mapping

from hermes_cli.fact_contract import Delta, Fact, FactKey, Pending, Position, Ready, Scope, Unavailable
from hermes_cli.fact_map import FactMap
from hermes_cli.fact_expiry import ExpiryIndex
from hermes_cli.fact_wire import encode, encoded_size


def view_key(scope: Scope, view: str, selector: Mapping) -> FactKey:
    token = json.dumps([view, dict(selector)], sort_keys=True, separators=(",", ":"), allow_nan=False)
    return FactKey(scope, "ViewFact", token)


@dataclass(frozen=True)
class Snapshot:
    scope: Scope
    epoch: str
    rev: int
    _facts: Mapping[FactKey, Fact]

    def get(self, key: FactKey) -> Fact | None:
        return self._facts.get(key) if key.scope == self.scope else None

    def get_many(self, keys: Iterable[FactKey]) -> tuple[Fact | None, ...]:
        return tuple(self.get(key) for key in keys)


    def view(self, view: str, selector: Mapping) -> Ready | Pending | Unavailable:
        fact = self.get(view_key(self.scope, view, selector))
        if fact is None:
            return Pending()
        if fact.health != "fresh":
            return Unavailable(fact.error_code or fact.health)
        return Ready(fact.value["encoded"], self.epoch, self.rev)


class FactStore:
    """Owner publishes entire generations; request readers retain immutable snapshots."""

    key = staticmethod(FactKey)
    position = staticmethod(Position)
    view_key = staticmethod(view_key)

    Ready = Ready
    Pending = Pending
    Unavailable = Unavailable

    def __init__(self, *, ring_revisions: int = 4096, ring_bytes: int = 16 * 1024 * 1024,
                 subscriber_envelopes: int = 64, subscriber_bytes: int = 4 * 1024 * 1024,
                 fact_bytes: int = 48 * 1024 * 1024) -> None:
        if min(ring_revisions, ring_bytes, subscriber_envelopes, subscriber_bytes) < 1:
            raise ValueError("ring limits must be positive")
        self._condition = threading.Condition()
        if not 1024 < fact_bytes <= 48 * 1024 * 1024:
            raise ValueError("fact byte budget must fit reset transport")
        self._fact_limit = fact_bytes
        self._fact_bytes = 1024
        self._epoch = uuid.uuid4().hex
        self._rev = 0
        self._facts = FactMap()
        self._sizes = FactMap()
        self._expiry = ExpiryIndex()
        self._ring: deque[tuple[int, frozenset[FactKey], int]] = deque()
        self._ring_revisions = ring_revisions
        self._ring_bytes = ring_bytes
        self._retained_bytes = 0
        self._subscribers = set()
        self._subscriber_envelopes = subscriber_envelopes
        self._subscriber_bytes = subscriber_bytes

    def subscribe(self, scope, kinds, keys, after, callback):
        from hermes_cli.fact_subscription import Subscription
        if after is not None and (not isinstance(after, Position) or not isinstance(after.epoch, str)
                                  or type(after.rev) is not int):
            raise ValueError("invalid_position")
        with self._condition:
            subscription = Subscription(self, scope, frozenset(kinds),
                                        None if keys is None else frozenset(keys), callback, after,
                                        self._subscriber_envelopes, self._subscriber_bytes)
            subscription.enqueue()
            self._subscribers.add(subscription)
            return subscription

    def drain(self) -> None:
        with self._condition:
            subscriptions = tuple(self._subscribers)
        for subscription in subscriptions:
            try:
                subscription.drain()
            except Exception:
                logging.getLogger(__name__).exception("Fact subscriber delivery failed; reset retained")

    def snapshot(self, scope: Scope) -> Snapshot:
        with self._condition:
            return Snapshot(scope, self._epoch, self._rev, self._facts)

    def publish(self, upserts: Iterable[Fact] = (), *,
                deleted_keys: Iterable[FactKey] = ()) -> int:
        # Validate wire values before committing, and never encode under the publication lock.
        # Reserve room for owner-generated stale health without budget growth.
        prepared = []
        for fact in upserts:
            deadline = fact.valid_until
            if deadline is not None and (type(deadline) not in (int, float) or not math.isfinite(deadline)):
                raise ValueError("invalid_validity_deadline")
            prepared.append((fact, encoded_size(fact) + 128))
        return self._publish(prepared, deleted_keys)

    def _publish(self, prepared, deleted_keys):
        with self._condition:
            staged = self._facts
            sizes = self._sizes
            touched = set()
            for key in deleted_keys:
                staged = staged.discard(key)
                sizes = sizes.discard(key)
                touched.add(key)
            for fact, size in prepared:
                old = staged.get(fact.key)
                normalized = replace(fact, rev=old.rev if old else 0)
                if old != normalized:
                    staged = staged.put(fact.key, replace(fact, rev=self._rev + 1))
                    sizes = sizes.put(fact.key, size)
                touched.add(fact.key)
            changed = frozenset(key for key in touched if self._facts.get(key) != staged.get(key))
            if changed:
                fact_bytes = self._fact_bytes + sum(sizes.get(key, 0) - self._sizes.get(key, 0)
                                                    for key in changed)
                if fact_bytes > self._fact_limit:
                    raise ValueError("fact_budget_exceeded")
                size = 1024 + sum(sizes.get(key, len(encode(key))) for key in changed)
                self._fact_bytes = fact_bytes
                self._rev += 1
                self._facts = staged
                self._sizes = sizes
                for key in changed:
                    self._expiry.record(key, staged.get(key))
                self._ring.append((self._rev, changed, size))
                self._retained_bytes += size
                while len(self._ring) > self._ring_revisions or self._retained_bytes > self._ring_bytes:
                    self._retained_bytes -= self._ring.popleft()[2]
                for subscriber in self._subscribers:
                    subscriber.enqueue()
                self._condition.notify_all()
            return self._rev

    def changes(self, scope: Scope, after: Position | None, *,
                kinds: Iterable[str] = (), keys: Iterable[FactKey] | None = None) -> Delta:
        with self._condition:
            return self._changes(scope, after, frozenset(kinds), None if keys is None else frozenset(keys))

    def _changes(self, scope: Scope, after: Position | None,
                 kinds: frozenset[str], keys: frozenset[FactKey] | None) -> Delta:
        reset = self._needs_reset(after)
        if reset:
            selected = set(self._facts)
        else:
            selected = set().union(*(changed for rev, changed, _size in self._ring if rev > after.rev))
        selected = {key for key in selected if key.scope == scope
                    and (not kinds or key.kind in kinds) and (keys is None or key in keys)}
        ordered = sorted(selected, key=lambda key: (key.kind, key.id))
        return Delta(self._epoch, after.rev if after else 0, self._rev,
                     tuple(self._facts[key] for key in ordered if key in self._facts),
                     tuple(key for key in ordered if key not in self._facts), reset)

    def _needs_reset(self, after):
        floor = self._ring[0][0] - 1 if self._ring else self._rev
        return after is None or after.epoch != self._epoch or not floor <= after.rev <= self._rev

    def _delta_size(self, delta):
        # Conservative wire accounting includes record/envelope punctuation and revision digits.
        return (1024 + sum(self._sizes[f.key] for f in delta.upserts)
                + sum(len(encode(key)) + 1 for key in delta.deletes))


def reset_delta(snapshot, kinds=(), keys=None):
    selected = [f for k, f in snapshot._facts.items() if k.scope == snapshot.scope
                and (not kinds or k.kind in kinds) and (keys is None or k in keys)]
    selected.sort(key=lambda f: (f.key.kind, f.key.id))
    return Delta(snapshot.epoch, 0, snapshot.rev, tuple(selected), (), True)
