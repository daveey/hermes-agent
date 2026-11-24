"""Owner-only dependency reductions; no source I/O or consumer callbacks. [daveey]"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from graphlib import TopologicalSorter

from hermes_cli.fact_contract import Fact, FactKey, Scope, evolve
from hermes_cli.fact_store import FactStore, Snapshot


def _changed(old: Fact | None, new: Fact | None) -> bool:
    return old != (evolve(new, rev=old.rev if old else 0) if new else None)


class StagedSnapshot:
    """Read-through batch overlay; retained public snapshots never see staging."""

    def __init__(self, base: Snapshot, staged: dict[FactKey, Fact | None]):
        self.base = base
        self.staged = staged
        self.reads: set[FactKey] = set()
        self.allowed_kinds: tuple[str, ...] = ()

    def get(self, key: FactKey) -> Fact | None:
        if key.kind not in self.allowed_kinds:
            raise ValueError("undeclared input kind")
        if key.scope != self.base.scope:
            return None
        self.reads.add(key)
        return self.staged[key] if key in self.staged else self.base.get(key)

    def get_many(self, keys: Iterable[FactKey]) -> tuple[Fact | None, ...]:
        return tuple(self.get(key) for key in keys)


@dataclass(frozen=True)
class ReducerSpec:
    name: str
    version: int
    output_kind: str
    input_kinds: tuple[str, ...]
    dependencies: Callable[[FactKey, StagedSnapshot], set[FactKey]]
    reduce: Callable[[FactKey, StagedSnapshot, float], Fact | None]
    validate: Callable[[FactKey], object] | None = None


class ReducerEngine:
    """Single projection owner supplies source batches and binds demanded output keys.

    Dependents must declare membership keys as well as current members. Kind-level
    ordering makes dynamic edge changes safe: a reducer cannot read a later kind.
    This engine is not a concurrent publication facade; only its owner calls publish.
    ``now`` is wall-clock epoch seconds, matching Fact observation/expiry fields
    and canonical serializers. Feed scheduling alone uses monotonic deadlines.
    """

    def __init__(self, store: FactStore, scope: Scope):
        self.store = store
        self.scope = scope
        self._specs: dict[str, ReducerSpec] = {}
        self._order: dict[str, int] = {}
        self._bound: set[FactKey] = set()
        self._pending: set[FactKey] = set()
        self._dependencies: dict[FactKey, set[FactKey]] = {}
        self._reverse: dict[FactKey, set[FactKey]] = defaultdict(set)

    def register(self, spec: ReducerSpec) -> None:
        if spec.output_kind in self._specs:
            raise ValueError("output kind already registered")
        specs = {**self._specs, spec.output_kind: spec}
        order = tuple(TopologicalSorter({kind: item.input_kinds for kind, item in specs.items()}).static_order())
        self._specs = specs
        self._order = {kind: index for index, kind in enumerate(order)}

    def bind(self, key: FactKey) -> None:
        if key.scope != self.scope:
            raise ValueError("output scope mismatch")
        if key.kind not in self._specs:
            raise ValueError("unregistered output kind")
        validate = self._specs[key.kind].validate
        if validate is not None:
            validate(key)
        self._bound.add(key)
        self._pending.add(key)

    def unbind(self, key: FactKey) -> None:
        """Withdraw an unpublished demand after failure, without deleting facts."""
        self._bound.discard(key)
        self._pending.discard(key)
        self._install_edges({key: set()})
        self._dependencies.pop(key, None)

    def _closure(self, dirty: set[FactKey]) -> set[FactKey]:
        closure = set(dirty)
        todo = list(dirty)
        while todo:
            for dependent in self._reverse.get(todo.pop(), ()):
                if dependent not in closure:
                    closure.add(dependent)
                    todo.append(dependent)
        return closure & self._bound

    def _install_edges(self, edges: dict[FactKey, set[FactKey]]) -> None:
        for key, dependencies in edges.items():
            previous = self._dependencies.get(key, set())
            for removed in previous - dependencies:
                self._reverse[removed].discard(key)
                if not self._reverse[removed]:
                    del self._reverse[removed]
            for added in dependencies - previous:
                self._reverse[added].add(key)
            self._dependencies[key] = dependencies

    def _reduce(self, key: FactKey, snapshot: StagedSnapshot, now: float) -> tuple[Fact | None, set[FactKey]]:
        spec = self._specs[key.kind]
        snapshot.reads.clear()
        snapshot.allowed_kinds = spec.input_kinds
        dependencies = set(spec.dependencies(key, snapshot))
        if any(dep.scope != self.scope for dep in dependencies):
            raise ValueError("dependency scope mismatch")
        if any(dep.kind not in spec.input_kinds for dep in dependencies):
            raise ValueError("undeclared dependency kind")
        fact = spec.reduce(key, snapshot, now)
        if fact is not None and fact.key != key:
            raise ValueError("reducer output key mismatch")
        return fact, dependencies | snapshot.reads

    def publish(self, upserts: Iterable[Fact] = (), *, deleted_keys: Iterable[FactKey] = (),
                now: float) -> int:
        base = self.store.snapshot(self.scope)
        staged = dict.fromkeys(deleted_keys)
        staged.update((fact.key, fact) for fact in upserts)
        if any(key.scope != self.scope for key in staged):
            raise ValueError("source scope mismatch")
        if any(key.kind in self._specs for key in staged):
            raise ValueError("source cannot publish a derived key")
        staged = {key: evolve(fact, rev=base.rev + 1) if fact else None
                  for key, fact in staged.items() if _changed(base.get(key), fact)}
        dirty = self._closure(set(staged) | self._pending)
        snapshot = StagedSnapshot(base, staged)
        edges = {}
        for key in sorted(dirty, key=lambda item: (self._order[item.kind], item.id)):
            fact, edges[key] = self._reduce(key, snapshot, now)
            old = base.get(key)
            if _changed(old, fact):
                staged[key] = evolve(fact, rev=base.rev + 1) if fact else None
            # Unchanged outputs remain visible through the base snapshot. Do
            # not send them through FactStore's wire validation/encoding again;
            # still install newly discovered edges after successful publication.
        revision = self.store.publish((fact for fact in staged.values() if fact is not None),
                                      deleted_keys=(key for key, fact in staged.items() if fact is None))
        self._install_edges(edges)
        self._pending.difference_update(dirty)
        return revision
