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
  own context (a serve hosting several profiles keeps each profile's demand under its own home), and renews a copy
  past ``HOLDER_REFRESH_AGE_S`` with a real read (bypassing the counts cache), so a fresh ``read_at`` is published
  before adopters drop the copy at ``_COUNTS_MAX_AGE_S``. A miss anywhere still refreshes locally as before:
  no added latency, and ``HERMES_ANNOTATION_HOST_COPY=0`` turns publishing and adopting off.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import os
import threading
import time
import weakref
from pathlib import Path
from typing import Any

from hermes_cli import kanban_annotation_disk as _disk
from hermes_cli import kanban_annotation_mutation as _mutation

log = logging.getLogger("hermes_cli.kanban_session_activity")

KILL_SWITCH_ENV = "HERMES_ANNOTATION_HOST_COPY"  # health: allow HX002 -- the design's kill switch (§2c)
LEASE_NAME = "kanban-annotation"
DEMAND_WINDOW_S = 60.0
DEMAND_TOUCH_S = 10.0  # a poller re-touches its scope's demand file at most this often
HOLDER_INTERVAL_S = 2.0
HOLDER_MOVED_FLOOR_S = 10.0
DISPLAY_MAX_AGE_S = 12.0
HOLDER_REFRESH_AGE_S = 20.0  # the holder re-reads an unchanged scope this long after its copy's read
NARROW_CONFIRM_S = 6.0  # an unchanged narrow key re-stamps the copy's display ``verified_at`` this often
_VERSION = 1
_UNDECLARED = "undeclared"
_fp_local = threading.local()  # .waiting: the declared waiting inputs the last fingerprint() on this thread read
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
        _fp_local.waiting = None
        if not _ksa._revalidate.revalidatable(full, wall):
            return None
        waiting, complete = _waiting_inputs(dict(_ksa._waiting_sources))
        _fp_local.waiting = waiting
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


def _narrow_now(scope: tuple[str, ...], full: tuple, fp: tuple[str, bool] | None) -> str | None:
    """The display/holder-only narrow key (``kanban_annotation_narrow``) for the inputs :func:`fingerprint` just
    read on this thread; None (= moved) whenever it is unknown. Never used to certify an exact adoption."""
    waiting = getattr(_fp_local, "waiting", None)
    if fp is None or waiting is None:
        return None
    from hermes_cli import kanban_annotation_narrow as _narrow

    return _narrow.key(scope, full, waiting)


# -- the read -----------------------------------------------------------------------------------------------------

# Per origin scope, the last frozen annotation parts (``read_annotation_parts``).
frozen_parts: dict[tuple[str, ...], tuple] = {}


def read_annotation_parts() -> tuple:
    """``kanban_session_activity._read_annotation_parts``: one read of the scope's counts, waiting sources and
    attention, frozen. Every helper is looked up on that module (tests replace them there)."""
    from hermes_cli import kanban_session_activity as _ksa

    snapshot = _ksa._all_counts(include_snapshot=True)
    active, blocked, unseen, remote_stale, waiting, parked, held, stalled, remote_keys = snapshot[:9]
    execution = getattr(snapshot, "execution", {})
    extra = _ksa._extra_waiting()
    try:
        attention = _ksa.attention_task_counts()
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


# -- adopt --------------------------------------------------------------------------------------------------------

def _display_identity(scope: tuple[str, ...]) -> tuple[str, bool]:
    """Code/registration identity only: never runs a waiting input probe."""
    from hermes_cli import kanban_session_activity as _ksa

    sources = dict(_ksa._waiting_sources)
    waiting, complete = [], True
    for name, fn in sorted(sources.items()):
        declared = _declared.get(name)
        has_inputs = declared is not None and declared[0] is fn
        complete = complete and has_inputs
        waiting.append([name, _code_id(fn), _code_id(declared[1]) if declared is not None and has_inputs else _UNDECLARED])
    body = [_VERSION, list(scope), core_sha(),
            [[name, _code_id(fn)] for name, fn in sorted(_ksa._done_checks.items())], waiting]
    return hashlib.sha256(json.dumps(body, separators=(",", ":"), sort_keys=True).encode()).hexdigest(), complete


def adopt_display(scope: tuple[str, ...]) -> tuple | None:
    """Display-only bounded adoption; cannot certify strict task/closeout inputs.

    Caller holds the annotation lock. Reject pending own writes, and verify code
    identity in the same disk read as the parts, without probing changing inputs.
    """
    from hermes_cli import kanban_session_activity as _ksa

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
        if not 0 <= now - seen <= DISPLAY_MAX_AGE_S:
            return None
        parts = copy[2]
    except Exception:  # health: allow BLE001 -- unknown code/copy fails closed to the normal read
        log.debug("kanban annotation display copy not adopted", exc_info=True)
        return None
    if not complete:
        extra = {sid: frozenset(ids) for sid, ids in _ksa._extra_waiting().items()}
        parts = parts[:9] + (extra,) + parts[10:]
    _ksa._bounded.record(scope, parts, copy[1])
    _ksa._last_annotation[scope] = parts
    return parts


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
        # Only a forced read can acknowledge the cue: an ordinary poll may reuse
        # counts that predate a closeout mutation outside the board fingerprint.
        token = _mutation.current(scope) if getattr(_ksa._revalidate.force_read, "scope", None) == scope else None
        local, full, wall, fp = _inputs_now(scope)
        narrow = _narrow_now(scope, full, fp) if getattr(_holder_ctx, "active", False) else None
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
    return (full, wall, fp, token, display_identity, narrow)


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

    if prepared is None or prepared[2] is None:
        return False
    full, wall, (digest, _complete), token, display_identity, narrow = prepared
    with _ksa._cache_lock:
        if (_ksa._cache is None or _ksa._cache_scope != scope or _ksa._cache_fingerprint != full
                or not _ksa._cache_revalidatable):
            return False
        counts_at = time.time() - max(0.0, time.monotonic() - _ksa._cache_read_at)
    return _disk.save_host(scope, digest, min(wall, counts_at), parts, mutation_token=token,
                           display_identity=display_identity, narrow_key=narrow)


# -- holder -------------------------------------------------------------------------------------------------------

_known: dict[tuple[str, ...], contextvars.Context] = {}
_touched: dict[tuple[str, ...], float] = {}
_holder: list = []


def kick(scope: tuple[str, ...]) -> None:
    """Post-commit mutation cue; never acknowledge a cue that arrives during a read."""
    if not enabled():
        return
    demand(scope)
    try:
        _mutation.record(scope)
    except (OSError, ValueError):
        log.warning("kanban annotation mutation cue not recorded", exc_info=True)


def demand(scope: tuple[str, ...]) -> None:
    """A poll of ``scope``: remember how to reproduce it here and touch its demand file (throttled)."""
    if not enabled():
        return
    now = time.monotonic()
    if now - _touched.get(scope, -1e9) < DEMAND_TOUCH_S:
        return
    _touched[scope] = now
    _known[scope] = contextvars.copy_context()
    _disk.touch_demand(scope)


def _holder_pass() -> int:
    """Refresh every scope this process can reproduce that someone polled lately and whose copy is missing,
    another input's, or aging. Returns how many it refreshed."""
    refreshed = 0
    for scope, ctx in list(_known.items()):
        try:
            refreshed += bool(ctx.run(_holder_refresh, scope))
        except Exception:  # health: allow BLE001 -- one scope's failed read must not stop the others
            log.debug("kanban annotation holder refresh failed", exc_info=True)
    return refreshed


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
        return False  # nobody polled it lately, or this context no longer reproduces the scope
    copy = _disk.load_host(scope, shared=True, header_out=(header := {}))
    copy_age = time.time() - copy[1] if copy is not None else None
    renew = copy_age is not None and copy_age >= HOLDER_REFRESH_AGE_S
    kicked = _mutation.pending(scope)
    if copy is not None and not renew and not kicked:
        _local, full, wall, fp = _inputs_now(scope)
        if fp is not None and fp[0] == copy[0]:
            return False
        if copy_age is not None and 0 <= copy_age < HOLDER_MOVED_FLOOR_S:
            return False
        # [daveey] Exact inputs moved past the floor: refresh only for a substantive move (narrow key). An
        # unchanged narrow key re-stamps the display ``verified_at`` (header only) and waits for the 20 s renewal.
        published = header.get("narrow_key")
        if published and _narrow_now(scope, full, fp) == published:
            verified = header.get("verified_at")
            if not isinstance(verified, (int, float)) or wall - verified >= NARROW_CONFIRM_S:
                _disk.confirm_host(scope, published, wall)
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


def start_holder(role: str) -> threading.Thread | None:
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

    def loop() -> None:
        while not stop.wait(HOLDER_INTERVAL_S):
            try:
                if lease.held():
                    _holder_pass()
                    lease.beat()
            except Exception:  # health: allow BLE001 -- the refresher must outlive one bad pass
                log.warning("kanban annotation holder pass failed", exc_info=True)

    thread = spawn_context_thread(loop, name="kanban-annotation-holder")
    thread.start()
    _holder.append((thread, stop, lease))
    return thread


def reset() -> None:
    _known.clear()
    _touched.clear()
    _code_ids.clear()
    _core.clear()
    _pin_set.scope = None
