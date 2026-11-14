"""Pure, bounded dependency-key selector over replica Rows + semantic task tokens.

NOT the production union reader and NOT an authority/freshness claim: the result is a read-only snapshot of
``(key, semantic token or None)`` pairs for one origin ``(gateway, profile)``. It performs no store I/O.

Selected keys (all ``(gw, key)`` replica keys):
  * seeds: rows whose record links a session of this origin (``linked_sessions`` semantics), explicit ledger
    ``owner_keys`` and ``retained_keys`` (present or absent; an absent key selects with token ``None``, so its
    deletion/insertion changes the result);
  * every publisher row, on ANY gateway/profile and in either legacy 2-part or 3-part key form, that shares a
    seed's ``(board, task_id)``. This is deliberately over-inclusive: the minimal rule would also match
    ``created_at``; ignoring it closes legacy-twin / moved-publisher / alias support at the price of occasionally
    selecting an unrelated same-id record. Unrelated board/id rows never enter, and there is no whole-replica key.

Cost: a full call scans all N rows (O(N) dict walk + key split); only rows whose token changed are JSON-decoded.
[daveey] With ``changed``/``base`` (the keys :class:`TaskTokens` reports changed since the mapping this selector
last saw) a call is O(changed + picked) and returns the SAME result object when no changed row touches a seed
identity: on a 40k-row replica the full scan is ~20 ms per moved generation (benchmark_kanban_annotation_holder).
Any mismatch (other base, other origin, first call) falls back to the full scan. Invalid identities or tokens
inconsistent with the rows raise ``ValueError`` (fail closed) and leave the previous state untouched.
"""
from __future__ import annotations

import json
from collections.abc import Collection, Iterable, Mapping
from typing import TYPE_CHECKING

from hermes_cli.kanban_replica_projection import LINK_RELS, link_targets

if TYPE_CHECKING:
    from hermes_cli.kanban_replica_rows import Rows


def _identity(key) -> tuple[str, str]:
    """(board, task_id) from a replica key ``(gw, "profile/board/id" | "board/id")``; ValueError if invalid."""
    if not isinstance(key, tuple) or len(key) != 2 or not isinstance(key[1], str):
        raise ValueError("invalid replica key")
    parts = key[1].split("/")
    if len(parts) not in (2, 3) or not all(parts):
        raise ValueError("invalid replica task key")
    return parts[-2], parts[-1]


