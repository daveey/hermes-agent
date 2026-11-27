"""Which sessions have a turn running right now, in any Hermes process. [daveey]

The sidebar learns that a turn runs from its own stream events and from the ACTIVE gateway's
``session.active_list`` poll. A turn started anywhere else never reaches it: a Kanban close-out turn
the backend wakes in a chat on a gateway the window is not looking at, a turn held by an SSH-isolated
``serve`` backend or the dashboard process, or a CLI/gateway turn. The row then painted its stored
outcome (red for a decision) or green while the agent was still working in it.

Session-list endpoints now stamp ``turn_live: true`` on each row whose compression lineage holds a
live durable turn lease (state.db ``session_turn_leases``, held from admission to flush of every turn
by any surface, keyed by the lineage root). A lease whose holder pid is provably dead does not count.
A turn a core update paused at a step boundary (gateway update checkpoint, or a Desktop/TUI
``core_update_pause`` turn marker) counts too while it can still auto-continue: it holds no lease
while it waits, and the row painted its stored red decision for the length of every update.
Cached for a couple of seconds because the sidebar polls.
"""

from __future__ import annotations
from hermes_cli.kanban_annotation_facts import consumer as _fact_consumer

import json
import logging
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable

from hermes_cli.kanban_execution_observations import ADAPTER_ERRORS
from hermes_cli.turn_process_execution_observations import TURN_SOURCE, profile_for_state_path, replace, turn

_log = logging.getLogger(__name__)

_CACHE_TTL_S = 2.0
_cache_lock = threading.Lock()
_cache: tuple[float, frozenset[str]] | None = None
# Live lease rows behind the last collection, keyed by lineage root. Source times only.
_leases: dict[str, list[dict[str, Any]]] = {}


def _state_db_paths() -> list[Path]:
    paths: list[Path] = []
    try:
        from hermes_constants import get_hermes_home

        home = Path(get_hermes_home())
        paths.append(home / "state.db")
        profiles = home / "profiles"
        if profiles.is_dir():
            paths.extend(sorted(profiles.glob("*/state.db")))
    except OSError:
        _log.debug("state.db paths unavailable", exc_info=True)
    return [p for p in paths if p.is_file()]


