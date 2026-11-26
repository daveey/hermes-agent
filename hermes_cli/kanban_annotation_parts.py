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
- ``attention``: an independent revision over its selected board tables plus complete semantic done-check
  declarations. Path-only database dependencies, missing declarations and provider errors force a fresh read;
  arbitrary plugin event or comment dependencies cannot be inferred from the built-in attention table set.

Plain inputs are everything else the holder watches for the scope (state.db, the hub database, config files, the
mutation cue), keyed by ``kanban_annotation_watch.signature`` (stat, header change counter, WAL-index header). A
part that is not forced is served by its own cache under that cache's ordinary freshness rules, exactly as a
request poll is.

Every part is forced when no key can vouch for the inputs: a provider clock deadline came due (wall-time verdicts
move without any write), a board has no content revision and its file stat is not settled, a done check declared no
watch paths, a key could not be read, or the scope has no completed holder read yet. Waiting sources are gated
as a group by their exact declared inputs, with detached holder-only output; ordinary request reads are unchanged.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)

COUNTS, LINKS, ATTENTION, WAITING = "counts", "links", "attention", "waiting"
ALL = frozenset({COUNTS, LINKS, ATTENTION, WAITING})
_MAX_SCOPES = 64

# Per scope, the part keys the last completed holder read answered. Holder thread only.
_seen: dict[tuple[str, ...], dict[str, object]] = {}
_waiting: dict[tuple[str, ...], tuple[tuple[str, frozenset[str]], ...]] = {}


def _waiting_key() -> object:
    """Only complete, portable declarations can certify a waiting answer."""
    from hermes_cli import kanban_annotation_host as host
    from hermes_cli import kanban_session_activity as ksa

    sources = dict(ksa._waiting_sources)
    values, complete = host._waiting_inputs(sources)
    # Code identity alone does not distinguish two closures registered from the same source.
    return (tuple(sorted(sources.items())), values) if complete else object()


def waiting(read):
    """Reuse detached waiting output only in the serialized holder pass.

    Requests keep their existing literal behavior. A holder clock, unknown key,
    replacement or failed projection forces the source again through plan().
    """
    from hermes_cli import kanban_session_activity as ksa

    scope = ksa._origin_scope()
    if getattr(ksa._revalidate.force_read, "scope", None) != scope:
        return read()
    if ksa._revalidate.forced(scope, WAITING) or scope not in _waiting:
        value = read()
        frozen = tuple((sid, frozenset(ids)) for sid, ids in value.items())
        while len(_waiting) >= _MAX_SCOPES:
            del _waiting[next(iter(_waiting))]
        _waiting[scope] = frozen
    return {sid: set(ids) for sid, ids in _waiting[scope]}


def _signature(scope: tuple[str, ...], paths) -> str:
    """Change signal of ``paths``: SQLite-aware for files (sidecars and header counters), stat for the rest."""
    from hermes_cli import kanban_annotation_watch as watch

    paths = sorted({str(p) for p in paths})
    plain = [p for p in paths if os.path.isdir(p)]
    return watch.signature(scope, [p for p in paths if p not in plain], plain)


def _plain_key(scope: tuple[str, ...]) -> object:
    """Non-board inputs, with state.db narrowed to canonical continuation edges."""
    from hermes_cli import kanban_annotation_host as host
    from hermes_cli import kanban_session_activity as ksa

    dbs, files = host._watch_paths(scope)
    discovery, _dirs = host._discovery_paths()
    keyed = {str(p) for p in ksa._board_db_paths()} | {str(p) for p in ksa._replica_paths()}
    hub = ksa._hub_db_path()
    if hub is not None:
        keyed.add(str(hub))  # covered by the transactional hub key, including literal metadata
    from hermes_cli import kanban_annotation_lineage as lineage
    from hermes_constants import get_hermes_home

    state = get_hermes_home() / 'state.db'
    return (_signature(scope, {*dbs, *files, *(str(p) for p in discovery)} - keyed - {str(state)}),
            lineage.key(state))


def _replica_dir() -> str | None:
    """Where this host's sync replicas live; None when that cannot be told."""
    try:
        from hermes_cli import kanban_sync_client as ksc
        return str(ksc.shadow_path("x").parent)
    except Exception:  # health: allow BLE001 -- unknown layout: declared replicas stay keyed by their file stat
        return None