class DependencySelector:
    """Caller-owned cache of decoded link/board facts keyed by semantic token. Caller serializes access.

    The cache holds one entry per CURRENT row (history never accumulates) and changes only after a fully
    successful call. No global state.
    """

    def __init__(self) -> None:
        self._facts: dict[tuple, tuple[str, tuple[str, str], frozenset]] = {}
        self._by_identity: dict[tuple[str, str], set[tuple]] = {}
        self._linking: dict[tuple[str, str], int] = {}  # identity -> rows under it that link the origin
        self._origin: tuple | None = None
        self._tokens: Mapping | None = None  # the mapping the structures above describe
        self._extra: tuple = ()
        self._extra_idents: frozenset = frozenset()
        self._result: tuple | None = None
        self.decodes = 0  # work counter for tests
        self.full_scans = 0

    def _fact(self, key, row, token) -> tuple[str, tuple[str, str], frozenset]:
        from hermes_cli.kanban_replica_rows import text

        if not isinstance(token, str) or not token:
            raise ValueError("invalid task token")
        kept = self._facts.get(key)
        if kept is not None and kept[0] == token:
            return kept
        ident = _identity(key)
        try:
            task = json.loads(text(row[2]))
        except (ValueError, TypeError) as exc:
            raise ValueError("invalid replica task data") from exc
        if not isinstance(task, dict) or not task.get("id") or ident[1] != str(task["id"]):
            raise ValueError("replica task identity mismatch")
        self.decodes += 1
        # Invalid endpoint shapes cannot equal the string origin scope;
        # don't let an unrelated malformed link make a valid one unkeyable.
        targets = (pair for pair in link_targets(task) if all(isinstance(part, str) for part in pair))
        return token, ident, frozenset(targets)

    def select(self, rows: Rows, tokens: Mapping[tuple, str], gateway: str, profile: str,
               owner_keys: Iterable[tuple] = (), retained_keys: Iterable[tuple] = (), *,
               changed: Collection[tuple] | None = None, base: Mapping | None = None) -> tuple:
        by_key = rows.by_key
        origin = (gateway, profile)
        extra = tuple(owner_keys) + tuple(retained_keys)
        # Identities are validated (fail closed) whenever the seed keys differ from the last validated ones.
        extra_idents = self._extra_idents if extra == self._extra else frozenset(_identity(key) for key in extra)
        if self._result is not None and tokens is self._tokens and self._origin == origin:
            if extra != self._extra:  # only the ledger's owner/retained seeds moved
                self._extra, self._extra_idents = extra, extra_idents
                self._result = self._picked(tokens, set(self._linking) | extra_idents, extra)
            return self._result
        if (changed is None or base is None or base is not self._tokens or self._origin != origin
                or self._result is None or len(tokens) != len(by_key)):
            return self._full(by_key, tokens, origin, extra, extra_idents)
        # Phase 1 (may raise; nothing mutated): new facts for every changed key.
        updates = []
        for key in changed:
            if key in tokens:
                row = by_key.get(key)
                if row is None:
                    raise ValueError("tokens inconsistent with rows")
                updates.append((key, self._fact(key, row, tokens[key])))
            elif key in by_key:
                raise ValueError("tokens inconsistent with rows")
            else:
                updates.append((key, None))
        touched, flipped = set(), False
        # Phase 2 (no raising operations): apply.
        for key, fact in updates:
            old = self._facts.pop(key, None)
            if old is not None:
                touched.add(old[1])
                group = self._by_identity.get(old[1])
                if group is not None:
                    group.discard(key)
                    if not group:
                        del self._by_identity[old[1]]
                if origin in old[2]:
                    self._linking[old[1]] -= 1
                    if not self._linking[old[1]]:
                        del self._linking[old[1]]
                        flipped = True  # identity stopped being a seed
            if fact is not None:
                self._facts[key] = fact
                touched.add(fact[1])
                self._by_identity.setdefault(fact[1], set()).add(key)
                if origin in fact[2]:
                    flipped = flipped or fact[1] not in self._linking
                    self._linking[fact[1]] = self._linking.get(fact[1], 0) + 1
        self._tokens = tokens
        if extra != self._extra:
            self._extra, self._extra_idents = extra, frozenset(extra_idents)
        elif not flipped and not any(i in self._linking or i in self._extra_idents for i in touched):
            # Seed set unchanged and no changed row is under a seed identity: the same selection.
            return self._result
        self._result = self._picked(tokens, set(self._linking) | self._extra_idents, extra)
        return self._result

    def _picked(self, tokens: Mapping, seeds: set, extra: tuple) -> tuple:
        picked: set[tuple] = set(extra)
        for ident in seeds:
            picked.update(self._by_identity.get(ident, ()))
        return tuple((key, tokens.get(key)) for key in sorted(picked, key=lambda k: (str(k[0]), str(k[1]))))

    def _full(self, by_key, tokens, origin, extra, extra_idents) -> tuple:
        if set(tokens) != set(by_key):
            raise ValueError("tokens inconsistent with rows")
        facts: dict[tuple, tuple[str, tuple[str, str], frozenset]] = {}
        by_identity: dict[tuple[str, str], set[tuple]] = {}
        linking: dict[tuple[str, str], int] = {}
        for key, row in by_key.items():
            fact = facts[key] = self._fact(key, row, tokens[key])
            by_identity.setdefault(fact[1], set()).add(key)
            if origin in fact[2]:
                linking[fact[1]] = linking.get(fact[1], 0) + 1
        self.full_scans += 1
        self._facts, self._by_identity, self._linking = facts, by_identity, linking
        self._origin, self._tokens, self._extra, self._extra_idents = origin, tokens, extra, extra_idents
        self._result = self._picked(tokens, set(linking) | extra_idents, extra)
        return self._result


__all__ = ["LINK_RELS", "DependencySelector"]
