"""Display/holder-only narrow origin key for the annotation holder. [daveey]

The holder probes it every 2 s once a scope's copy is past the 10 s floor, and it decides whether the inputs moved
substantively for the annotation of one origin scope. It is NEVER used by strict callers (task APIs, closeout, the
default ``_remote_links()``, ``kanban_annotation_host.adopt``): those keep the exact ``_links_fingerprint`` / host
``fingerprint`` path, whose inputs are unchanged.

Key inputs (union for one origin ``(gateway, profile)``):
  * hub replica rows linking this origin (and ledger-bound owner rows, plus every same ``(board, id)`` publisher) by
    liveness-normalized semantic token (``kanban_annotation_semantic``): ``activity_at``/``activity_kind`` and a
    valid observation's ``task_hash`` restamp do not move the key;
  * the scoped origin-ledger snapshot token and its raw current-match bits;
  * gateway info rows with every fresh ``observed_at`` clock replaced by its freshness verdict at now (the owner's
    ``scope_window``, as ``scope_heartbeats`` judges it), and the replica's ``last_ok`` stale verdict at now
    (``HUB_STALE_AFTER_S``) plus ``offline_since``. A clock that only advances never moves the key; a freshness flip
    does, even with no new row. (180 s buckets of each owner's clock moved the key once per bucket per owner: with
    ~11 owners on hermes-hub, every ~16 s.)
  * own boards and sync shadows by origin content, never their raw revisions (``kanban_annotation_local``): the open
    cards this profile's sessions own or shepherd and their open ancestors (status, membership, needs-human), and
    this origin's linked shadow rows with the shadow's stale verdict. Raw revisions moved on every commit to the
    shared default board or shadow, so every scope's key moved on nearly every probe (t_01a1db1387f61a98);
  * each waiting source's and done check's code identity. A waiting source's declared inputs are not keyed: the
    chat-closeout inputs carry every board's and shadow's raw content key, so they moved with the same churn.

Anything the key misses is bounded by the holder's 20 s forced renewal, and an own mutation kicks the scope past
the floor; any read error returns None, which the holder treats as "moved" (exact fallback).
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
import time
from pathlib import Path
from types import MappingProxyType

log = logging.getLogger(__name__)

_VERSION = 2
stats = {"keys": 0, "errors": 0, "selections": 0}


def _normalize_info(value, fresh):
    """Gateway info with every ``observed_at`` clock replaced by ``fresh(clock)``; other fields verbatim."""
    if isinstance(value, dict):
        return {k: (fresh(v) if k == "observed_at" else _normalize_info(v, fresh)) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize_info(v, fresh) for v in value]
    return value


def _infos(rows: tuple, now: float) -> list:
    from hermes_cli import kanban_scope_heartbeat as hb
    from hermes_cli.kanban_replica_rows import text

    out = []
    for gw, data in rows:
        info = json.loads(text(data) if not isinstance(data, str) else data)
        window = hb.scope_window(info)  # this owner's window, read before the period is rounded below

        def fresh(clock, window=window):
            if isinstance(clock, float) and not math.isfinite(clock):
                raise ValueError("nonfinite clock")
            # A quiet owner's last beat decides which of its records stand (``published_before_silence``), so the
            # clock itself is keyed once it is no longer fresh; it does not move while the owner stays silent.
            return None if clock is None else (hb.fresh(clock, now, window) or clock)
        # A collector's heartbeat period jitters by seconds; it only sizes a >=450 s window.
        if isinstance(info, dict) and isinstance(info.get("scope_heartbeat_period_s"), (int, float)):
            info = dict(info, scope_heartbeat_period_s=round(info["scope_heartbeat_period_s"] / 60))
        out.append([gw, _normalize_info(info, fresh)])
    return out


def _stale(last_ok, now: float) -> bool:
    """``_project_replica``'s staleness test of the replica's last successful sync (``offline_since`` is keyed apart)."""
    from hermes_cli.kanban_session_activity import HUB_STALE_AFTER_S

    if last_ok is None:
        return True
    last_ok = float(last_ok)
    if not math.isfinite(last_ok):
        raise ValueError("nonfinite clock")
    return now - last_ok > HUB_STALE_AFTER_S


class _ScopeState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.clear()

    def clear(self) -> None:
        """Drop every derived view (never the lock): a failed read is never reused."""
        from hermes_cli.kanban_annotation_dependencies import DependencySelector

        self.cursor: tuple | None = None
        self.token_base: MappingProxyType | None = None
        self.selector = DependencySelector()
        self.memo: tuple | None = None  # (selection object, its digest)
        self.ledger_memo: tuple | None = None  # (fence, LedgerSnapshot)
        self.current_cache: dict = {}


_states: dict[tuple, _ScopeState] = {}
_states_lock = threading.Lock()
_SCOPE_CAP = 32


def _state(scope: tuple[str, ...]) -> _ScopeState:
    with _states_lock:
        state = _states.get(scope)
        if state is None:
            while len(_states) >= _SCOPE_CAP:
                del _states[next(iter(_states))]
            state = _states[scope] = _ScopeState()
        return state


def reset() -> None:
    from hermes_cli import kanban_annotation_inventory as inventory
    from hermes_cli import kanban_annotation_local as local

    with _states_lock:
        _states.clear()
    local.reset()
    inventory.reset()


def _ledger_path() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "kanban" / "origin-ledger.db"


def _origin_part(state: _ScopeState, profile: str, now: float) -> list:
    from hermes_cli import kanban_annotation_inventory as inventory
    from hermes_cli import kanban_annotation_ledger as kal
    from hermes_cli import kanban_session_activity as ksa

    hub = ksa._hub_db_path()
    if hub is None or not Path(hub).is_file():
        return ["no-hub"]
    with ksa.cached_ro(Path(hub), busy_budget=0) as conn:
        conn.execute("BEGIN")
        try:
            snap, tokens, cursor, changed = inventory.read(conn, hub, state.cursor)
        finally:
            conn.rollback()
    rows = snap.rows

    ledger_path = _ledger_path()
    if ledger_path.is_file():
        with ksa.cached_ro(ledger_path, busy_budget=0) as lconn:
            lconn.execute("BEGIN")
            try:
                # [daveey] Same connection, same data_version/total_changes, same file: no commit since the last
                # snapshot, so the O(obligations) decode + binding-hash validation is not repeated.
                st = ledger_path.stat()
                fence = (lconn, lconn.execute("PRAGMA data_version").fetchone()[0], lconn.total_changes,
                         st.st_dev, st.st_ino, snap.gateway, profile)
                memo = state.ledger_memo
                if memo is not None and len(memo[0]) == len(fence) and all(
                        a is b or a == b for a, b in zip(memo[0], fence)):
                    ledger = memo[1]
                else:
                    ledger = kal.snapshot(lconn, snap.gateway, profile)
                    state.ledger_memo = (fence, ledger)
            finally:
                lconn.rollback()
        current = kal.current_matches(ledger, rows, state.current_cache)
        ledger_token, owner_keys = ledger.token, ledger.owner_keys
    else:
        current, ledger_token, owner_keys = (), "absent", ()
    selection = state.selector.select(rows, tokens, snap.gateway, profile, owner_keys=owner_keys,
                                      changed=changed, base=state.token_base)
    state.cursor, state.token_base = cursor, tokens
    if state.memo is None or state.memo[0] is not selection:
        stats["selections"] += 1
        digest = hashlib.sha256(json.dumps([[list(k), t] for k, t in selection], separators=(",", ":"),
                                           allow_nan=False).encode()).hexdigest()
        state.memo = (selection, digest)
    return [snap.gateway, ledger_token, [list(c) for c in current], state.memo[1],
            _infos(snap.info_rows, now), _stale(snap.last_ok, now), snap.offline_since is not None]


def key(scope: tuple[str, ...], full: tuple, waiting: list) -> str | None:
    """Narrow key for ``scope``; ``full`` is the exact activity fingerprint (hub signature first when a hub
    exists; its board and shadow entries name the files and memoise their reads), ``waiting`` the waiting sources'
    code identity (``kanban_annotation_host.waiting_identity``). None = unknown (caller treats as moved)."""
    from hermes_cli import kanban_annotation_host as host
    from hermes_cli import kanban_annotation_local as own
    from hermes_cli import kanban_session_activity as ksa

    if len(scope) < 2:
        return None
    state = _state(scope)
    if not state.lock.acquire(blocking=False):
        return None
    try:
        now = time.time()
        local = list(full[1:]) if ksa._hub_db_path() is not None else list(full)
        body = [_VERSION, list(scope), host.core_sha(), own.local_part(local, scope[1], now), waiting,
                [[name, host._code_id(fn)] for name, fn in sorted(ksa._done_checks.items())],
                _origin_part(state, scope[1], now)]
        stats["keys"] += 1
        return hashlib.sha256(json.dumps(body, separators=(",", ":"), sort_keys=True,
                                         allow_nan=False).encode()).hexdigest()
    except Exception:  # health: allow BLE001 -- an unknown input is no key; the holder falls back to exact
        stats["errors"] += 1
        state.clear()
        log.debug("kanban annotation narrow key unavailable", exc_info=True)
        return None
    finally:
        state.lock.release()