def _done_key(scope: tuple[str, ...], wall: float) -> object:
    """Complete semantic declarations, or fresh fallback for unknown database inputs.

    File-only legacy declarations retain their literal change signatures; a path alone cannot certify which
    database columns an arbitrary plugin reads. Complete ``inputs`` callbacks take precedence over paths.
    """
    from hermes_cli import kanban_annotation_host as host
    from hermes_cli import kanban_annotation_done_inputs as declared_inputs
    from hermes_cli import kanban_session_activity as ksa

    checks = dict(ksa._done_checks)
    if any(name in declared_inputs._declared for name in checks):
        # Explicit complete dependencies supersede broad path signatures. A mixed,
        # missing, malformed or failed declaration cannot certify suppression.
        return declared_inputs.key(checks)
    paths: set[str] = set()
    for name, fn in sorted(checks.items()):
        declared = host._done_event_sources.get(name)
        if declared is None or declared[0] is not fn:
            return object()
        paths.update(str(p) for p in declared[1]())
    # Path-only providers may select arbitrary comments, heartbeat events or
    # uncovered tables. Only literal signatures cover that contract.
    if any(p.endswith('.db') for p in paths):
        return object()  # no complete semantic declaration: never suppress a DB-dependent check
    return (tuple(sorted(checks.items())), _signature(scope, paths))


def _part_fingerprint(part: str, *, shadows: bool = False) -> tuple:
    from hermes_cli import kanban_annotation_revision as revision
    from hermes_cli import kanban_session_activity as ksa

    paths = ksa._replica_paths() if shadows else ksa._board_db_paths()
    values = tuple((str(p), revision.content_key(p, part)) for p in paths)
    hub = None if shadows else ksa._hub_db_path()
    return values + ((ksa._hub_content_signature(hub),) if hub is not None else ())


class _Keys(dict):
    """The raw receipt is publication custody, not part of semantic equality."""

    receipt: object = None


def keys(scope: tuple[str, ...]) -> dict[str, object] | None:
    """One key per part, read now in ``scope``'s context; None when the inputs cannot be vouched for."""
    from hermes_cli import kanban_session_activity as ksa
    from hermes_cli import kanban_annotation_revision as revision

    wall = time.time()
    local = [(str(p), revision.content_keys(p)) for p in ksa._board_db_paths()]
    from hermes_cli import kanban_annotation_hub_key as hub_key_reader

    hub = ksa._hub_db_path()
    hub_value, receipt = hub_key_reader.capture(hub, wall) if hub is not None else (None, None)
    hub_key = (hub_value,) if hub is not None else ()
    raw_hub = (ksa._hub_content_signature(hub),) if hub is not None else ()
    fingerprints = {part: tuple((path, values[part]) for path, values in local)
                    + (hub_key if part == COUNTS else raw_hub)
                    for part in (COUNTS, LINKS, ATTENTION)}
    shadows = _part_fingerprint(COUNTS, shadows=True)
    ledger = ksa._origin_ledger_fingerprint()
    plain = _plain_key(scope)
    result = _Keys({COUNTS: (fingerprints[COUNTS], shadows, ledger, plain), LINKS: (fingerprints[LINKS], ledger),
                    ATTENTION: (fingerprints[ATTENTION], _done_key(scope, wall)), WAITING: _waiting_key()})
    result.receipt = receipt
    return result


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
    from hermes_cli import kanban_annotation_counts as counts

    counts.begin(scope, now)
    renew = renew or counts.needs_read(scope)
    before = _seen.get(scope)
    if clock or now is None or before is None:
        return ALL, now
    moved = {part for part in ALL if before.get(part) != now[part]}
    return frozenset(moved | {COUNTS} if renew else moved), now


def done(scope: tuple[str, ...], now: dict[str, object] | None) -> None:
    """The read planned with ``now`` completed: its forced parts answered those keys."""
    _seen.pop(scope, None)
    receipt = getattr(now, 'receipt', None)
    if receipt is not None and not receipt.valid():
        failed(scope)
        return  # a racing result cannot authorize heartbeat-normalized suppression
    if now is not None:
        while len(_seen) >= _MAX_SCOPES:
            del _seen[next(iter(_seen))]
        _seen[scope] = now


def failed(scope: tuple[str, ...]) -> None:
    """A holder read of ``scope`` raised: nothing is vouched for."""
    _seen.pop(scope, None)
    _waiting.pop(scope, None)
    from hermes_cli import kanban_annotation_counts as counts
    counts.failed(scope)


def measured(part: str, read):
    """Account inclusive part CPU without changing its cache policy or exception behavior.

    ``served`` means cache-eligible, NOT a cache hit: TTL expiry can still read. Counts includes its nested
    links work; do not add those counters together. Waiting uses its declared holder key.
    """
    from hermes_cli import kanban_session_activity as ksa
    from hermes_cli import loop_accounting as la

    mode = "forced" if ksa._revalidate.forced(ksa._origin_scope(), part) else "served"
    with la.pass_(f"annotation.part.{part}.{mode}"):
        return read()


def reset() -> None:
    _seen.clear()
    _waiting.clear()
    from hermes_cli import kanban_annotation_counts as counts
    counts.reset()
