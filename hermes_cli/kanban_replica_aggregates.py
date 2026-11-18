"""Generation-bound aggregate views over derived bystander scalars. [daveey]"""
from __future__ import annotations

import marshal
from collections.abc import Mapping, Set as AbstractSet


class EvidenceSet(AbstractSet):
    def __init__(self, relevant, scalars, kind, stale, fresh):
        self.relevant = frozenset(relevant)
        self.scalars, self.kind, self.stale, self.fresh = scalars, kind, stale, fresh

    def _closed(self, packed, scope) -> bool:
        """A bystander scalar whose owner stands behind its verified terminal record (as ``_apply`` judges a staged
        row): fresh, or silent since a beat at or after the record's publication."""
        from hermes_cli import kanban_scope_heartbeat as hb
        row = marshal.loads(packed)
        return bool(row[6] & 2) and hb.attests(self.fresh, scope, (row[3] or 0) / 1000.0)

    def _keys(self):
        if self.stale:
            return
        for (gw, key), packed in self.scalars.items():
            profile, board, tid = key.split('/')
            if self.kind == 'owners':
                yield gw, board, tid
            elif self._closed(packed, (gw, profile, board)):
                yield gw, profile, board, tid

    def __iter__(self):
        yield from self.relevant
        seen = set(self.relevant)
        for key in self._keys():
            if key not in seen:
                seen.add(key)
                yield key

    def __len__(self):
        return sum(1 for _ in self)

    def __contains__(self, key):
        if key in self.relevant:
            return True
        if self.stale:
            return False
        if self.kind == 'closed' and isinstance(key, tuple) and len(key) == 4:
            gw, profile, board, tid = key
            packed = self.scalars.get((gw, f'{profile}/{board}/{tid}'))
            return packed is not None and self._closed(packed, (gw, profile, board))
        return any(candidate == key for candidate in self._keys())


class BorrowedSet:
    """An output sink that borrows immutable projection sets, but accepts full-read adds."""
    def __init__(self):
        self.value = set()

    def update(self, value):
        self.value = value

    def add(self, value):
        self.value.add(value)

    def __contains__(self, value):
        return value in self.value


def remembered_publications(moved, previous):
    published = moved.get('published')
    if isinstance(published, Published):
        keys = set()
        live_gateways = {scope[0] for scope in moved.get('live_scopes', ())}
        for card in previous or ():
            # Match _retire_moved's eligibility before touching publication keys.
            if card.get('ownership_state') != 'qualified' or card.get('created_at') is None:
                continue
            scope = (card.get('gateway'), card.get('owner_profile'), card.get('board'))
            # ...or _release_removed_owners': an owner with no live scope at all may be gone, and its row is then
            # retired when another gateway publishes the card.
            if scope not in moved.get('live_scopes', ()) and card.get('gateway') in live_gateways:
                continue
            key = (card.get('board'), card.get('id'), card.get('created_at'))
            try:
                hash(key)
            except TypeError:
                # Leave malformed eligible cards to the original retirement path.
                continue
            keys.add(key)
        moved['published'] = published.lookup_many(keys)


class Published(Mapping):
    def __init__(self, relevant, scalars):
        self.relevant, self.scalars = dict(relevant), scalars

    def lookup_many(self, keys):
        """Resolve remembered publications in one scan, not one scan per card."""
        out = {key: set(self.relevant.get(key, ())) for key in keys}
        for (gw, key), packed in self.scalars.items():
            _, board, tid = key.split('/')
            created = marshal.loads(packed)[4]
            publication = (board, tid, created)
            if created is not None and publication in out:
                out[publication].add(gw)
        return {key: frozenset(gws) for key, gws in out.items() if gws}

    def __getitem__(self, key):
        return self.lookup_many((key,))[key]

    def __iter__(self):
        seen = set(self.relevant)
        yield from seen
        for (_, key), packed in self.scalars.items():
            _, board, tid = key.split('/')
            created = marshal.loads(packed)[4]
            publication = (board, tid, created)
            if created is not None and publication not in seen:
                seen.add(publication)
                yield publication

    def __len__(self):
        return sum(1 for _ in self)


def snapshot(state):
    stale, fresh = state.key[2:]
    scalars = state.bystanders
    seen = dict(state.seen)
    for (gw, _), packed in scalars.items():
        updated = marshal.loads(packed)[3]
        seen[gw] = max(seen.get(gw, 0.0), (updated or 0) / 1000.0)
    return {'owners': EvidenceSet(() if stale else state.owners, scalars, 'owners', stale, fresh),
            'closed': EvidenceSet(state.closed, scalars, 'closed', stale, fresh), 'seen': seen,
            'moved': {} if stale else {'published': Published(state.published_out, scalars), 'live_scopes': frozenset(fresh)}}
