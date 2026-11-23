"""Owner-side bridge from pure canonical projections to atomic ViewFacts. [daveey]

Consumer owners supply their extracted serializers; this module does not define
alternate wire fields. No registration here is production coverage by itself.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from types import MappingProxyType

from hermes_cli.fact_contract import Fact
from hermes_cli.fact_reducers import ReducerSpec


@dataclass(frozen=True)
class Projection:
    name: str
    input_kinds: tuple[str, ...]
    dependencies: Callable
    render: Callable


def view_reducer(projections: Iterable[Projection]) -> ReducerSpec:
    """Build once at owner startup, after all canonical projections register."""
    registry = {}
    for projection in projections:
        if projection.name in registry:
            raise ValueError("duplicate projection name")
        registry[projection.name] = projection
    kinds = tuple(sorted({kind for item in registry.values() for kind in item.input_kinds}))

    def select(key):
        name, selector = json.loads(key.id)
        if not isinstance(selector, dict):
            raise ValueError("selector must be a mapping")
        if not isinstance(name, str) or name not in registry:
            raise ValueError("unregistered projection")
        return registry[name], selector

    def inputs(key, snapshot):
        projection, selector = select(key)
        allowed = snapshot.allowed_kinds
        snapshot.allowed_kinds = projection.input_kinds
        try:
            keys = set(projection.dependencies(key.scope, selector, snapshot))
            if any(item.scope != key.scope or item.kind not in projection.input_kinds for item in keys):
                raise ValueError('invalid projection dependency')
            return keys, False
        except Exception:  # health: allow BLE001 -- malformed dependency callbacks fail one view, not source/health publication
            return set(snapshot.reads), True
        finally:
            snapshot.allowed_kinds = allowed

    def dependencies(key, snapshot):
        return inputs(key, snapshot)[0]

    def reduce(key, snapshot, now):
        projection, selector = select(key)
        keys, failed = inputs(key, snapshot)
        # Membership lookups performed by dependencies also carry freshness.
        keys.update(snapshot.reads)
        if any(item.kind not in projection.input_kinds for item in keys):
            raise ValueError("projection read an undeclared kind")
        facts = {item: snapshot.get(item) for item in sorted(keys, key=lambda item: (item.kind, item.id))}
        missing = any(fact is None for fact in facts.values())
        present = tuple(fact for fact in facts.values() if fact is not None)
        health = "unknown" if missing or any(f.health == "unknown" for f in present) else (
            "stale" if any(f.health != "fresh" or f.valid_until is not None and f.valid_until <= now
                           for f in present) else "fresh")
        deadlines = [f.valid_until for f in present if f.valid_until is not None]
        vector = tuple(entry for fact in present for entry in fact.source_vector)
        value = {"view": projection.name, "selector": selector}
        error_code = None if health == "fresh" else "dependency_unavailable"
        if failed:
            health, error_code = 'unknown', 'projection_dependencies_failed'
        if health == "fresh":
            try:
                payload = projection.render(selector, MappingProxyType(facts), now)
                encoded = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode()
                value.update(payload=payload, encoded=encoded)
            except Exception:  # health: allow BLE001 -- plugin serializers fail one view, never the source generation; no exception data is exported
                health, error_code = "unknown", "projection_failed"
        return Fact(key, value, source_vector=vector, observed_at=min((f.observed_at for f in present), default=now),
                    valid_until=min(deadlines, default=None), health=health, error_code=error_code)

    return ReducerSpec("canonical-views", 1, "ViewFact", kinds, dependencies, reduce, select)
