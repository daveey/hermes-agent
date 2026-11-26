"""Holder-only counts with source-owned age; never publish reuse as a new exact read. [daveey]"""
from __future__ import annotations

import copy
import threading
from dataclasses import dataclass

from hermes_cli.kanban_counts_scopes import CountSnapshot

MAX_SCOPES = 64
_entries: dict[tuple, Source] = {}
_use = threading.local()


@dataclass(frozen=True)
class Source:
    snapshot: CountSnapshot
    key: object
    mono: float
    wall: float
    raw: tuple
    token: object


def _detach(value):
    return CountSnapshot(copy.deepcopy(tuple(value)), copy.deepcopy(value.execution))


def read_at(scope):
    source = _entries.get(scope)
    return None if source is None else source.mono


def needs_read(scope):
    from hermes_cli import kanban_annotation_host as host
    from hermes_cli import kanban_session_activity as ksa

    source = _entries.get(scope)
    return (source is None or not 0 <= ksa.time.monotonic() - source.mono < host.STRICT_REFRESH_AGE_S
            or source.wall > ksa.time.time() or source.token != host._mutation.current(scope))


def begin(scope, keys):
    from hermes_cli import kanban_annotation_host as host
    from hermes_cli import kanban_session_activity as ksa

    clear_use()
    _use.scope, _use.keys = scope, keys
    _use.started, _use.token = ksa.time.monotonic(), host._mutation.current(scope)


def clear_use():
    _use.scope, _use.keys, _use.source, _use.reused = None, None, None, False
    _use.started, _use.token = None, None


def reused():
    return bool(getattr(_use, 'reused', False))


def observed_at(default):
    source = getattr(_use, 'source', None)
    return min(default, source.wall) if source is not None else default


def _valid(keys):
    receipt = getattr(keys, 'receipt', None)
    return keys is not None and (receipt is None or receipt.valid())


def read(reader):
    from hermes_cli import kanban_annotation_host as host
    from hermes_cli import kanban_annotation_parts as parts
    from hermes_cli import kanban_session_activity as ksa

    scope = ksa._origin_scope()
    if getattr(ksa._revalidate.force_read, 'scope', None) != scope:
        return reader()
    keys = getattr(_use, 'keys', None) if getattr(_use, 'scope', None) == scope else None
    if getattr(_use, 'scope', None) != scope:
        return reader()  # legacy holder: no counts plan or custody to change
    source = _entries.get(scope)
    now, wall = ksa.time.monotonic(), ksa.time.time()
    token = host._mutation.current(scope)
    if (source is not None and not ksa._revalidate.forced(scope, parts.COUNTS)
            and keys is not None and _valid(keys) and keys[parts.COUNTS] == source.key and token == source.token
            and 0 <= now - source.mono < host.STRICT_REFRESH_AGE_S and source.wall <= wall):
        value = _detach(source.snapshot)
        if _valid(keys):
            _use.source, _use.reused = source, True
            return value
    # An unforced/missing/expired receipt cannot borrow the request cache's age.
    forced = getattr(ksa._revalidate.force_read, 'parts', None)
    content_only = getattr(ksa._revalidate.force_read, 'content_only_boards', False)
    if needs_read(scope):
        ksa._revalidate.force_read.content_only_boards = False
    ksa._revalidate.force_read.parts = frozenset(forced or ()) | {parts.COUNTS}
    started = wall
    try:
        value = reader()
    finally:
        ksa._revalidate.force_read.parts = forced
        ksa._revalidate.force_read.content_only_boards = content_only
    _entries.pop(scope, None)
    raw = getattr(value, 'source', None)
    started_mono = getattr(_use, 'started', None)
    if (raw is not None and raw[0] == scope and raw[3] and keys is not None and _valid(keys)
            and started_mono is not None and raw[1] >= started_mono
            and token == getattr(_use, 'token', None)):
        age = ksa.time.monotonic() - raw[1]
        source_wall = min(started, ksa.time.time() - age)
        if age >= 0 and source_wall <= ksa.time.time():
            source = Source(_detach(value), keys[parts.COUNTS], raw[1], source_wall, raw, token)
            while len(_entries) >= MAX_SCOPES:
                evicted = next(iter(_entries))
                failed(evicted)
                parts._seen.pop(evicted, None)
            _entries[scope] = source
            _use.source = source
    return value


def failed(scope):
    _entries.pop(scope, None)


def reset():
    _entries.clear()
    clear_use()
