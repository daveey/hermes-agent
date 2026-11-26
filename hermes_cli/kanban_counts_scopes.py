"""Bounded count snapshots for multi-profile tree annotation. [daveey]

The legacy active slot is retained for its readers; switching profiles restores
all of its fields together. Restoring is NOT validation: callers still check the
board fingerprint, TTL and time-derived maximum age before returning counts.
Every operation is under kanban_session_activity._cache_lock. Published count
maps are read-only internally (callers construct their own annotation rows).
"""
from collections import OrderedDict

MAX_SCOPES = 32
_entries: OrderedDict[tuple[str, ...], tuple] = OrderedDict()
_FIELDS = ("_cache", "_cache_fingerprint", "_cache_read_at", "_cache_revalidatable", "_cache_generation")
_BOXES = ("_waiting_cache", "_parked_cache", "_stalled_cache", "_remote_keys_cache", "_local_keys_cache", "_execution_cache")


class CountSnapshot(tuple):
    """Keep the legacy nine-part read shape, with evidence bound to that generation."""
    execution: dict
    source: tuple | None = None

    def __new__(cls, values, execution):
        snapshot = super().__new__(cls, values)
        snapshot.execution = execution
        return snapshot


def remember_locked() -> None:
    from hermes_cli import kanban_session_activity as ksa

    scope = ksa._cache_scope
    assert scope is not None
    _entries[scope] = (tuple(getattr(ksa, name) for name in _FIELDS),
                       tuple(getattr(ksa, name)[0] for name in _BOXES))
    _entries.move_to_end(scope)
    while len(_entries) > MAX_SCOPES:
        _entries.popitem(last=False)


def restore_locked(scope: tuple[str, ...]) -> None:
    from hermes_cli import kanban_session_activity as ksa

    if ksa._cache is None:
        # Dropping the active slot is an explicit invalidation, not a scope miss.
        _entries.clear()
        return
    if ksa._cache_scope == scope:
        if scope in _entries:
            _entries.move_to_end(scope)
        return
    saved = _entries.get(scope)
    if saved is None:
        return
    values, boxes = saved
    for name, value in zip(_FIELDS, values):
        setattr(ksa, name, value)
    for name, value in zip(_BOXES, boxes):
        getattr(ksa, name)[0] = value
    ksa._cache_scope = scope
    _entries.move_to_end(scope)


def read_at_locked(scope: tuple[str, ...]) -> float | None:
    """Monotonic time of the read behind ``scope``'s counts, whichever scope owns the active slot; None = none."""
    from hermes_cli import kanban_session_activity as ksa

    if ksa._cache is None:
        return None
    if ksa._cache_scope == scope:
        return ksa._cache_read_at
    saved = _entries.get(scope)
    return None if saved is None else saved[0][_FIELDS.index("_cache_read_at")]


def result_locked(include_waiting: bool, include_snapshot: bool) -> tuple:
    """Annotation identities travel with their count generation; caller holds the cache lock."""
    from hermes_cli import kanban_session_activity as ksa

    assert ksa._cache is not None
    counts = ksa._cache[1:5]
    if include_snapshot:
        parked, held = ksa._parked_cache[0]
        snapshot = CountSnapshot((*counts, dict(ksa._waiting_cache[0]), parked, held, ksa._stalled_cache[0],
                                  ksa._identity.merge_keys(ksa._remote_keys_cache[0], ksa._local_keys_cache[0])),
                                 ksa._execution_cache[0])
        # Source custody is captured under the same lock as the returned maps.
        snapshot.source = (ksa._cache_scope, ksa._cache_read_at, ksa._cache_fingerprint,
                           ksa._cache_revalidatable, ksa._cache_generation)
        return snapshot
    return (*counts, dict(ksa._waiting_cache[0])) if include_waiting else counts


def reset_locked() -> None:
    _entries.clear()
