"""Display/holder-only narrow origin key for the annotation holder. [daveey]

The holder probes the exact fingerprint every 2 s. When it moved, this key
decides whether the move was substantive for the annotation of one origin
scope. It is NEVER used by strict callers (task APIs, closeout, the default
``_remote_links()``): those keep the exact ``_links_fingerprint`` path.

Key inputs (union for one origin ``(gateway, profile)``):
  * hub replica rows linking this origin (and ledger-bound owner rows, plus
    every same ``(board, id)`` publisher) by liveness-normalized semantic token
    (``kanban_annotation_semantic``): ``activity_at``/``activity_kind`` and a
    valid observation's ``task_hash`` restamp do not move the key;
  * the scoped origin-ledger snapshot token and its raw current-match bits;
  * gateway info rows with ``observed_at`` clocks reduced to 180 s buckets,
    and the replica's ``last_ok`` as a 180 s bucket plus ``offline_since``;
  * every own-board and sync-shadow content key from the exact fingerprint
    (unchanged), plus the exact waiting inputs and code identity.

Anything the key misses is bounded by the holder's 20 s forced renewal; any
read error returns None, which the holder treats as "moved" (exact fallback).
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
from pathlib import Path

log = logging.getLogger(__name__)

_VERSION = 1
BUCKET_S = 180.0
stats = {"keys": 0, "errors": 0, "selections": 0}


def _bucket(value) -> int | None:
    if value is None:
        return None
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("nonfinite clock")
    return int(value // BUCKET_S)


def _normalize_info(value):
    """Gateway info with every ``observed_at`` clock bucketed; other fields verbatim."""
    if isinstance(value, dict):
        return {k: (_bucket(v) if k == "observed_at" and isinstance(v, (int, float)) else _normalize_info(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize_info(v) for v in value]
    return value


def _infos(rows: tuple) -> list:
    from hermes_cli.kanban_replica_rows import text

    out = []
    for gw, data in rows:
        info = json.loads(text(data) if not isinstance(data, str) else data)
        # A collector's heartbeat period jitters by seconds; it only sizes a >=450 s window.
        if isinstance(info, dict) and isinstance(info.get("scope_heartbeat_period_s"), (int, float)):
            info = dict(info, scope_heartbeat_period_s=round(info["scope_heartbeat_period_s"] / 60))
        out.append([gw, _normalize_info(info)])
    return out


class _ScopeState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.clear()

    def clear(self) -> None:
        """Drop every derived view (never the lock): a failed read is never reused."""
        from hermes_cli.kanban_annotation_dependencies import DependencySelector
        from hermes_cli.kanban_annotation_reader import ReplicaReader
        from hermes_cli.kanban_annotation_semantic import TaskTokens

        self.reader = ReplicaReader()
        self.tokens = TaskTokens()
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
    with _states_lock:
        _states.clear()


def _ledger_path() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "kanban" / "origin-ledger.db"


def _origin_part(state: _ScopeState, profile: str) -> list:
    from hermes_cli import kanban_annotation_ledger as kal
    from hermes_cli import kanban_session_activity as ksa

    hub = ksa._hub_db_path()
    if hub is None or not Path(hub).is_file():
        return ["no-hub"]
    with ksa.cached_ro(Path(hub), busy_budget=0) as conn:
        conn.execute("BEGIN")
        try:
            snap = state.reader.read(conn, hub)
        finally:
            conn.rollback()
    rows = snap.rows
    tokens = state.tokens.advance(rows)
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
                                      changed=state.tokens.changed, base=state.tokens.base)
    if state.memo is None or state.memo[0] is not selection:
        stats["selections"] += 1
        digest = hashlib.sha256(json.dumps([[list(k), t] for k, t in selection], separators=(",", ":"),
                                           allow_nan=False).encode()).hexdigest()
        state.memo = (selection, digest)
    return [snap.gateway, ledger_token, [list(c) for c in current], state.memo[1],
            _infos(snap.info_rows), _bucket(snap.last_ok), snap.offline_since is not None]


def key(scope: tuple[str, ...], full: tuple, waiting: list) -> str | None:
    """Narrow key for ``scope``; ``full`` is the exact activity fingerprint (hub signature first when a hub
    exists), ``waiting`` the exact declared waiting inputs. None = unknown (caller treats as moved)."""
    from hermes_cli import kanban_annotation_host as host
    from hermes_cli import kanban_session_activity as ksa

    if len(scope) < 2:
        return None
    state = _state(scope)
    if not state.lock.acquire(blocking=False):
        return None
    try:
        local = list(full[1:]) if ksa._hub_db_path() is not None else list(full)
        body = [_VERSION, list(scope), host.core_sha(), host._portable(local), waiting,
                [[name, host._code_id(fn)] for name, fn in sorted(ksa._done_checks.items())],
                _origin_part(state, scope[1])]
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
