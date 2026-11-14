"""Display-only semantic tokens; strict publication identity remains separate."""
from __future__ import annotations

import hashlib
import json
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hermes_cli.kanban_replica_rows import Rows


class TaskTokens:
    """One caller-owned inventory over immutable replica Rows.

    Caller serializes access and bounds inventory lifetime with its replica
    reader. This class neither opens stores nor certifies origin dependencies.
    Generation reuse requires the same known file identity; missed deltas scan
    the inventory, but unchanged packed payloads need no decoding. Failed reads
    leave the previous generation untouched.
    """

    def __init__(self) -> None:
        self._view: Rows | None = None
        self._tokens: MappingProxyType[tuple, str] = MappingProxyType({})
        self.base: MappingProxyType | None = None
        self.changed: frozenset | None = None

    def advance(self, view: Rows) -> MappingProxyType[tuple, str]:
        from hermes_cli.kanban_replica_rows import text

        old = self._view
        same = (old is not None and view.ident is not None
                and (old.path, old.ident) == (view.path, view.ident))
        if same and old.gen == view.gen:
            return self._tokens
        delta = same and view.base_gen == old.gen
        removed = view.removed if delta else (self._tokens.keys() - view.by_key.keys() if same else ())
        candidates = view.changed if delta else view.by_key.keys()
        tokens: dict | None = None if same else {}
        moved: set | None = set() if same else None
        for key in removed:
            if key in self._tokens:
                if tokens is None:
                    tokens = dict(self._tokens)
                tokens.pop(key, None)
                if moved is not None:
                    moved.add(key)
        for key in candidates:
            row = view.by_key[key]
            previous = old.by_key.get(key) if same else None
            if previous is not None and previous[2] == row[2]:
                continue
            task = json.loads(text(row[2]))
            if not isinstance(task, dict) or not task.get("id"):
                raise ValueError("invalid replica task data")
            parts = str(row[1]).split("/")
            if len(parts) not in (2, 3) or not all(parts) or parts[-1] != str(task["id"]):
                raise ValueError("replica task identity mismatch")
            token = task_token(task)
            if tokens is None:
                if self._tokens.get(key) == token:
                    continue  # a liveness-only rewrite: the inventory (and its identity) is unchanged
                tokens = dict(self._tokens)
            if moved is not None and self._tokens.get(key) != token:
                moved.add(key)
            tokens[key] = token
        self._view = view
        if tokens is not None:  # unchanged tokens keep the same mapping object: callers memo on identity
            # [daveey] ``changed``: keys whose token differs from ``base`` (None = rebuilt from scratch; a consumer
            # must then rescan). Lets DependencySelector skip its O(N) walk on a moved generation.
            self.base, self.changed = self._tokens, (frozenset(moved) if moved is not None else None)
            self._tokens = MappingProxyType(tokens)
        return self._tokens


def task_token(task: dict[str, Any]) -> str:
    """Liveness-normalized task, retaining original hash-bound validity.

    Only a valid original observation loses its task_hash. Invalid/malformed
    evidence stays verbatim, so a stale hash never looks like a valid restamp.
    All other fields (including observation clocks) remain substantive. This is
    NOT a strict publication/version key, nor a complete origin dependency key.
    """
    from hermes_cli.kanban_origin_ledger import _owner_evidence

    valid = bool(_owner_evidence(task))
    normalized = {k: v for k, v in task.items() if k not in ("activity_at", "activity_kind")}
    if valid:
        normalized["origin_observation"] = {
            k: v for k, v in task["origin_observation"].items() if k != "task_hash"}
    encoded = json.dumps([valid, normalized], sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()
