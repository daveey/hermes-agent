"""Which parts of the annotation an event refresh must really read. [daveey]

The event-driven holder (``kanban_annotation_host._event_refresh``) forced every inner cache on every wake: the
counts, the remote-links projection and every done-check verdict were recomputed whenever any watched file was
written. The hub's sources are written about five times a second, mostly bookkeeping no reader selects (dispatch
stamps, heartbeats, sync clocks), so the full read ran back to back: 51% of a core and 17,000 context switches a
second in the ``kanban-annotation`` thread (t_01a204267fa8a30d).

A wake says only that some watched file moved. Before the read, the holder takes one key per part over exactly the
inputs that part reads, and forces a part only when its key differs from the one its last forced read answered:

- ``counts``: the boards' and sync shadows' content revisions, the origin ledger, and the plain inputs below;
- ``links``: the inputs the links cache keys on (``kanban_session_activity._links_fingerprint``): the boards'
  content revisions (the hub's tasks/gateway content signature among them) and the origin ledger. The plain inputs
  are left out on purpose: state.db (alias tips), config and the hub database's raw stat/WAL churn move every few
  seconds and that cache bounds them by its TTL instead. Keying links on them forced a
  full ledger projection under ``_links_lock`` on nearly every wake (91% of a hub backend's uptime in annotation
  refreshes, 2026-11-23), and every strict links reader, the Desktop action queue among them, queued behind it;
- ``attention``: the boards' content revisions and the files the done checks declared (a declared board or sync
  replica by its content revision, never its stat).

Plain inputs are everything else the holder watches for the scope (state.db, the hub database, config files, the
mutation cue), keyed by ``kanban_annotation_watch.signature`` (stat, header change counter, WAL-index header). A
part that is not forced is served by its own cache under that cache's ordinary freshness rules, exactly as a
request poll is.

Every part is forced when no key can vouch for the inputs: a provider clock deadline came due (wall-time verdicts
move without any write), a board has no content revision and its file stat is not settled, a done check declared no
watch paths, a key could not be read, or the scope has no completed holder read yet. Waiting sources are not gated
here: they are called on every refresh and own their input memo.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)

COUNTS, LINKS, ATTENTION = "counts", "links", "attention"
ALL = frozenset({COUNTS, LINKS, ATTENTION})
_MAX_SCOPES = 64

# Per scope, the part keys the last completed holder read answered. Holder thread only.
_seen: dict[tuple[str, ...], dict[str, object]] = {}


def _signature(scope: tuple[str, ...], paths) -> str:
    """Change signal of ``paths``: SQLite-aware for files (sidecars and header counters), stat for the rest."""
    from hermes_cli import kanban_annotation_watch as watch

    paths = sorted({str(p) for p in paths})
    plain = [p for p in paths if os.path.isdir(p)]
    return watch.signature(scope, [p for p in paths if p not in plain], plain)


def _plain_key(scope: tuple[str, ...]) -> str:
    """Every watched input of ``scope`` that no content revision covers."""
    from hermes_cli import kanban_annotation_host as host
    from hermes_cli import kanban_session_activity as ksa

    dbs, files = host._watch_paths(scope)
    discovery, _dirs = host._discovery_paths()
    keyed = {str(p) for p in ksa._board_db_paths()} | {str(p) for p in ksa._replica_paths()}
    return _signature(scope, {*dbs, *files, *(str(p) for p in discovery)} - keyed)


def _replica_dir() -> str | None:
    """Where this host's sync replicas live; None when that cannot be told."""
    try:
        from hermes_cli import kanban_sync_client as ksc
        return str(ksc.shadow_path("x").parent)
    except Exception:  # health: allow BLE001 -- unknown layout: declared replicas stay keyed by their file stat
        return None


def _done_key(scope: tuple[str, ...], wall: float) -> object:
    """The done checks' declared inputs; unequal to everything while any check is undeclared.

    A declared board is left out: the attention key carries every board's content revision. A declared sync
    replica is keyed by its content revision as well. Both files are rewritten several times a second by
    bookkeeping (claims, heartbeats, pull stamps) that moves no row a reader selects, and a stat key over them
    would rerun every done check on each of those writes. A replica whose revision cannot vouch (no counter and a
    stat not yet settled, or unreadable) makes the key unequal to everything. Any other declared file is keyed by
    its change signature."""
    from hermes_cli import kanban_annotation_host as host
    from hermes_cli import kanban_session_activity as ksa

    paths: set[str] = set()
    for name, fn in sorted(ksa._done_checks.items()):
        declared = host._done_event_sources.get(name)
        if declared is None or declared[0] is not fn:
            return object()
        paths.update(str(p) for p in declared[1]())
    paths -= {str(p) for p in ksa._board_db_paths()}
    shadow_dir = _replica_dir()
    replicas = sorted(p for p in paths if p.endswith(".db") and os.path.dirname(p) == shadow_dir)
    content = tuple((p, ksa._projection_content_key(Path(p), ksa._revision.SHADOW_TABLES)) for p in replicas)
    if not ksa._revalidate.revalidatable(content, wall):
        return object()
    return (tuple(sorted(ksa._done_checks)), content, _signature(scope, paths - set(replicas)))


def keys(scope: tuple[str, ...]) -> dict[str, object] | None:
    """One key per part, read now in ``scope``'s context; None when the inputs cannot be vouched for."""
    from hermes_cli import kanban_session_activity as ksa

    wall = time.time()
    local = ksa._activity_fingerprint(include_shadows=False)
    shadows = ksa._shadow_fingerprint()
    if not ksa._revalidate.revalidatable(local + shadows, wall):
        return None
    ledger = ksa._origin_ledger_fingerprint()
    plain = _plain_key(scope)
    return {COUNTS: (local, shadows, ledger, plain), LINKS: (local, ledger),
            ATTENTION: (local, _done_key(scope, wall))}


def plan(scope: tuple[str, ...], *, clock: bool, renew: bool = False) -> tuple[frozenset, dict[str, object] | None]:
    """``(parts to force, keys)`` for a holder read of ``scope`` starting now. ``clock``: a provider deadline came
    due. ``renew``: the cached counts are old enough that the published copy needs a new read time. Pass ``keys``
    to :func:`done` once the read succeeded. Taken before the read, so a write racing it moves the next plan's
    key."""
    try:
        now = keys(scope)
    except Exception:  # health: allow BLE001 -- an unreadable key is a change: every part reads
        log.debug("kanban annotation part keys unavailable", exc_info=True)
        now = None
    before = _seen.get(scope)
    if clock or now is None or before is None:
        return ALL, now
    moved = {part for part in ALL if before.get(part) != now[part]}
    return frozenset(moved | {COUNTS} if renew else moved), now


def done(scope: tuple[str, ...], now: dict[str, object] | None) -> None:
    """The read planned with ``now`` completed: its forced parts answered those keys."""
    _seen.pop(scope, None)
    if now is not None:
        while len(_seen) >= _MAX_SCOPES:
            del _seen[next(iter(_seen))]
        _seen[scope] = now


def failed(scope: tuple[str, ...]) -> None:
    """A holder read of ``scope`` raised: nothing is vouched for."""
    _seen.pop(scope, None)


def reset() -> None:
    _seen.clear()
