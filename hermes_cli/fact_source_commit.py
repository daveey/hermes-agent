"""Single-owner captured content -> atomic reducer publication. [daveey]

Content keys are source-namespaced inputs, not completed TaskFact/SessionFact
schemas. Materializers must supply those schemas from their declared inputs.
Membership, source cursor, deletes, health and dependent views commit together;
private membership/cursor advance only after the store accepts the generation.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, replace

from hermes_cli.fact_contract import Fact, FactKey, freeze


class SourcePublisher:
    """One instance per authorized adapter, called only by the feed owner.

    No source I/O, global discovery or consumer activation occurs here. Startup
    owns the engine and must not register duplicate source identities. Source key
    prefixes prevent local and shadow rows with identical ids colliding.
    """
    def __init__(self, engine, source, kind):
        if source.scope != engine.scope:
            raise ValueError('source scope mismatch')
        if kind in ('SourceState', 'SourceMembership') or kind in engine._specs:
            raise ValueError('source kind reserved or derived')
        self.engine, self.source, self.kind = engine, source, kind
        self.cursor = None
        self._members = set()
        self._failed = False
        identity = json.dumps([source.kind, source.origin_id], separators=(',', ':'))
        self.state_key = FactKey(source.scope, 'SourceState', identity)
        self.membership_key = FactKey(source.scope, 'SourceMembership', identity)

    def key(self, identity):
        if type(identity) is not str:
            raise ValueError('source record id must be a string')
        token = json.dumps([self.source.kind, self.source.origin_id, identity], separators=(',', ':'))
        return FactKey(self.source.scope, self.kind, token)

    def _fact(self, key, value, snapshot, capture, vector):
        value = freeze(value)
        old = snapshot.get(key)
        # Unchanged rows retain provenance/observation, avoiding false dirty
        # dependencies on FULL or a cursor-only wake. Recovery changes health.
        if old is not None and old.value == value:
            return replace(old, health='fresh', error_code=None)
        return Fact(key, value, source_vector=vector,
                    observed_at=capture.observed_at, health='fresh')

    def commit(self, capture):
        if capture.before != self.cursor:
            raise ValueError('source cursor mismatch')
        if capture.after is None:
            raise ValueError('source cursor coverage missing')
        records = {}
        ids = set(capture.ids)
        for row in capture.records:
            identity = row['id']
            self.key(identity)
            if identity in records:
                raise ValueError('duplicate source record')
            if identity not in ids:
                raise ValueError('record outside captured ids')
            records[identity] = row
        present = set(records)
        removed = (self._members - present) if capture.full else (ids - present)
        membership_changed = bool((removed & self._members) or (present - self._members))
        members = (self._members - removed) | present if membership_changed else self._members
        snapshot = self.engine.store.snapshot(self.source.scope)
        previous_membership = snapshot.get(self.membership_key)
        membership_value = (tuple(self.key(i) for i in sorted(members))
                            if membership_changed or previous_membership is None or self.cursor is None
                            else previous_membership.value)
        cursor = asdict(capture.after)
        vector = ((self.source, cursor),)
        facts = [self._fact(self.key(identity), row, snapshot, capture, vector)
                 for identity, row in records.items()]
        # Retry after a failed capture can legitimately be an empty delta: its
        # cursor still covers unchanged last-good rows. Recover them atomically.
        if self._failed:
            for identity in members - present:
                old = snapshot.get(self.key(identity))
                if old is None:
                    raise ValueError('source membership lost')
                facts.append(replace(old, health='fresh', error_code=None))
        if membership_changed or previous_membership is None or previous_membership.health != 'fresh':
            facts.append(self._fact(self.membership_key, membership_value, snapshot, capture, vector))
        facts.append(self._fact(self.state_key, {'source_kind': self.source.kind,
                     'origin_id': self.source.origin_id, 'cursor_state': cursor,
                     'revision_token': json.dumps(cursor, sort_keys=True, separators=(',', ':'))},
                     snapshot, capture, vector))
        revision = self.engine.publish(facts, deleted_keys=(self.key(i) for i in removed),
                                       now=time.time())
        self.cursor, self._members, self._failed = capture.after, members, False
        return revision

    def failure(self):
        """Sanitized last-good health; never publish exception text or deletions."""
        snapshot = self.engine.store.snapshot(self.source.scope)
        keys = [self.key(i) for i in self._members] + [self.membership_key, self.state_key]
        facts = []
        for key in keys:
            old = snapshot.get(key)
            facts.append(replace(old, health='stale' if self.cursor is not None else 'unknown',
                                 error_code='source_unavailable') if old else
                         Fact(key, None, health='unknown', error_code='source_unavailable'))
        self.engine.publish(facts, now=time.time())
        self._failed = True
