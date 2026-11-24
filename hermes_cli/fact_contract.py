"""Transport-neutral immutable values for the shared projection service. [daveey]"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from types import MappingProxyType
from typing import Any, Literal

CONTRACT_VERSION = 1
Health = Literal["fresh", "stale", "unknown"]


def freeze(value: Any) -> Any:
    """Detach projection data from mutable serializer and caller ownership."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: freeze(item) for key, item in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(freeze(item) for item in value)
    if value is None or isinstance(value, (str, bytes, bool, int, float)):
        return value
    if type(value) in (Scope, SourceId, FactKey):
        return value
    raise TypeError(f"unsupported projection value: {type(value).__name__}")


@dataclass(frozen=True)
class Scope:
    install_id: str
    profile_id: str
    board_id: str | None = None

    def __post_init__(self):
        if (type(self.install_id) is not str or type(self.profile_id) is not str
                or self.board_id is not None and type(self.board_id) is not str):
            raise TypeError("scope identity fields must be strings")


@dataclass(frozen=True)
class SourceId:
    scope: Scope
    kind: str
    origin_id: str

    def __post_init__(self):
        if type(self.scope) is not Scope or type(self.kind) is not str or type(self.origin_id) is not str:
            raise TypeError("source identity requires a Scope and strings")


@dataclass(frozen=True)
class FactKey:
    scope: Scope
    kind: str
    id: str

    def __post_init__(self):
        if type(self.scope) is not Scope or type(self.kind) is not str or type(self.id) is not str:
            raise TypeError("fact identity requires a Scope and strings")


@dataclass(frozen=True)
class Fact:
    key: FactKey
    value: Any
    rev: int = 0
    source_vector: tuple = ()
    observed_at: float = 0.0
    valid_until: float | None = None
    health: Health = "unknown"
    error_code: str | None = None

    def __post_init__(self) -> None:
        # Freeze only: evolve() skips this for fields it copies, so any check added here must be added there too.
        object.__setattr__(self, "value", freeze(self.value))
        object.__setattr__(self, "source_vector", freeze(tuple(self.source_vector)))


_FACT_FIELDS = tuple(item.name for item in fields(Fact))


def evolve(fact: Fact, **changes: Any) -> Fact:
    """``dataclasses.replace`` for a Fact that does not re-freeze what the Fact already holds. [daveey]

    A Fact froze its value and source_vector when it was built, so a copy that only changes rev, health,
    error_code or timing can share them. replace() runs __post_init__ again, which deep-walks the whole value
    once per copy; on the hub (2026-11-23) those walks were ~27% of the fact-change-feed thread. A value or
    source_vector given in *changes* is frozen as usual.
    """
    unknown = set(changes).difference(_FACT_FIELDS)
    if unknown:
        raise TypeError(f"unknown Fact field: {sorted(unknown)[0]}")
    if type(fact) is not Fact:
        raise TypeError("evolve requires a Fact")
    copy = object.__new__(Fact)
    for name in _FACT_FIELDS:
        object.__setattr__(copy, name, changes[name] if name in changes else getattr(fact, name))
    if "value" in changes:
        object.__setattr__(copy, "value", freeze(changes["value"]))
    if "source_vector" in changes:
        object.__setattr__(copy, "source_vector", freeze(tuple(changes["source_vector"])))
    return copy


@dataclass(frozen=True)
class Position:
    epoch: str
    rev: int


@dataclass(frozen=True)
class Ready:
    encoded: bytes
    epoch: str
    rev: int


@dataclass(frozen=True)
class Pending:
    reason: str = "not_materialized"


@dataclass(frozen=True)
class Unavailable:
    reason: str


@dataclass(frozen=True)
class Delta:
    epoch: str
    prev: int
    rev: int
    upserts: tuple[Fact, ...] = ()
    deletes: tuple[FactKey, ...] = ()
    reset: bool = False
    health: Mapping = field(default_factory=lambda: MappingProxyType({}))
