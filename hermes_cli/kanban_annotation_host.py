"""One session-list annotation read per host: publish it under its full inputs, adopt it elsewhere. [daveey]

Every web-server process (each Desktop ``serve``, the dashboard) and the gateway computed the same annotation parts
for the same scope: ``kanban_session_activity._refresh_annotation_locked`` was 13.3% of a steady hub serve's samples,
10.8% of the gateway's and 12.2% of a fresh serve's (design t_01a1d45d75851804 §2c, review item R9).

* **Publish.** A refresh takes the host fingerprint of its inputs BEFORE it reads (:func:`prepare`) and, when the read
  answered exactly those inputs, writes ``(scope, fingerprint, read_at, parts)`` beside the restart copy
  (``kanban_annotation_disk.save_host``). ``read_at`` is when the counts were really read, never later.
* **Fingerprint** (:func:`fingerprint`): the scope; the boards', hub replica's and sync shadows' content keys
  (``_activity_fingerprint``, the same inputs ``kanban_session_activity_revalidate.unchanged_inputs`` compares),
  portable across processes (a stat key keeps the stat, drops its process-local connection) and ``revalidatable``
  (no unsettled stat, no unreadable board); the core sha; each registered done check's and waiting source's code
  identity (its source files' stats when first seen in this process, so a process still running older plugin code
  never matches a newer one); and each waiting source's declared inputs (``register_waiting_source(..., inputs=)``,
  e.g. the chat-closeout ledger's committed revision). Anything it cannot vouch for makes it None: never adopted.
* **Adopt** (:func:`adopt`): before refreshing, a process adopts the copy only on an exact fingerprint match and
  ``read_at`` younger than ``_COUNTS_MAX_AGE_S`` (the bound a process's own unchanged-inputs reuse already has).
  When any waiting source declares no inputs, the copy's counts are adopted but every waiting source is re-run here,
  so a source the fingerprint cannot see is never served from another process.
* **Holder** (lease ``kanban-annotation``, ``host_loop_lease``): one process per home runs a demand-driven refresher
  for every scope it can reproduce that any process polled (``<scope>.demand`` touched) in the last
  ``DEMAND_WINDOW_S``, so the copy is current when the others poll. It reads each scope's demand inside that scope's
  own context (a serve hosting several profiles keeps each profile's demand under its own home). It renews a copy
  with a real read (bypassing the counts cache) past ``STRICT_REFRESH_AGE_S`` while an exact caller polled the scope
  lately (``<scope>.strict-demand``), so a fresh ``read_at`` is published before such adopters drop the copy at
  ``_COUNTS_MAX_AGE_S``; with display demand only, past ``HOLDER_REFRESH_AGE_S``. Between, a scope costs one
  watch signature per tick (``kanban_annotation_watch``) until a file it reads moves or a mutation cue lands.
  A miss anywhere still refreshes locally as before:
  no added latency, and ``HERMES_ANNOTATION_HOST_COPY=0`` turns publishing and adopting off.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import math
import os
import threading
import time
import weakref
from pathlib import Path
from typing import Any

from hermes_cli import kanban_annotation_disk as _disk
from hermes_cli import kanban_annotation_mutation as _mutation
from hermes_cli import kanban_annotation_parts as _parts

log = logging.getLogger("hermes_cli.kanban_session_activity")

KILL_SWITCH_ENV = "HERMES_ANNOTATION_HOST_COPY"  # health: allow HX002 -- the design's kill switch (§2c)
LEASE_NAME = "kanban-annotation"
DEMAND_WINDOW_S = 60.0
DEMAND_TOUCH_S = 10.0  # a poller re-touches its scope's demand file at most this often
HOLDER_INTERVAL_S = 2.0
HOLDER_MOVED_FLOOR_S = 10.0
DISPLAY_MAX_AGE_S = 12.0
# [daveey] Due-time fallback: the holder re-reads an unchanged scope this long after its copy's read (was 20 s).
# Between, a scope wakes only when a file it reads changes (``watch_signature``) or a mutation cue lands
# (t_01a1db1387f61a98 phase 2, owner decision A: the 20 s renewal of 8 scopes was ~11% of a core of identical reads).
# Strict adopters still need an exact copy younger than ``_COUNTS_MAX_AGE_S``; past it they read for themselves.
HOLDER_REFRESH_AGE_S = 60.0
# While an exact caller polls the scope, renew every 20 s as before: it adopts only copies younger than 30 s
# (``_COUNTS_MAX_AGE_S``) and otherwise reads for itself on the poll path (design t01a1d563 C16 / review R10).
STRICT_REFRESH_AGE_S = 20.0
# A holder-verified copy whose watched files have not moved since the verification stays displayable this long
# (one fallback plus slack); any watched move falls back to ``DISPLAY_MAX_AGE_S`` from the last verification.
DISPLAY_WATCH_MAX_AGE_S = HOLDER_REFRESH_AGE_S + 15.0
NARROW_CONFIRM_S = 6.0  # an unchanged narrow key re-stamps the copy's display ``verified_at`` this often
_WATCH_PATHS_TTL_S = HOLDER_MOVED_FLOOR_S  # a new board/shadow file is picked up within this (plus the fallback)
_VERSION = 1
_UNDECLARED = "undeclared"
_holder_ctx = threading.local()  # .active: this thread is the holder (only it pays for a narrow key)


class _Unportable(Exception):
    """A fingerprint input that means nothing in another process (a connection, an unreadable board)."""


def enabled() -> bool:
    return _disk.enabled() and os.environ.get(KILL_SWITCH_ENV, "").strip() != "0"


# -- waiting-source inputs ---------------------------------------------------------------------------------------

# name -> (fn, inputs): ``inputs()`` returns a JSON-able key that changes whenever ``fn()`` could answer differently.
_declared: dict[str, tuple[Any, Any]] = {}


def declare_inputs(name: str, fn, inputs) -> None:
    """Called by ``register_waiting_source``: ``inputs`` belongs to exactly this ``fn`` (a replacement undeclares)."""
    if fn is None or inputs is None:
        _declared.pop(name, None)
    else:
        _declared[name] = (fn, inputs)


def _waiting_inputs(sources: dict[str, Any]) -> tuple[list, bool]:
    """``([(name, code id, inputs or "undeclared")...], all declared)`` for the registered waiting sources."""
    out, complete = [], True
    for name in sorted(sources):
        fn = sources[name]
        declared = _declared.get(name)
        if declared is not None and declared[0] is fn:
            out.append([name, _code_id(fn), _portable(declared[1]())])
        else:
            out.append([name, _code_id(fn), _UNDECLARED])
            complete = False
    return out, complete


# Event coverage is distinct from fingerprint inputs: obtaining a fingerprint may
# read SQL, whereas these callbacks must only enumerate paths / retained deadlines.
_event_sources: dict[str, tuple[Any, Any, Any]] = {}
_done_event_sources: dict[str, tuple[Any, Any, Any]] = {}
_event_warned: set[tuple[str, str]] = set()


def declare_events(name, fn, watch_paths, next_deadline, *, done_check=False) -> None:
    registry = _done_event_sources if done_check else _event_sources
    if fn is None or watch_paths is None:
        registry.pop(name, None)
    else:
        registry[name] = (fn, watch_paths, next_deadline)


def register_waiting_source(name: str, fn, inputs=None, *, watch_paths=None, next_deadline=None) -> None:
    """Register a waiting provider; ``fn=None`` removes it.

    ``inputs()`` is its exact JSON cache key. ``watch_paths()`` declares every file
    dependency (SQLite sidecars are added by the holder); ``next_deadline()`` returns
    its next absolute wall-clock transition or None. Event callbacks must not read
    databases. Undeclared providers retain a logged, bounded 60-second fallback.
    """
    from hermes_cli import kanban_session_activity as activity
    declare_inputs(name, fn, inputs)
    declare_events(name, fn, watch_paths, next_deadline)
    if fn is None:
        activity._waiting_sources.pop(name, None)
    else:
        activity._waiting_sources[name] = fn
    wake_holder()


def waiting_events(now: float) -> tuple[set[Path], float | None]:
    """Paths plus earliest absolute wall deadline; undeclared sources stay bounded.

    An empty declared path list explicitly means a clock-only/pure provider.
    None from next_deadline means no pending clock transition, not unknown inputs.
    """
    from hermes_cli import kanban_session_activity as _ksa

    paths: set[Path] = set()
    deadlines: list[float] = []
    providers = [('waiting source', name, fn, _event_sources.get(name))
                 for name, fn in list(_ksa._waiting_sources.items())]
    providers += [('done check', name, fn, _done_event_sources.get(name))
                  for name, fn in list(_ksa._done_checks.items())]
    for kind, name, fn, declaration in providers:
        if declaration is None or declaration[0] is not fn:
            if (kind, name) not in _event_warned:
                log.warning("annotation %s %s has no watch_paths; using bounded 60s refresh", kind, name)
                _event_warned.add((kind, name))
            deadlines.append(now + HOLDER_REFRESH_AGE_S)
            continue
        try:
            paths.update(Path(p) for p in declaration[1]())
            due = declaration[2]() if declaration[2] is not None else None
            if due is not None:
                due = float(due)
                deadlines.append(due if math.isfinite(due) and due > now else now + HOLDER_INTERVAL_S)
        except Exception:  # health: allow BLE001 -- plugin failure retains bounded recovery
            log.warning("annotation %s %s event declaration failed", kind, name, exc_info=True)
            deadlines.append(now + HOLDER_INTERVAL_S)
    return paths, min(deadlines, default=None)


# -- code identity ------------------------------------------------------------------------------------------------

_code_ids: weakref.WeakKeyDictionary[Any, Any] = weakref.WeakKeyDictionary()
_core: list = []


def core_sha() -> str:
    """The core code this process runs (read once). Raises when unknown: no identity, no adoption."""
    if not _core:
        from hermes_cli.version_info import get_code_identity

        sha = get_code_identity().get("sha")
        _core.append(sha if isinstance(sha, str) and sha else None)
    if _core[0] is None:
        raise _Unportable("core code identity unknown")
    return _core[0]


def _core_root() -> str:
    from hermes_cli._startup_fast import project_root_str

    return os.path.realpath(project_root_str()) + os.sep


def _package_root(path: Path) -> Path:
    """The outermost package directory holding ``path`` (a plugin's own tree), else the file itself."""
    root, parent = path, path.parent
    while (parent / "__init__.py").is_file() and parent.parent != parent:
        root, parent = parent, parent.parent
    return root


def _code_id(fn) -> Any:
    """``"core"`` for core code (the core sha covers it), else the stats of its package's source files as this
    process first saw them. Raises for a callable whose source cannot be located."""
    try:
        cached = _code_ids.get(fn)
    except TypeError:  # not weak-referenceable
        cached = None
    if cached is not None:
        return cached
    filename = getattr(getattr(fn, "__code__", None), "co_filename", None)
    if not filename or not os.path.isfile(filename):
        raise _Unportable(f"no source file for {fn!r}")
    real = os.path.realpath(filename)
    if real.startswith(_core_root()):  # core and its bundled plugins: the core sha covers them
        ident: Any = "core"
    else:
        root = _package_root(Path(real))
        files = sorted(root.rglob("*.py")) if root.is_dir() else [root]
        ident = [str(root)]
        for f in files[:500]:
            st = f.stat()
            ident.append([str(f.relative_to(root)) if root.is_dir() else f.name, st.st_size, st.st_mtime_ns])
    try:
        _code_ids[fn] = ident
    except TypeError:
        pass
    return ident


# -- fingerprint --------------------------------------------------------------------------------------------------

def _portable(value: Any) -> Any:
    """``value`` as plain JSON data comparable across processes. A board stat key keeps its stat and drops the
    connection/version only this process can read; any other non-plain value raises ``_Unportable``."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, (tuple, list)):
        if value[:1] == ("stat",) or (isinstance(value, list) and value[:1] == ["stat"]):
            if len(value) != 4:  # ("stat", stat, object()): an unreadable board is unknown, never unchanged
                raise _Unportable("unreadable board")
            return ["stat", _portable(value[1])]
        return [_portable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _portable(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    raise _Unportable(type(value).__name__)


def fingerprint(scope: tuple[str, ...], full: tuple, wall: float) -> tuple[str, bool] | None:
    """``(digest, every waiting source declared)`` of everything ``scope``'s annotation reads, with ``full`` the
    activity fingerprint taken at ``wall``; None when any input can't be vouched for across processes."""
    from hermes_cli import kanban_session_activity as _ksa

    try:
        if not _ksa._revalidate.revalidatable(full, wall):
            return None
        waiting, complete = _waiting_inputs(dict(_ksa._waiting_sources))
        body = [_VERSION, list(scope), core_sha(), _portable(full),
                [[name, _code_id(fn)] for name, fn in sorted(_ksa._done_checks.items())], waiting]
        digest = hashlib.sha256(json.dumps(body, separators=(",", ":"), sort_keys=True).encode()).hexdigest()
        return digest, complete
    except Exception:  # health: allow BLE001 -- an unknown input is no fingerprint; the caller reads locally
        log.debug("kanban annotation host fingerprint unavailable", exc_info=True)
        return None


def _inputs_now(scope: tuple[str, ...]) -> tuple[tuple, tuple, float, tuple[str, bool] | None]:
    """``(local, full, wall, fingerprint)`` read now (or the pinned pair a revalidation already took)."""
    from hermes_cli import kanban_session_activity as _ksa

    wall = time.time()
    local = _ksa._activity_fingerprint(include_shadows=False)
    pinned = getattr(_ksa._revalidate.inputs_pin, "fingerprints", None)
    full = pinned[1] if pinned is not None and pinned[1] is not None else local + _ksa._shadow_fingerprint()
    return local, full, wall, fingerprint(scope, full, wall)


def _narrow_now(scope: tuple[str, ...], full: tuple) -> str | None:
    """The display/holder-only narrow key (``kanban_annotation_narrow``) for the activity fingerprint ``full``;
    None (= moved) whenever it is unknown. Never used to certify an exact adoption. [daveey] It keys the waiting
    sources' code identity, not their declared inputs, so it never runs a waiting-input probe."""
    from hermes_cli import kanban_annotation_narrow as _narrow

    try:
        waiting = waiting_identity()[0]
    except Exception:  # health: allow BLE001 -- unknown code identity is no key (= moved)
        log.debug("kanban annotation waiting identity unavailable", exc_info=True)
        return None
    return _narrow.key(scope, full, waiting)


def _activity_now() -> tuple[tuple, float]:
    """``(full, wall)``: the activity fingerprint with shadows (or the pinned one), without waiting inputs."""
    from hermes_cli import kanban_session_activity as _ksa

    wall = time.time()
    pinned = getattr(_ksa._revalidate.inputs_pin, "fingerprints", None)
    if pinned is not None and pinned[1] is not None:
        return pinned[1], wall
    return _ksa._activity_fingerprint(include_shadows=False) + _ksa._shadow_fingerprint(), wall


# -- watch signature (holder wake gate, display trust) -------------------------------------------------------------

_paths_memo: dict[tuple[str, ...], tuple[float, list[str], list[str]]] = {}


def _watch_paths(scope: tuple[str, ...]) -> tuple[list[str], list[str]]:
    """``(sqlite files, plain files)`` ``scope``'s annotation and narrow key read, resolved in the current (scope's)
    context; the enumeration (board globs, sync config) is memoised for ``_WATCH_PATHS_TTL_S``."""
    from hermes_cli import kanban_annotation_narrow as _narrow
    from hermes_cli import kanban_session_activity as _ksa
    from hermes_constants import get_hermes_home

    now = time.monotonic()
    memo = _paths_memo.get(scope)
    if memo is not None and 0 <= memo[0] - now <= _WATCH_PATHS_TTL_S:
        return memo[1], memo[2]
    hub = _ksa._hub_db_path()
    dbs = ([str(hub)] if hub is not None else []) + [str(p) for p in _ksa._board_db_paths()]
    dbs += [str(p) for p in _ksa._replica_paths()]
    dbs += [str(get_hermes_home() / "state.db"), str(_narrow._ledger_path())]
    files = [str(_disk._path(scope).with_suffix(".mutation.json"))]
    while len(_paths_memo) >= 32:
        del _paths_memo[next(iter(_paths_memo))]
    _paths_memo[scope] = (now + _WATCH_PATHS_TTL_S, dbs, files)
    return dbs, files


def watch_signature(scope: tuple[str, ...]) -> str | None:
    """``kanban_annotation_watch.signature`` of ``scope``'s inputs; None (= moved) when anything is unreadable."""
    from hermes_cli import kanban_annotation_watch as _watch

    try:
        dbs, files = _watch_paths(scope)
        return _watch.signature(scope, dbs, files)
    except Exception:  # health: allow BLE001 -- an unknown signature is a change: the holder probes as before
        log.debug("kanban annotation watch signature unavailable", exc_info=True)
        return None


# -- the read -----------------------------------------------------------------------------------------------------

# Per origin scope, the last frozen annotation parts (``read_annotation_parts``).
frozen_parts: dict[tuple[str, ...], tuple] = {}


def read_annotation_parts() -> tuple:
    """``kanban_session_activity._read_annotation_parts``: one read of the scope's counts, waiting sources and
    attention, frozen. Every helper is looked up on that module (tests replace them there)."""
    from hermes_cli import kanban_session_activity as _ksa

    from hermes_cli import kanban_annotation_counts as counts

    snapshot = _parts.measured(_parts.COUNTS, lambda: counts.read(lambda: _ksa._all_counts(include_snapshot=True)))
    active, blocked, unseen, remote_stale, waiting, parked, held, stalled, remote_keys = snapshot[:9]
    execution = getattr(snapshot, "execution", {})
    extra = _parts.measured(_parts.WAITING, lambda: _parts.waiting(_ksa._extra_waiting))
    try:
        attention = _parts.measured(_parts.ATTENTION, _ksa.attention_task_counts)
    except Exception:
        log.debug("kanban attention unavailable", exc_info=True)
        attention = {}
    raw = (active, blocked, unseen, remote_stale, waiting, parked, held, stalled, remote_keys, extra, attention, execution)
    # Nothing moved since the last read of this scope: return that read's frozen copy instead of freezing an equal
    # one. Safe: frozen parts are never mutated (every poll already shares one), and ``==`` per part (sets and
    # frozensets compare by members) proves the copy is exactly what freezing ``raw`` would build. The same object
    # also lets ``_annotation_copy.published`` skip its deep comparison.
    scope = _ksa._origin_scope()
    previous = frozen_parts.get(scope)
    if previous is not None and len(previous) == len(raw) and all(old is new or old == new for old, new in zip(previous, raw)):
        return previous
    frozen = _ksa._identity.freeze_annotation(raw)
    frozen_parts[scope] = frozen
    return frozen


def refresh_locked(scope: tuple[str, ...], read_at: float | None = None) -> tuple:
    """Read and publish under the caller's annotation lock, retaining count-source age."""
    from hermes_cli import kanban_annotation_counts as holder_counts
    from hermes_cli import kanban_session_activity as ksa

    ksa._annotate_flight_scope, ksa._annotate_flight_result = scope, None
    read_at = ksa.time.time() if read_at is None else read_at
    prepared = prepare(scope)
    try:
        if getattr(ksa._revalidate.force_read, 'scope', None) != scope:
            holder_counts.clear_use()
        try:
            value = ksa._read_annotation_parts()
        finally:
            finish(scope)
        ksa._bounded.record(scope, value, holder_counts.observed_at(read_at))
        ksa._last_annotation[scope] = value
        ksa._annotate_flight_result = value
        if not holder_counts.reused():
            ksa._annotation_copy.published(scope, value)
        publish(scope, prepared, value)
        return value
    finally:
        holder_counts.clear_use()


# -- adopt --------------------------------------------------------------------------------------------------------

def waiting_identity() -> tuple[list, bool]:
    """``([[name, code id, inputs code id or "undeclared"]...], all declared)`` of the registered waiting sources:
    code/registration identity only, never a waiting input probe."""
    from hermes_cli import kanban_session_activity as _ksa

    sources = dict(_ksa._waiting_sources)
    waiting, complete = [], True
    for name, fn in sorted(sources.items()):
        declared = _declared.get(name)
        has_inputs = declared is not None and declared[0] is fn
        complete = complete and has_inputs
        waiting.append([name, _code_id(fn), _code_id(declared[1]) if declared is not None and has_inputs else _UNDECLARED])
    return waiting, complete


def _display_identity(scope: tuple[str, ...]) -> tuple[str, bool]:
    """Code/registration identity only: never runs a waiting input probe."""
    from hermes_cli import kanban_session_activity as _ksa

    waiting, complete = waiting_identity()
    body = [_VERSION, list(scope), core_sha(),
            [[name, _code_id(fn)] for name, fn in sorted(_ksa._done_checks.items())], waiting]
    return hashlib.sha256(json.dumps(body, separators=(",", ":"), sort_keys=True).encode()).hexdigest(), complete


def _display_read(scope: tuple[str, ...]) -> tuple | None:
    """Read and validate an immutable display copy, without running sources or changing the annotation cache."""
    if not enabled():
        return None
    try:
        identity, complete = _display_identity(scope)
        token = _mutation.current(scope)
        header: dict = {}
        copy = _disk.load_host(scope, shared=True, display_identity=identity, mutation_token=token,
                               header_out=header)
        now = time.time()
        if copy is None or _mutation.current(scope) != token:
            return None
        verified = header.get("verified_at")
        # Display age runs from the holder's last narrow-key confirmation (never ahead of now, never before read).
        seen = max(copy[1], float(verified)) if isinstance(verified, (int, float)) and verified <= now else copy[1]
        if not 0 <= now - seen <= DISPLAY_MAX_AGE_S and not (
                # [daveey] Nothing the holder watches moved since it verified this copy: trust it to the fallback.
                0 <= now - seen <= DISPLAY_WATCH_MAX_AGE_S and header.get("narrow_key")
                and header.get("watch_sig") and header.get("watch_sig") == watch_signature(scope)):
            return None
        parts = copy[2]
    except Exception:  # health: allow BLE001 -- unknown code/copy fails closed to the normal read
        log.debug("kanban annotation display copy not adopted", exc_info=True)
        return None
    return parts, copy[1], complete


def adopt_display(scope: tuple[str, ...]) -> tuple | None:
    """Caller holds the annotation lock; adopt a validated copy and rerun undeclared sources."""
    from hermes_cli import kanban_session_activity as _ksa

    read = _display_read(scope)
    if read is None:
        return None
    parts, observed_at, complete = read
    if not complete:
        extra = {sid: frozenset(ids) for sid, ids in _ksa._extra_waiting().items()}
        parts = parts[:9] + (extra,) + parts[10:]
    _ksa._bounded.record(scope, parts, observed_at)
    _ksa._last_annotation[scope] = parts
    return parts


def peek_display(scope: tuple[str, ...], previous: tuple | None) -> tuple | None:
    """Prefer a newer host copy even during a slow refresh, without writing its cache or running sources."""
    from hermes_cli import kanban_annotation_refresh as bounded

    read = _display_read(scope)
    if read is None or not read[2]:
        return previous
    parts, observed_at, _complete = read
    # Restart seeds carry provenance in the disk cache, not the bounded-read registry.
    previous_at = _disk.observed_at(previous)
    if previous_at is None:
        previous_at = bounded.read_at(previous)
    if previous_at is not None and previous_at >= observed_at:
        return previous
    return bounded.ObservedParts(parts, observed_at)



def adopt(scope: tuple[str, ...]) -> tuple | None:
    """Caller holds the annotation refresh lock. The published parts for ``scope`` when its fingerprint is exactly
    this process's and its read is younger than ``_COUNTS_MAX_AGE_S``; recorded as this process's last read (dated
    by the copy's own ``read_at``). None otherwise: the caller refreshes as before."""
    from hermes_cli import kanban_session_activity as _ksa

    if not enabled():
        return None
    try:
        copy = _disk.load_host(scope, shared=True)
        if copy is None or time.time() - copy[1] >= _ksa._COUNTS_MAX_AGE_S:
            return None
        _local, _full, _wall, fp = _inputs_now(scope)
        if fp is None or fp[0] != copy[0]:
            return None
        parts = copy[2]
    except Exception:  # health: allow BLE001 -- adoption is only a shortcut; the refresh reports a real read error
        log.debug("kanban annotation host copy not adopted", exc_info=True)
        return None
    if not fp[1]:  # a waiting source the fingerprint cannot see: run them all here (raises like a refresh would)
        extra = {sid: frozenset(ids) for sid, ids in _ksa._extra_waiting().items()}
        parts = parts[:9] + (extra,) + parts[10:]
    _ksa._bounded.record(scope, parts, copy[1])
    _ksa._last_annotation[scope] = parts
    return parts


# -- publish ------------------------------------------------------------------------------------------------------

_pin_set = threading.local()


def prepare(scope: tuple[str, ...]) -> tuple | None:
    """Before a refresh reads: the inputs it is about to read, pinned so the read keys on exactly them (the read
    would take the same pair microseconds later). Pass the result to :func:`publish` after the read."""
    from hermes_cli import kanban_session_activity as _ksa

    if not enabled():
        return None
    try:
        # [daveey] Taken before any input read: a write racing the read moves it, so the holder probes again.
        watch = watch_signature(scope) if getattr(_holder_ctx, "active", False) else None
        # Only a forced read can acknowledge the cue: an ordinary poll may reuse
        # counts that predate a closeout mutation outside the board fingerprint.
        token = _mutation.current(scope) if getattr(_ksa._revalidate.force_read, "scope", None) == scope else None
        local, full, wall, fp = _inputs_now(scope)
        narrow = _narrow_now(scope, full) if fp is not None and getattr(_holder_ctx, "active", False) else None
    except Exception:  # health: allow BLE001 -- the read itself reports a board error
        log.debug("kanban annotation host inputs unavailable", exc_info=True)
        return None
    try:
        display_identity, _complete = _display_identity(scope)
    except Exception:  # health: allow BLE001 -- unavailable display identity must not disable exact publication
        display_identity = None
    if getattr(_ksa._revalidate.inputs_pin, "fingerprints", None) is None:
        _ksa._revalidate.inputs_pin.fingerprints = (local, full)
        _pin_set.scope = scope
    return (full, wall, fp, token, display_identity, narrow, watch if narrow else None)


def finish(scope: tuple[str, ...]) -> None:
    """Drop the pin :func:`prepare` set (never one a caller up the stack owns)."""
    from hermes_cli import kanban_session_activity as _ksa

    if getattr(_pin_set, "scope", None) == scope:
        _ksa._revalidate.inputs_pin.fingerprints = None
        _pin_set.scope = None


def publish(scope: tuple[str, ...], prepared: tuple | None, parts: tuple) -> bool:
    """Write the host copy when the read answered exactly ``prepared``'s inputs: its counts are cached under that
    fingerprint and revalidatable. ``read_at`` = when those counts were read (never the later wall time)."""
    from hermes_cli import kanban_session_activity as _ksa

    from hermes_cli import kanban_annotation_counts as counts

    if counts.reused():
        return False  # retained counts cannot acknowledge a new raw fingerprint or mutation token
    if prepared is None or prepared[2] is None:
        return False
    full, wall, (digest, _complete), token, display_identity, narrow, watch = prepared
    with _ksa._cache_lock:
        if (_ksa._cache is None or _ksa._cache_scope != scope or _ksa._cache_fingerprint != full
                or not _ksa._cache_revalidatable):
            return False
        counts_at = time.time() - max(0.0, time.monotonic() - _ksa._cache_read_at)
    return _disk.save_host(scope, digest, min(wall, counts_at), parts, mutation_token=token,
                           display_identity=display_identity, narrow_key=narrow, watch_sig=watch)


# -- holder -------------------------------------------------------------------------------------------------------

_known: dict[tuple[str, ...], contextvars.Context] = {}
_touched: dict[tuple[tuple[str, ...], bool], float] = {}
_holder: list = []
# [daveey] Per scope, ``(watch signature, due wall time, copy read_at or None)``: the holder sleeps on the scope while
# the signature is unchanged and the due time (floor end, or the renewal of the copy read at ``read_at``) has not
# come. ``_seen``: the last signature observed.
_gates: dict[tuple[str, ...], tuple[str, float, float | None]] = {}
_seen: dict[tuple[str, ...], str | None] = {}
_wake = threading.Event()  # compatibility for direct legacy holder callers
_native_events: list = []
_clock_deadlines: dict[tuple[str, ...], list[float]] = {}
_event_ready: set[tuple[str, ...]] = set()
# A source notification can precede the visibility of the commit it announces:
# SQLite WAL appends (and with synchronous=FULL fsyncs) the -wal frames, then
# publishes the commit through the memory-mapped -shm index, which emits no
# event. A read answering a source invalidation is therefore confirmed by
# bounded one-shot re-reads; those confirmations never schedule further ones,
# so an idle source stays unread.
SETTLE_S = (0.25, HOLDER_INTERVAL_S)
_unsettled: set[tuple[str, ...]] = set()
_settle_deadlines: dict[tuple[str, ...], tuple[float, ...]] = {}
_coverage_stale: set[bool] = set()  # non-empty: the last coverage install failed


def note_deadline(due: float | None) -> None:
    """Record a clock boundary while projecting already-read source rows."""
    pending = getattr(_holder_ctx, 'deadlines', None)
    if pending is not None and due is not None and due > time.time():
        pending.append(due)


def replace_deadlines(owner: tuple, values) -> None:
    """Stage a successful source's complete current clocks; never replace a peer's."""
    from hermes_cli import kanban_session_activity as _ksa
    from hermes_cli.kanban_annotation_deadlines import Plan

    pending = getattr(_holder_ctx, 'deadlines', None)
    if isinstance(pending, Plan):
        # Complete input probes may project rows while planning, without
        # publishing any output. Only the subsequent real projection owns
        # replacement; a probe must not borrow a newer clock for old output.
        if getattr(_ksa._revalidate.force_read, 'scope', None) == _ksa._origin_scope():
            pending.replace(owner, values, time.time())
    elif pending is not None:  # legacy holder: retain conservative unowned clocks
        for due in values:
            note_deadline(due)


def wake_holder() -> None:
    """Wake native demand/registration changes without reading any source."""
    _wake.set()
    for events in tuple(_native_events):
        events.wake()


def _discovery_paths() -> tuple[set[Path], set[Path]]:
    """Watch creation as well as existing inputs, using the readers' roots."""
    from hermes_constants import get_hermes_home
    from hermes_cli import kanban_db, kanban_sync_client

    home = get_hermes_home()
    boards = kanban_db.boards_root()
    hub = Path(os.environ.get('HERMES_HUB_STATE') or str(Path.home() / '.local/state/hermes-hub'))
    mail = Path(os.environ.get('HERMES_MAIL_CONFIG') or str(Path.home() / '.config/hermes-mail/config.json'))
    files = {kanban_db.kanban_home() / 'kanban.db', hub / 'hub.db',
             home / 'config.yaml', mail, kanban_db.current_board_path()}
    directories = {boards, kanban_sync_client.shadow_path('default').parent}
    if boards.is_dir():
        directories.update(p for p in boards.iterdir() if p.is_dir())
    return files, directories


def _scope_interested(scope: tuple[str, ...]) -> bool:
    """Read only demand-file metadata, never source DBs, in the scope's context."""
    from hermes_cli import kanban_session_activity as _ksa

    age = _disk.demand_age(scope)
    return age is not None and age <= DEMAND_WINDOW_S and _ksa._origin_scope() == scope


def _scope_retry(scope: tuple[str, ...]) -> float:
    from hermes_cli.kanban_annotation_deadlines import retry

    now = time.time()
    due = now + HOLDER_INTERVAL_S
    _clock_deadlines[scope] = retry(_clock_deadlines.get(scope, ()), due, now)
    return due


def _event_refresh(events) -> float | None:
    """Install all scope coverage before reading; return the next one-shot due time.

    Re-enumeration is event-driven, including absent source parents and discovery
    directories. No lease beat enters this function.
    """
    from hermes_cli import kanban_session_activity as _ksa

    # Demand files carry interest, not source invalidation. Keep the existing
    # descriptor (and queued source events) when all scopes are already current.
    due = min((t for times in _clock_deadlines.values() for t in times), default=None)
    # A failed/busy scope has an explicit retry deadline, not a valid result.
    # Demand churn must not bypass that backoff; a source event still does.
    settled = _event_ready | {scope for scope, times in _clock_deadlines.items() if times}
    if getattr(events, 'control_only', False):
        # An unrequested scope is settled for this wake, not certified. Recheck
        # its demand metadata each control event so first demand still activates it.
        settled |= {scope for scope, ctx in list(_known.items())
                    if scope not in settled and not ctx.run(_scope_interested, scope)}
        if not _coverage_stale and set(_known) <= settled and (due is None or due > time.time()):
            return due
    else:
        _unsettled.update(_known)  # source/overflow/ownership/explicit wake
    # Lost notifications (overflow, ignored watch) say nothing about what moved: no part key vouches for the
    # inputs, so every part reads. Taken before ``replace`` resets the flag.
    lost = bool(getattr(events, 'coverage_lost', False)) and not getattr(events, 'control_only', False)
    files, directories, controls, scopes = set(), set(), set(), []
    deadlines = []
    for scope, ctx in list(_known.items()):
        def coverage():
            _paths_memo.pop(scope, None)
            dbs, plain = _watch_paths(scope)
            plugin, _ = waiting_events(time.time())
            discovery_files, discovery_dirs = _discovery_paths()
            demand_path = _disk._path(scope)
            return ({*(Path(p) for p in dbs + plain), *plugin, *discovery_files},
                    discovery_dirs,
                    {demand_path.with_suffix('.demand'), demand_path.with_suffix('.strict-demand')})
        try:
            scoped_files, scoped_dirs, scoped_controls = ctx.run(coverage)
        except Exception:  # health: allow BLE001 -- one profile's unavailable discovery must not starve peers
            log.warning('annotation scope coverage failed', exc_info=True)
            deadlines.append(_scope_retry(scope))
            continue
        files.update(scoped_files)
        directories.update(scoped_dirs)
        controls.update(scoped_controls)
        scopes.append((scope, ctx))
    try:
        events.replace(files, directories=directories, controls=controls)
    except BaseException:
        # The event that asked for this refresh is already drained and the
        # loop's retry arrives as a plain timeout: nothing stays certified, and
        # the next call reads whatever woke it.
        _event_ready.clear()
        _coverage_stale.add(True)
        raise
    _coverage_stale.clear()
    _event_ready.clear()
    for scope, ctx in scopes:
        def refresh_scope():
            if not _scope_interested(scope):
                _clock_deadlines.pop(scope, None)
                # A deadline dropped here may have come due unseen: the part keys vouch for nothing after it.
                _parts.failed(scope)
                return None
            from hermes_cli import kanban_annotation_counts as counts

            if not _ksa._annotate_refresh_lock.acquire(blocking=False):
                return _scope_retry(scope)
            try:
                # Native scheduling uses part keys, never the legacy narrow-key gate. Do not build its expensive
                # inventory in prepare or claim its extended display lifetime: publish the ordinary exact copy.
                # Unread owners and legacy clocks retain their boundaries. A
                # successful source may replace only its own superseded clocks.
                confirmations = _settle_deadlines.get(scope, ())
                started = time.time()
                from hermes_cli.kanban_annotation_deadlines import Plan

                pending = Plan(_clock_deadlines.get(scope, ()), confirmations, started)
                _holder_ctx.deadlines = pending
                # [daveey] Force only the parts whose own inputs moved (kanban_annotation_parts). A provider deadline
                # that came due is a wall-time transition no key sees: it forces them all. A settle confirmation is
                # not one: it re-takes the keys, which is what shows a commit the first read was too early for. The
                # counts are also read once their last read is ``STRICT_REFRESH_AGE_S`` old, so the published
                # ``read_at`` is renewed before exact adopters drop the copy at ``_COUNTS_MAX_AGE_S``.
                # The age is this scope's own: the active count slot belongs to whichever scope was read last.
                read_at = counts.read_at(scope)
                if read_at is None:
                    with _ksa._cache_lock:
                        read_at = _ksa._count_scopes.read_at_locked(scope)
                if lost:
                    _parts.failed(scope)
                clock = pending.expired
                renew = read_at is None or time.monotonic() - read_at >= STRICT_REFRESH_AGE_S
                forced, part_keys = _parts.plan(scope, clock=clock, renew=renew)
                _ksa._revalidate.force_read.scope = scope
                _ksa._revalidate.force_read.parts = forced
                _ksa._revalidate.force_read.content_only_boards = not (lost or clock or renew)
                # No part needs a read: avoid prepare/freeze/publication and ordinary inner TTLs.
                # Keep the deadline/settle path below: this hint can precede COMMIT visibility.
                if forced:
                    _ksa._refresh_annotation_locked(scope)
                _parts.done(scope, part_keys)
                _, due = waiting_events(time.time())
                note_deadline(due)
                now = time.time()
                if scope in _unsettled:
                    # Coalesce: a newer invalidation replaces pending confirmations.
                    _unsettled.discard(scope)
                    confirmations = tuple(now + delay for delay in SETTLE_S)
                else:
                    confirmations = tuple(t for t in confirmations if t > now)
                _settle_deadlines[scope] = confirmations
                _clock_deadlines[scope] = pending.finish(confirmations)
                _event_ready.add(scope)
                return min(_clock_deadlines[scope], default=None)
            except BaseException:
                _parts.failed(scope)
                raise
            finally:
                counts.clear_use()
                _holder_ctx.deadlines = None
                _ksa._revalidate.force_read.scope = None
                _ksa._revalidate.force_read.parts = None
                _ksa._revalidate.force_read.content_only_boards = False
                _ksa._annotate_refresh_lock.release()
        try:
            due = ctx.run(refresh_scope)
        except Exception:  # health: allow BLE001 -- failed scopes must not starve healthy scopes
            log.warning('annotation scope refresh failed', exc_info=True)
            due = _scope_retry(scope)
        if due is not None:
            deadlines.append(due)
    return min(deadlines, default=None)


def kick(scope: tuple[str, ...]) -> None:
    """Post-commit mutation cue; never acknowledge a cue that arrives during a read."""
    if not enabled():
        return
    demand(scope)
    _gates.pop(scope, None)
    try:
        _mutation.record(scope)
    except (OSError, ValueError):
        log.warning("kanban annotation mutation cue not recorded", exc_info=True)
    wake_holder()


def demand(scope: tuple[str, ...], *, strict: bool = False) -> None:
    """A poll of ``scope``: remember how to reproduce it here and touch its demand file (throttled). ``strict``: an
    exact caller; while one polls, the holder renews the copy every ``STRICT_REFRESH_AGE_S`` so it stays adoptable."""
    if not enabled():
        return
    now = time.monotonic()
    key = (scope, strict)
    if now - _touched.get(key, -1e9) < DEMAND_TOUCH_S:
        return
    _touched[key] = now
    new_scope = scope not in _known
    _known[scope] = contextvars.copy_context()
    _disk.touch_demand(scope)
    if strict:
        _disk.touch_demand(scope, strict=True)
    if new_scope:
        wake_holder()


def _renew_age(scope: tuple[str, ...]) -> float:
    """[daveey] The holder's due-time fallback for ``scope``: ``HOLDER_REFRESH_AGE_S``, or ``STRICT_REFRESH_AGE_S``
    while an exact caller (which adopts only copies younger than ``_COUNTS_MAX_AGE_S``) polled it lately."""
    age = _disk.demand_age(scope, strict=True)
    return STRICT_REFRESH_AGE_S if age is not None and age <= DEMAND_WINDOW_S else HOLDER_REFRESH_AGE_S


def _holder_pass() -> int:
    """Refresh every scope this process can reproduce that someone polled lately and whose copy is missing,
    another input's, or aging. A scope whose watched files have not moved since the holder last verified it, and
    whose due time has not come, costs a signature and nothing else. Returns how many it refreshed."""
    refreshed = 0
    for scope, ctx in list(_known.items()):
        try:
            if ctx.run(_asleep, scope):
                continue
            refreshed += bool(ctx.run(_holder_refresh, scope))
        except Exception:  # health: allow BLE001 -- one scope's failed read must not stop the others
            _gates.pop(scope, None)
            log.debug("kanban annotation holder refresh failed", exc_info=True)
    return refreshed


def _asleep(scope: tuple[str, ...]) -> bool:
    gate = _gates.get(scope)
    if gate is None:
        return False
    due = gate[1] if gate[2] is None else min(gate[1], gate[2] + _renew_age(scope))  # an exact caller may arrive
    if not 0 < due - time.time() <= HOLDER_REFRESH_AGE_S:  # due (or a clock jump): wake
        return False
    sig = watch_signature(scope)
    return sig is not None and sig == gate[0]


def _arm(scope: tuple[str, ...], sig: str | None, due: float, read_at: float | None = None) -> None:
    if sig is None:
        _gates.pop(scope, None)
    else:
        _gates[scope] = (sig, due, read_at)


def _holder_refresh(scope: tuple[str, ...]) -> bool:
    """Runs inside ``scope``'s own context: its demand file and copy live under that profile's home. One
    ``annotation.holder_refresh/<profile>`` pass (loop_accounting): a refresh is a key change, no refresh a skip."""
    from hermes_cli import loop_accounting
    with loop_accounting.pass_(f"annotation.holder_refresh/{scope[1] if len(scope) > 1 else '?'}") as acct:
        refreshed = _holder_refresh_scope(scope)
        acct.change(refreshed)
        if not refreshed:
            acct.skip()
        return refreshed


def _holder_refresh_scope(scope: tuple[str, ...]) -> bool:
    from hermes_cli import kanban_session_activity as _ksa

    age = _disk.demand_age(scope)
    if age is None or age > DEMAND_WINDOW_S or _ksa._origin_scope() != scope:
        _gates.pop(scope, None)
        return False  # nobody polled it lately, or this context no longer reproduces the scope
    sig = watch_signature(scope)  # before reading anything: a write racing this pass moves it
    seen, _seen[scope] = _seen.get(scope), sig
    _gates.pop(scope, None)
    copy = _disk.load_host(scope, shared=True, header_out=(header := {}))
    copy_age = time.time() - copy[1] if copy is not None else None
    renew = copy_age is not None and copy_age >= _renew_age(scope)
    kicked = _mutation.pending(scope)
    if copy is not None and not renew and not kicked:
        # [daveey] Inside the floor nothing refreshes either way: return before reading any input.
        if copy_age is not None and 0 <= copy_age < HOLDER_MOVED_FLOOR_S:
            _arm(scope, sig, copy[1] + HOLDER_MOVED_FLOOR_S)
            return False
        # Past the floor: refresh only for a substantive move (narrow key). An unchanged narrow key re-stamps the
        # display ``verified_at`` (header only) and sleeps until a watched file moves or the fallback renewal.
        # The narrow key needs no exact fingerprint (whose waiting-input probes cost more than it).
        published, narrow = header.get("narrow_key"), None
        if published:
            full, wall = _activity_now()
            narrow = _narrow_now(scope, full)
            if narrow == published:
                verified = header.get("verified_at")
                watched = sig is not None and header.get("watch_sig") == sig
                # Nothing watched moved since the last verification: nothing to re-stamp. Otherwise stamp every
                # NARROW_CONFIRM_S, and at once when a new signature held for a whole tick (the scope went quiet),
                # so a display then trusts the quiet copy to the fallback.
                if not watched and (not isinstance(verified, (int, float)) or wall - verified >= NARROW_CONFIRM_S
                                    or (sig is not None and sig == seen)):
                    watched = _disk.confirm_host(scope, published, wall, watch_sig=sig) and sig is not None
                if watched:
                    _arm(scope, sig, copy[1] + HOLDER_REFRESH_AGE_S, copy[1])
                return False
        if not published or narrow is None:  # no narrow verdict: the exact fingerprint decides
            _local, full, wall, fp = _inputs_now(scope)
            if fp is not None and fp[0] == copy[0]:
                return False
    if not _ksa._annotate_refresh_lock.acquire(blocking=False):
        return False  # a poll here is already reading (it publishes)
    # [daveey] An aging copy is renewed by a real read: the counts cache would answer the unchanged inputs with the
    # old read, republishing its ``read_at`` until the copy expires for every adopter at ``_COUNTS_MAX_AGE_S``.
    _ksa._revalidate.force_read.scope = scope if renew or kicked else None
    _holder_ctx.active = True
    try:
        _ksa._refresh_annotation_locked(scope)
    finally:
        _holder_ctx.active = False
        _ksa._revalidate.force_read.scope = None
        _ksa._annotate_refresh_lock.release()
    return True


_holder_lock = threading.RLock()
_holder_transfer_lock = threading.RLock()
_holder_suspended = False


def suspend_holder(*, timeout: float = 5.0) -> None:
    """Process-wide cutover seam: stop/join and prohibit a racing legacy start."""
    from hermes_cli.kanban_annotation_prewarm import suspend_annotation_prewarm

    global _holder_suspended
    with _holder_transfer_lock:
        # A warmup may request the holder lock; never hold it while joining.
        with _holder_lock:
            _holder_suspended = True
        suspend_annotation_prewarm(timeout=timeout)
        stop_holder(timeout=timeout)


def resume_holder(role: str) -> None:
    """Global rollback calls this once after every facts consumer unsubscribes."""
    from hermes_cli.kanban_annotation_prewarm import resume_annotation_prewarm

    global _holder_suspended
    with _holder_transfer_lock, _holder_lock:
        if not _holder_suspended:
            return
        stop_holder()
        resume_annotation_prewarm()
        _holder_suspended = False
        _start_holder(role)


def stop_holder(*, timeout: float = 5.0) -> None:
    """Drain the legacy owner before transferring projection ownership.

    Keep custody on timeout: a replacement must never start while an old reader
    can still publish. The holder itself closes watches and releases its lease.
    """
    with _holder_lock:
        if not _holder:
            return
        for _thread, stop, _lease in _holder:
            stop.set()
        wake_holder()
        deadline = time.monotonic() + timeout
        for thread, _stop, _lease in _holder:
            thread.join(max(0.0, deadline - time.monotonic()))
        if any(thread.is_alive() for thread, _stop, _lease in _holder):
            raise TimeoutError("legacy annotation holder did not drain")
        _holder.clear()


def start_holder(role: str) -> threading.Thread | None:
    with _holder_lock:
        if _holder_suspended:
            return None
        return _start_holder(role)


def _start_holder(role: str) -> threading.Thread | None:
    """Run the demand-driven refresher under the ``kanban-annotation`` host lease (once per process; never under
    the test harness)."""
    if _holder or os.environ.get("HERMES_TEST_ISOLATION") or not enabled():
        return None
    from agent.memory_provider import spawn_context_thread
    from hermes_cli import kanban_session_activity as _ksa
    from hermes_cli.host_loop_lease import HostLoopLease

    lease = HostLoopLease(LEASE_NAME, role, interval=HOLDER_INTERVAL_S)
    stop = threading.Event()
    try:  # this process's own scope, even before a poll reaches it here
        _known.setdefault(_ksa._origin_scope(), contextvars.copy_context())
    except Exception:  # health: allow BLE001 -- a scope polls register themselves too
        log.debug("kanban annotation holder: own scope unavailable", exc_info=True)

    from hermes_cli.annotation_file_events import FileEvents
    from hermes_cli.annotation_event_loop import holder_coalesce, run
    try:
        events = FileEvents()
    except (OSError, NotImplementedError):
        # Local request reads remain authoritative. Never disguise a timed poll
        # as native notification support on an unsupported platform.
        log.warning("annotation native holder unavailable; using request reads", exc_info=True)
        return None
    _native_events.append(events)

    def loop() -> None:
        try:
            run(events, stop, lease, lambda: _event_refresh(events), interval=HOLDER_INTERVAL_S,
                coalesce=holder_coalesce(), floor_all=holder_coalesce() == 3.0)
        finally:
            _native_events.remove(events)
            events.close()
            lease.release()

    thread = spawn_context_thread(loop, name="kanban-annotation-holder")
    thread.start()
    _holder.append((thread, stop, lease))
    return thread


def reset() -> None:
    _event_ready.clear()
    _coverage_stale.clear()
    _unsettled.clear()
    _settle_deadlines.clear()
    _clock_deadlines.clear()
    _known.clear()
    _touched.clear()
    _code_ids.clear()
    _core.clear()
    _gates.clear()
    _seen.clear()
    _paths_memo.clear()
    _parts.reset()
    _pin_set.scope = None