def _open_ro(path: Path) -> sqlite3.Connection:
    """Read-only open; ``mode=ro`` refuses a WAL db whose -shm is absent, so fall back to query_only."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
        return conn
    except sqlite3.OperationalError:
        conn = sqlite3.connect(f"file:{path}?mode=rw", uri=True, timeout=2.0)
        conn.execute("PRAGMA query_only=ON")
        return conn


_PID_RE = re.compile(r"(?:^|:)pid=(\d+)(?::|$)")


def _holder_dead(holder: str) -> bool:
    """Display-grade liveness: True when the holder's ``pid=`` is not a running process on this host.

    Lease reclaim refuses to probe unstamped holders (it waits out the TTL so a false reclaim never
    ends a live turn), but that would leave a crashed turn painting "working" for up to the lease TTL.
    A wrong answer here only repaints a row, so probe directly. Unparseable holders count as live."""
    match = _PID_RE.search(holder or "")
    pid = int(match.group(1)) if match else 0
    if pid <= 0 or pid == os.getpid() or os.name == "nt":
        return False
    try:
        os.kill(pid, 0)  # windows-footgun: ok — nt returns above
    except ProcessLookupError:
        return True
    except (OSError, OverflowError):  # PermissionError: alive, another user's
        return False
    return False


# [daveey] A turn a Hermes core update paused at a step boundary holds no lease while it waits, yet it
# continues automatically: the row must keep reading as working, not paint its stored outcome (a red
# decision) for the minute or two the update takes. A Desktop/TUI pause counts only within the bounds its
# producer applies (the home's configured ``desktop.auto_continue``: enabled, freshness_minutes,
# max_attempts), so a pause nobody will continue stops counting. The gateway bound (1 h) is this reader's
# own fail-closed cap on a checkpoint, not a mirrored producer setting.
_GATEWAY_CHECKPOINT_PREFIX = "gateway.update_checkpoint.v1."
_GATEWAY_CHECKPOINT_END = _GATEWAY_CHECKPOINT_PREFIX[:-1] + chr(ord(_GATEWAY_CHECKPOINT_PREFIX[-1]) + 1)  # key range end
_GATEWAY_PAUSE_FRESH_S = 3600.0
_TUI_PAUSE_REASON = "core_update_pause"


def _update_paused_gateway_turns(conn: sqlite3.Connection, now: float) -> set[str]:
    """Sessions whose started gateway admission is parked on an update checkpoint
    (``hermes_state_update_pause.checkpoint_session_input``), dated by the checkpointed last message."""
    return {sid for sid, stamp in _paused_gateway_checkpoints(conn) if now - stamp <= _GATEWAY_PAUSE_FRESH_S}


def _paused_gateway_checkpoints(conn: sqlite3.Connection) -> list[tuple[str, float]]:
    """``(session id, checkpointed last message time)`` of every started gateway admission parked on an update
    checkpoint; clock-free, so a retained read stays exact (the caller applies the freshness window)."""
    # Driven from the checkpoint keys (state_meta's primary-key range, usually empty), then the admission by
    # its unique id: CROSS JOIN pins that order. Starting from session_admissions scans the whole table (no
    # index leads with status), 87 ms of CPU per poll on one 4.8k-admission profile store.
    try:
        rows = conn.execute(
            "SELECT a.target_session_id, m.value FROM state_meta m CROSS JOIN session_admissions a"
            " ON a.admission_id = substr(m.key, ?) WHERE m.key >= ? AND m.key < ? AND a.status = 'started'",
            (len(_GATEWAY_CHECKPOINT_PREFIX) + 1, _GATEWAY_CHECKPOINT_PREFIX, _GATEWAY_CHECKPOINT_END)).fetchall()
    except sqlite3.Error:  # no admissions table (older store) or a busy one
        return []
    out: list[tuple[str, float]] = []
    for sid, value in rows:
        try:
            receipt = json.loads(value)
            stamp = conn.execute("SELECT timestamp FROM messages WHERE id = ?",
                                 (int(receipt["max_message_id"]),)).fetchone()
            when = float(stamp[0]) if stamp and stamp[0] is not None else None
        except (ValueError, TypeError, KeyError, sqlite3.Error):
            continue
        if sid and when is not None:
            out.append((str(sid), when))
    return out


def _update_paused_tui_turns(home: Path, now: float) -> set[str]:
    """Sessions whose Desktop/TUI turn marker (``tui_gateway.turn_marker``) records a core-update pause
    that may still auto-continue."""
    try:
        data = json.loads((home / "desktop" / "interrupted_turns.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return set()
    return project_paused_tui_turns(data, lambda: _auto_continue_bounds(home), now)


def project_paused_tui_turns(data, bounds_for_home: Callable[[], tuple[bool, float, int]], now):
    """Pure canonical pause classifier; bounded captures supply detached data."""
    out: set[str] = set()
    bounds: tuple[bool, float, int] | None = None
    for key, entry in (data.items() if isinstance(data, dict) else ()):
        if not (key and isinstance(entry, dict) and entry.get("reason") == _TUI_PAUSE_REASON
                and entry.get("auto_continue", True)):
            continue
        try:
            since = max(float(entry.get(k) or 0) for k in ("started_at", "parked_at", "paused_at"))
            attempts = int(entry.get("attempts") or 0)
        except (TypeError, ValueError):
            continue
        if bounds is None:
            bounds = bounds_for_home()
        enabled, fresh_s, max_attempts = bounds
        if enabled and now - since <= fresh_s and attempts < max_attempts:
            out.add(str(key))
    return out


def _auto_continue_bounds(home: Path) -> tuple[bool, float, int]:
    """The home's configured ``desktop.auto_continue`` bounds, parsed as the producer parses them. The
    home's own config (``load_profile_config_effective``), never this process's: each profile's backend
    continues its own pauses."""
    from hermes_cli.config_effective import load_profile_config_effective
    from tui_gateway.turn_marker import auto_continue_bounds
    try:
        cfg = load_profile_config_effective(home / "config.yaml")
    except Exception:  # health: allow BLE001 -- the producer's _load_cfg also falls open to {} (defaults)
        _log.debug("auto-continue config unreadable in %s", home, exc_info=True)
        cfg = {}
    return auto_continue_bounds(cfg)


_store_lock = threading.Lock()
# [daveey] Per state.db: (commit stamp, lease rows, paused-checkpoint (session, message time) pairs) of its last read.
# Only clock-free facts are kept: expiry, pause freshness and holder liveness are judged again on every collect, so
# a retained read answers exactly what a fresh one would until the store commits (which moves the stamp). Opening
# every profile's store for each collect (schema load + close of 13 WAL stores) was most of this annotation's cost
# on the hub, where most stores are idle between sidebar polls (t_01a21ab4eb964866).
_store_reads: dict[Path, tuple[tuple, tuple, tuple]] = {}


def _db_stamp(path: Path) -> tuple | None:
    """The store's committed generation (``goal_session_activity._db_stamp``); None = unknown, never retained."""
    from hermes_cli.goal_session_activity import _db_stamp as stamp

    return stamp(path)


def _read_store(path: Path, now: float) -> tuple[tuple, tuple]:
    """``(lease rows unexpired at ``now``, paused gateway (session id, message time) pairs)`` of one store."""
    conn = _open_ro(path)
    try:
        try:
            rows = tuple(conn.execute(
                "SELECT conversation_id, holder, acquired_at, expires_at FROM session_turn_leases"
                " WHERE expires_at > ?", (now,)
            ).fetchall())
        except sqlite3.Error:  # no lease table yet (nothing ever ran a turn there) or a busy/odd store
            rows = ()
        paused = tuple(_paused_gateway_checkpoints(conn))
    finally:
        conn.close()
    return rows, paused


def _store(path: Path, now: float) -> tuple[tuple, tuple]:
    """``_read_store``, reused while the store's commit stamp is unchanged. A lease unexpired at the retained
    read's time may expire since (the caller filters by its own clock); one expired then cannot revive without a
    write, so the retained rows are a superset of a fresh read's."""
    before = _db_stamp(path)
    with _store_lock:
        prior = _store_reads.get(path)
    if before is not None and prior is not None and prior[0] == before:
        return prior[1], prior[2]
    rows, paused = _read_store(path, now)
    with _store_lock:
        if before is not None and before == _db_stamp(path):
            _store_reads[path] = (before, rows, paused)
        else:
            _store_reads.pop(path, None)
    return rows, paused


def _unexpired(expires_at: Any, now: float) -> bool:
    """``expires_at > now`` for a retained row; a row SQL returned with a non-numeric expiry stays counted, as the
    read that returned it counted it (SQLite orders text above every number)."""
    if isinstance(expires_at, (int, float)) and not isinstance(expires_at, bool):
        return expires_at > now
    return expires_at is not None


def _collect(now: float) -> frozenset[str]:
    global _leases
    live: set[str] = set()
    leases: dict[str, list[dict[str, Any]]] = {}
    paths = _state_db_paths()
    with _store_lock:
        for gone in _store_reads.keys() - set(paths):
            del _store_reads[gone]
    for path in paths:
        try:
            rows, paused = _store(path, now)
        except (sqlite3.Error, OSError):
            _log.debug("turn leases unreadable in %s", path, exc_info=True)
            with _store_lock:
                _store_reads.pop(path, None)
            continue
        live.update(sid for sid, stamp in paused if now - stamp <= _GATEWAY_PAUSE_FRESH_S)
        live.update(_update_paused_tui_turns(path.parent, now))
        for cid, holder, acquired_at, expires_at in rows:
            if not _unexpired(expires_at, now):
                continue
            if cid and not _holder_dead(str(holder or "")):
                live.add(cid)
                leases.setdefault(cid, []).append({"conversation_id": cid, "holder": holder, "acquired_at": acquired_at,
                               "expires_at": expires_at, "profile": profile_for_state_path(path)})
    _leases = leases
    return frozenset(live)


def live_turn_session_ids() -> frozenset[str]:
    """Lineage-root ids with a live turn lease in any profile's state.db, cached briefly."""
    global _cache
    mono = time.monotonic()
    with _cache_lock:
        if _cache is not None and mono < _cache[0]:
            return _cache[1]
    live = _collect(time.time())
    with _cache_lock:
        _cache = (mono + _CACHE_TTL_S, live)
    return live


def _row_ids(row: dict[str, Any]) -> Iterable[str]:
    for sid in (row.get("id"), row.get("_lineage_root_id"), *(row.get("_lineage_ids") or ())):
        if sid:
            yield sid


@_fact_consumer('turn')
def annotate_turn_activity(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stamp ``turn_live: true`` on each row whose lineage holds a live turn lease; pop it otherwise.
    Never raises: the list must render without it."""
    if not rows:
        return rows
    try:
        live = live_turn_session_ids()
    except (sqlite3.Error, OSError):
        _log.debug("turn activity unavailable", exc_info=True)
        return rows
    leases = _leases
    try:
        from hermes_cli.kanban_execution_observations import local_scope
        gateway = local_scope()[0]
    except ADAPTER_ERRORS:
        _log.debug("turn gateway identity unavailable", exc_info=True)
        gateway = None
    now = time.time()
    return project_turn_activity(rows, live=live, leases=leases, gateway=gateway, now=now)


def project_turn_activity(rows, *, live, leases, gateway, now, lease_project=None):
    """Pure serializer over captured live IDs and per-ID lease dictionaries."""
    lease_project = turn if lease_project is None else lease_project
    for row in rows:
        roots = [sid for sid in _row_ids(row) if sid in live]
        if roots:
            row["turn_live"] = True
        else:
            row.pop("turn_live", None)
        try:
            found = [lease for sid in dict.fromkeys(roots) for lease in leases.get(sid, ())]
            replace(row, {TURN_SOURCE}, [lease_project(lease, gateway=gateway, profile=lease["profile"], now=now)
                                         for lease in found])
        except ADAPTER_ERRORS:
            _log.debug("turn execution observation unavailable", exc_info=True)
    return rows


def _invalidate_cache() -> None:
    global _cache
    with _cache_lock:
        _cache = None
    with _store_lock:
        _store_reads.clear()


_reset_cache_for_tests = _invalidate_cache
