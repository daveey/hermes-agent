"""Which sessions still have kanban work in flight.

A session that hands work to ``kanban_create`` ends its own turn right away,
while the tasks it created keep running for hours in worker processes. The
sidebar painted such a session as "done" (green stripe) although its work was
still going on. Every kanban task records the session that created it
(``tasks.session_id``), so session-list endpoints stamp each row with how many
of its tasks are still pending or running (``kanban_active``); the desktop
treats a non-zero count like a running background job. [daveey]

Read-only and cheap: one indexed GROUP BY per board database, cached for a few
seconds because the sidebar polls.
"""

from __future__ import annotations

from hermes_cli import kanban_lockguard as _kanban_lockguard
from hermes_cli import kanban_activity_identity as _identity
from hermes_cli import kanban_annotation_disk as _annotation_copy
from hermes_cli import kanban_annotation_refresh as _bounded
from hermes_cli import kanban_activity_revision as _revision
from hermes_cli import kanban_session_activity_busy as _busy
from hermes_cli import kanban_session_activity_hub as _hub_signature
from hermes_cli import kanban_session_activity_revalidate as _revalidate
from hermes_cli import kanban_counts_scopes as _count_scopes
from hermes_cli import kanban_session_activity_recent_done as _recent_done
from hermes_cli.kanban_session_activity_origin_snapshot import remote_snapshot as _remote_snapshot
from hermes_cli.kanban_session_activity_hub import _hub_content_signature
from hermes_cli import kanban_scope_heartbeat as _scope_hb
from hermes_cli.kanban_session_activity_release import TERMINAL_STATUSES
from hermes_cli.kanban_session_activity_release import owner_attests_terminal as _owner_attests_terminal
from hermes_cli.kanban_db_sync import sync_enabled as _board_sync_enabled
from hermes_cli.kanban_scope_heartbeat import scope_heartbeats as _scope_heartbeats
from hermes_cli.kanban_session_activity_records import _fold_legacy_aliases, _moved_evidence, _retire_moved
from hermes_cli.kanban_session_activity_attention import AttentionCache as _AttentionCache
from hermes_cli.kanban_session_activity_attention import run_checks as _run_done_checks
from hermes_cli.kanban_session_activity_card_details import _add_card_details
from hermes_cli.kanban_watch import not_watch_sql as _not_watch
import contextlib
import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

_log = logging.getLogger(__name__)

# Operational parked/review work remains unfinished, not runnable in the scheduler.
ACTIVE_STATUSES = ("todo", "ready", "running", "scheduled", "review")
# Only validated explicit requests paint the sidebar red, in either parked status.
BLOCKED_STATUSES = ("blocked", "triage")

_CACHE_TTL_S = 3.0
# [daveey] Max age of counts under an unchanged fingerprint (kanban_session_activity_revalidate): bounds the
# time-derived states no key sees (stalled after STALL_AFTER_S = 2 h, remote stale after HUB_STALE_AFTER_S = 180 s).
_COUNTS_MAX_AGE_S = 30.0
_cache_lock = threading.Lock()
_cache: tuple[float, dict[str, int], dict[str, int], dict[str, int], dict[str, int]] | None = None
_cache_scope: tuple[str, ...] | None = None
_cache_fingerprint: tuple = ()
_cache_read_at = 0.0  # monotonic time of the read behind ``_cache``
_cache_revalidatable = False  # its fingerprint proves no write since (``_revalidate.revalidatable``)
_waiting_cache: list[dict[str, frozenset[str]]] = [{}]
# (parked, held) per session from the same snapshot as ``_cache`` (see ``_read_parked``); the sink is set while
# ``_all_counts`` reads local boards, like ``_pairs_sink``. [daveey]
_parked_cache: list[tuple[dict[str, int], dict[str, int]]] = [({}, {})]
_parked_sink = threading.local()  # .maps: (parked, held) or None; per thread, so concurrent reads never mix
# [daveey] Per session, parked cards whose agent-owned wait is older than kanban_wait_info.STALL_AFTER_S (amber
# "stalled" in the sidebar), from the same snapshot as ``_cache``.
_stalled_cache: list[dict[str, int]] = [{}]
# [daveey] Per session, the remote cards counted (key, bucket, parked) from the same snapshot as ``_cache``: a
# lineage whose sessions share a card (created in one, shepherded from another) counts it once, as the list shows it.
_remote_keys_cache: list[dict[str, frozenset[tuple[str, str, bool]]]] = [{}]
_local_keys_cache: list[dict[str, frozenset[tuple[str, str, bool]]]] = [{}]
_local_keys_sink = threading.local()
_pairs_lock = threading.Lock()
# _all_counts single flight: held for a whole recompute; the generation bumps when one publishes.
_counts_flight = threading.Lock()
_counts_generation = 0
_cache_generation = 0  # publication generation of the active scoped snapshot
# Cross-thread annotation flight. ``one_annotation_snapshot`` only shares a read
# inside one thread. Session-list polls arrive on many AnyIO workers at once; each
# used to wait on ``_counts_flight`` and then run waiting sources and attention
# itself, so the pool filled and inventory/hub-sync timed out while the event loop
# sat in select (2026-10-28). One refresher runs; overlapping polls take the last
# good snapshot instead of waiting. A poll that arrives after the refresher
# finishes still reads again.
_annotate_refresh_lock = threading.Lock()
_annotate_flight_scope: tuple[str, ...] | None = None
_annotate_flight_result: tuple | None = None
_last_annotation: dict[tuple[str, ...], tuple] = {}
_annotate_waiters = 0
_REFRESH_WAIT_S = 1.0  # [daveey] see kanban_annotation_refresh
WAITING_ON_CAP = 50


def _origin_scope() -> tuple[str, ...]:
    """Namespace cached projections by origin profile and configured storage roots."""
    from hermes_constants import get_hermes_home
    from hermes_cli import kanban_db
    from hermes_cli.profiles import get_active_profile_name

    return (str(get_hermes_home()), get_active_profile_name(), str(kanban_db.kanban_home()),
            os.environ.get('HERMES_HUB_STATE', str(Path.home() / '.local/state/hermes-hub')),
            os.environ.get('HERMES_MAIL_CONFIG', str(Path.home() / '.config/hermes-mail/config.json')))



def _board_db_paths() -> list[Path]:
    from hermes_cli import kanban_db

    paths: list[Path] = []
    root = kanban_db.kanban_home()
    default = root / "kanban.db"
    if default.exists():
        paths.append(default)
    try:
        boards = kanban_db.boards_root()
        if boards.is_dir():
            paths.extend(p for p in sorted(boards.glob("*/kanban.db")) if p.is_file())
    except OSError:
        pass
    return paths


def _status_filter(conn: sqlite3.Connection, statuses: tuple[str, ...]) -> tuple[str, tuple]:
    """Split display counts by explicit evidence, without changing execution gates.

    A connection-local function keeps SQL and replica validation identical,
    including malformed JSON and Python's non-ASCII whitespace handling.
    """
    ph = ",".join("?" for _ in statuses)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    conn.create_function("kanban_needs_human", 2, _stored_needs_human, deterministic=True)
    evidence = "t.human_request" if "human_request" in cols else "NULL"
    kind = "t.block_kind" if "block_kind" in cols else "NULL"
    human = f"kanban_needs_human({evidence}, {kind})"
    parked = "t.status IN ('blocked', 'triage')"
    if statuses == BLOCKED_STATUSES:
        return f"({parked} AND {human})", ()
    return f"((t.status IN ({ph}) OR ({parked} AND NOT {human})) AND {_not_watch(conn)})", statuses  # [daveey]


def _stored_needs_human(value, kind) -> bool:
    """Decode the DB's JSON text before applying the shared evidence contract."""
    from hermes_cli.kanban_human_request import needs_human

    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return False
    return needs_human(value, kind=kind)


# Set while ``_all_counts`` reads local boards: collects the (session, task) pairs behind the counts, so the
# open card ids per session (``waiting_on_task_ids``) cost no extra queries. [daveey]
_pairs_sink: set[tuple[str, str]] | None = None


# Subscription reasons that make a ``tui`` subscriber own the card's progress (stamped in
# ``delivery_metadata.reason``): ``create`` (kanban_create's auto-sub) and ``shepherd``
# (explicit ``notify-subscribe``). Explicit ``touch`` (auto-sub on comment/unblock/link)
# does not own progress. Legacy/invalid metadata cannot distinguish a shepherd from a
# touch: preserve that obligation until provenance is reconciled, never silently release it.
# [daveey]
COUNTED_SUB_REASONS = ("create", "shepherd")


def _counted_sub_clause(conn: sqlite3.Connection) -> str:
    """SQL predicate (aliases ``s`` subs, ``t`` tasks) for a ``tui`` sub that counts as the chat's work."""
    owner = "s.chat_id = COALESCE(t.session_id, '')"
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(kanban_notify_subs)")}
    except sqlite3.Error:
        cols = set()
    if "delivery_metadata" not in cols:
        return "1"
    # CASE avoids json_extract on invalid legacy metadata (SQLite need not
    # short-circuit boolean OR expressions). Only proven incidental touches release.
    return (f"({owner} OR CASE WHEN json_valid(s.delivery_metadata) "
            "THEN COALESCE(json_extract(s.delivery_metadata, '$.reason'), '') != 'touch' "
            "ELSE 1 END)")


def _read_counts(path: Path, statuses: tuple[str, ...] = ACTIVE_STATUSES) -> dict[str, int]:
    """Tasks per session in ``statuses``: tasks the session created, plus tasks
    it shepherds (a ``tui`` notify sub stamped ``create``/``shepherd``, see
    ``COUNTED_SUB_REASONS``): it is just as "still working" until they land.
    A touch-only sub (one comment on someone else's card) does not count.
    Each (session, task) pair counts once."""
    conn = _open_ro(path)
    try:
        return _counts_on(conn, statuses)
    finally:
        conn.close()


def _counts_on(conn: sqlite3.Connection, statuses: tuple[str, ...] = ACTIVE_STATUSES) -> dict[str, int]:
    if "session_id" not in {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}:
        return {}  # explicitly supported pre-session schema, not a failed read
    match, params = _status_filter(conn, statuses)
    pairs: set[tuple[str, str]] = {
        (str(sid), str(tid)) for sid, tid in conn.execute(
            f"SELECT session_id, id FROM tasks t WHERE session_id IS NOT NULL "
            f"AND session_id != '' AND {match}",
            params,
        )
    }
    try:
        pairs.update(
            (str(sid), str(tid)) for sid, tid in conn.execute(
                f"SELECT s.chat_id, t.id FROM kanban_notify_subs s JOIN tasks t ON t.id = s.task_id "
                f"WHERE s.platform = 'tui' AND s.chat_id != '' AND {_counted_sub_clause(conn)} AND {match}",
                params,
            )
        )
    except sqlite3.Error:  # board predates notify subscriptions
        pass
    counts: dict[str, int] = {}
    for sid, _tid in pairs:
        counts[sid] = counts.get(sid, 0) + 1
    if _pairs_sink is not None:
        _pairs_sink.update(pairs)
    return counts


# [daveey] Open cards nobody is working right now: they still count in ``kanban_active`` (the chat isn't done, and
# the archive guard must see them), but the sidebar must not paint them as running. ``scheduled`` waits on a time or
# event wake; a non-human ``blocked``/``triage`` card waits on an operational condition; a ``todo`` card waits on an
# unfinished parent. "Held" is the subset of those queued cards whose chain waits on a human ask upstream.
PARKED_STATUSES = ("scheduled",) + BLOCKED_STATUSES
_FINISHED_STATUSES = ("done", "archived")


def _local_wait_info(conn: sqlite3.Connection, task_ids: Iterable[str], now: float | None = None,
                     ) -> dict[str, dict[str, Any]]:
    """[daveey] ``{task_id: wait info}`` (``kanban_wait_info.describe``) for the parked cards among ``task_ids``: what
    each waits on, since when, and whether only an agent can end it. One query for the cards, one for their newest
    park events. Missing columns/tables degrade to status-only info."""
    return _describe_waits(*_local_wait_rows(conn, task_ids), now=now)


def _local_wait_rows(conn: sqlite3.Connection, task_ids: Iterable[str]) -> tuple[dict[str, tuple], dict[str, tuple]]:
    """``(parked, events)``: the stored rows ``_describe_waits`` turns into wait info. Separate so a board memo can
    keep the rows and re-judge their age at each read (the stall cut-off moves with the clock, the rows do not)."""
    from hermes_cli import kanban_wait_info as kwi

    ids = [str(t) for t in dict.fromkeys(task_ids)]
    if not ids:
        return {}, {}
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    pick = ", ".join(f"t.{c}" if c in cols else f"NULL AS {c}" for c in ("id", "status", "block_kind", "human_request",
                                                                          "awaiting"))
    parked: dict[str, tuple] = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        ph = ",".join("?" for _ in chunk)
        for tid, status, kind, request, awaiting in conn.execute(
                f"SELECT {pick} FROM tasks t WHERE t.id IN ({ph}) AND t.status IN ('scheduled', 'blocked', 'triage')",
                chunk):
            parked[str(tid)] = (status, kind, request, awaiting)
    if not parked:
        return {}, {}
    events: dict[str, tuple] = {}
    keys = list(parked)
    kph = ",".join("?" for _ in kwi.PARK_EVENT_KINDS)
    for i in range(0, len(keys), 500):
        chunk = keys[i:i + 500]
        ph = ",".join("?" for _ in chunk)
        try:
            for tid, kind, payload, at in conn.execute(
                    f"SELECT e.task_id, e.kind, e.payload, e.created_at FROM task_events e WHERE e.id IN ("
                    f"SELECT MAX(id) FROM task_events WHERE task_id IN ({ph}) AND kind IN ({kph}) GROUP BY task_id)",
                    (*chunk, *kwi.PARK_EVENT_KINDS)):
                events[str(tid)] = (kind, payload, at)
        except sqlite3.Error:  # board predates task_events
            break
    return parked, events


def _describe_waits(parked: dict[str, tuple], events: dict[str, tuple], now: float | None = None,
                    ) -> dict[str, dict[str, Any]]:
    from hermes_cli import kanban_wait_info as kwi

    out: dict[str, dict[str, Any]] = {}
    for tid, (status, kind, request, awaiting) in parked.items():
        ev_kind, payload, at = events.get(tid, (None, None, None))
        try:
            human = _stored_needs_human(request, kind)
        except Exception:
            human = False
        info = kwi.describe(status, block_kind=kind, human=human, park=payload, park_kind=ev_kind, since=at,
                            awaiting=awaiting, now=now)
        if info is not None:
            out[tid] = info
    return out


def _read_parked(path: Path) -> tuple[dict[str, int], dict[str, int]]:
    """``(parked, held)`` per session over the same (session, task) pairs ``_read_counts`` counts as active.

    A ``todo`` card with every parent finished (or none) is about to be promoted, so it stays live. A ``todo`` card
    whose ancestors include a card blocked on a validated human request is held, not parked. Boards without
    ``task_links`` classify by status alone."""
    conn = _open_ro(path)
    try:
        return _parked_on(conn)
    finally:
        conn.close()


def _parked_on(conn: sqlite3.Connection) -> tuple[dict[str, int], dict[str, int]]:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    if "session_id" not in cols:
        return {}, {}
    match, params = _status_filter(conn, ACTIVE_STATUSES)
    evidence = "t.human_request" if "human_request" in cols else "NULL"
    kind = "t.block_kind" if "block_kind" in cols else "NULL"
    pairs: set[tuple[str, str]] = {
        (str(sid), str(tid)) for sid, tid in conn.execute(
            f"SELECT session_id, id FROM tasks t WHERE session_id IS NOT NULL AND session_id != '' AND {match}",
            params)}
    try:
        pairs.update(
            (str(sid), str(tid)) for sid, tid in conn.execute(
                f"SELECT s.chat_id, t.id FROM kanban_notify_subs s JOIN tasks t ON t.id = s.task_id "
                f"WHERE s.platform = 'tui' AND s.chat_id != '' AND {_counted_sub_clause(conn)} AND {match}",
                params))
    except sqlite3.Error:  # board predates notify subscriptions
        pass
    if not pairs:
        return {}, {}
    info: dict[str, tuple[str, bool]] = {
        str(tid): (status, bool(human)) for tid, status, human in conn.execute(
            f"SELECT t.id, t.status, kanban_needs_human({evidence}, {kind}) FROM tasks t "
            f"WHERE t.status NOT IN ({','.join('?' * len(_FINISHED_STATUSES))})", _FINISHED_STATUSES)}
    parents: dict[str, list[str]] = {}
    try:
        for parent, child in conn.execute("SELECT parent_id, child_id FROM task_links"):
            parents.setdefault(str(child), []).append(str(parent))
    except sqlite3.Error:  # board predates dependencies
        pass

    def open_parents(tid: str) -> list[str]:
        return [p for p in parents.get(tid, ()) if p in info]  # finished/unknown parents never wait

    def waits_on_human(tid: str) -> bool:
        seen: set[str] = set()
        stack = open_parents(tid)
        while stack:
            p = stack.pop()
            if p in seen:
                continue
            seen.add(p)
            status, human = info[p]
            if status in BLOCKED_STATUSES and human:
                return True
            stack.extend(open_parents(p))
        return False

    parked: dict[str, int] = {}
    held: dict[str, int] = {}
    for sid, tid in pairs:
        status = info.get(tid, ("", False))[0]
        if status == "todo":
            if not open_parents(tid):
                continue
            into = held if waits_on_human(tid) else parked
        elif status in PARKED_STATUSES:
            into = parked
        else:
            continue
        into[sid] = into.get(sid, 0) + 1
        captured = getattr(_parked_sink, "pairs", None)
        if captured is not None:
            captured[0 if into is parked else 1].add((sid, tid))
    return parked, held


def _parked_pairs(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    """(session, task) for every parked card (scheduled/blocked/triage) a session created or shepherds."""
    if "session_id" not in {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}:
        return set()
    parked = f"(t.status IN ('scheduled', 'blocked', 'triage') AND {_not_watch(conn)})"  # watch: kanban_watch
    pairs: set[tuple[str, str]] = {
        (str(sid), str(tid)) for sid, tid in conn.execute(
            f"SELECT session_id, id FROM tasks t WHERE session_id IS NOT NULL AND session_id != '' AND {parked}")}
    try:
        pairs.update(
            (str(sid), str(tid)) for sid, tid in conn.execute(
                f"SELECT s.chat_id, t.id FROM kanban_notify_subs s JOIN tasks t ON t.id = s.task_id "
                f"WHERE s.platform = 'tui' AND s.chat_id != '' AND {_counted_sub_clause(conn)} AND {parked}"))
    except sqlite3.Error:  # board predates notify subscriptions
        pass
    return pairs


def _read_stalled(path: Path, now: float | None = None) -> dict[str, int]:
    """[daveey] Per session, its parked cards whose wait is agent-owned and older than
    ``kanban_wait_info.STALL_AFTER_S`` (nothing outside the agents will end it): the sidebar's amber "stalled".
    Same (session, task) pairs as the counts."""
    return _stalled_counts(*_read_stall_rows(path), now=now)


def _read_stall_rows(path: Path) -> tuple[set[tuple[str, str]], dict[str, tuple], dict[str, tuple]]:
    """``(pairs, parked, events)`` behind ``_read_stalled``: what the board stores, before the clock judges it."""
    conn = _open_ro(path)
    try:
        return _stall_rows_on(conn)
    finally:
        conn.close()


def _stall_rows_on(conn: sqlite3.Connection) -> tuple[set[tuple[str, str]], dict[str, tuple], dict[str, tuple]]:
    pairs = _parked_pairs(conn)
    if not pairs:
        return set(), {}, {}
    return (pairs, *_local_wait_rows(conn, (tid for _, tid in pairs)))


def _stalled_counts(pairs: set[tuple[str, str]], parked: dict[str, tuple], events: dict[str, tuple],
                    now: float | None = None, *, keys: set[tuple[str, str]] | None = None) -> dict[str, int]:
    if not pairs:
        return {}
    info = _describe_waits(parked, events, now=now)
    out: dict[str, int] = {}
    for sid, tid in pairs:
        if (info.get(tid) or {}).get("wait_state") == "stalled":
            out[sid] = out.get(sid, 0) + 1
            if keys is not None:
                keys.add((sid, tid))
    return out


# Terminal kinds that produce a visible notice (mirrors tui_gateway.session_notifications; silent kinds such as
# archived/unblocked never leave a notice waiting).
UNSEEN_KINDS = ("completed", "blocked", "gave_up", "crashed", "timed_out", "status")


def _read_unseen(path: Path) -> dict[str, int]:
    """``tui`` subscriptions per chat whose cursor trails a terminal event: a result the session has not been
    handed yet (its backend is gone, so no poller claims it until the unloaded-session sweeper does). [daveey]"""
    conn = _kanban_lockguard.connect(f"file:{path}?mode=ro", uri=True, timeout=0.5)
    try:
        return _unseen_on(conn)
    finally:
        conn.close()


def _unseen_on(conn: sqlite3.Connection) -> dict[str, int]:
    try:
        ph = ",".join("?" for _ in UNSEEN_KINDS)
        rows = conn.execute(
            f"SELECT s.chat_id, COUNT(*) FROM kanban_notify_subs s WHERE s.platform = 'tui' AND s.chat_id != '' "
            f"AND EXISTS (SELECT 1 FROM task_events e WHERE e.task_id = s.task_id AND e.id > s.last_event_id "
            f"AND e.kind IN ({ph})) GROUP BY s.chat_id", UNSEEN_KINDS).fetchall()
    except sqlite3.Error:  # board predates notify subscriptions
        return {}
    return {str(sid): int(n) for sid, n in rows}


# Remote ownership comes from create/adopt receipts in the origin ledger. Replica
# session links observe the linked card only, never its dependency graph. Created
# children inherit their own origin subscription/receipt; dependency children may
# be unrelated downstream work and MUST NOT be walked. Ledger release stays strict.
def _hub_db_path() -> Path | None:
    base = os.environ.get("HERMES_HUB_STATE") or str(Path.home() / ".local" / "state" / "hermes-hub")
    path = Path(base) / "hub.db"
    return path if path.is_file() else None


def replica_task_resolution(ids: list[str], now: float | None = None) -> tuple[set[str], bool]:
    """Which ``t_<hex>`` ids exist as tasks in the hermes-hub replica of any gateway (same file ``_remote_links``
    reads; replica keys are ``[profile/]board/<id>``, so qualified ``@task:p@gw/<id>`` refs resolve by id).

    Returns ``(found, authoritative)``; False when a replica exists but can't answer (unreadable, offline, or not
    synced within ``HUB_STALE_AFTER_S``): absent ids are *unknown*, never flagged. No replica is authoritative only
    when ``kanban.sync`` is off; a Board-synced host (worker node: no hub sync) holds a partial board. [daveey]"""
    path = _hub_db_path()
    if path is None or not ids:  # sync_enabled never raises: an unreadable config reads as sync off (standalone)
        return set(), not ids or not _board_sync_enabled()
    try:
        conn = _open_ro(path)
        try:
            offline = _meta_float(conn, "offline_since")
            synced = max(filter(None, (_meta_float(conn, "last_ok"), _meta_float(conn, "last_digest"))), default=None)
            found = {
                tid for tid in ids
                if conn.execute("SELECT 1 FROM replica WHERE coll = 'tasks' AND substr(key, -?) = ? LIMIT 1",
                                (len(tid) + 1, "/" + tid)).fetchone()
            }
        finally:
            conn.close()
    except sqlite3.Error:
        return set(), False
    now = time.time() if now is None else now
    fresh = offline is None and synced is not None and now - synced <= HUB_STALE_AFTER_S
    return found, fresh


from hermes_cli.kanban_generation import open_ro as _open_ro


# Linked-card reads run on every sidebar refresh across ~16 boards and replicas; a fresh connection pays schema
# parse + WAL-index setup + close each time (~2.5 ms a board, the whole latency budget). The dashboard is long-lived,
# so keep one read-only connection per file, shared across the threadpool (one per thread would hold ~3 fds per
# board per worker). An idle autocommit reader holds no snapshot, so it never blocks writers or checkpoints.
_RO_CACHE: dict[str, tuple[sqlite3.Connection, tuple[int, int], threading.Lock, tuple[int, int] | None]] = {}
_RO_CACHE_LOCK = threading.RLock()


from hermes_cli.kanban_generation import recover as _recover_generation, evict_readers, register_reader_cache
register_reader_cache(evict_readers)


@contextlib.contextmanager
def cached_ro(path: Path, *, busy_budget: float = 0.5) -> Iterator[sqlite3.Connection]:
    """A shared read-only connection to ``path``, held exclusively for the ``with`` body. Reopened when the file is
    replaced (new inode: a rebuilt replica), after any sqlite error, or when its ``-shm`` generation is gone (the
    connection is stranded on a deleted WAL index and would fail with ``disk I/O error`` or read stale rows until
    restart; kanban-759104eb56). Raises ``sqlite3.OperationalError`` when the file is missing, like ``_open_ro``."""
    from hermes_cli.kanban_reader_lifecycle import cached_entry

    key, entry = cached_entry(path, busy_budget)
    conn, _ident, lock, _shm = entry
    with lock:  # bodies read every result they start: an unfinished cursor would pin a WAL snapshot
        with _RO_CACHE_LOCK:
            if _RO_CACHE.get(key) is not entry:
                from hermes_cli.kanban_generation import DeletedWalGenerationError
                raise DeletedWalGenerationError(f"kanban DB {path}: reader was evicted while waiting; retry")
        conn.row_factory = None
        # [daveey] The budget covers the body's statements too: a warm cached reader otherwise spends its own 500 ms
        # SQLite busy timeout on a busy board even when the open was asked for less (t_01a19e082c8721b1).
        lowered = busy_budget < _RO_BUSY_TIMEOUT_S
        if lowered:
            conn.execute(f"PRAGMA busy_timeout={max(0, int(busy_budget * 1000))}")
        try:
            yield conn
        except sqlite3.Error:
            with _RO_CACHE_LOCK:
                if _RO_CACHE.get(key) is entry:
                    del _RO_CACHE[key]
            _close_quietly(conn)
            raise
        finally:
            if lowered and _RO_CACHE.get(key) is entry:
                try:
                    conn.execute(f"PRAGMA busy_timeout={int(_RO_BUSY_TIMEOUT_S * 1000)}")
                except sqlite3.Error:  # never keep a reader with the probe's budget
                    with _RO_CACHE_LOCK:
                        if _RO_CACHE.get(key) is entry:
                            del _RO_CACHE[key]
                    _close_quietly(conn)


# The cached reader's own SQLite busy timeout, set when ``kanban_reader_lifecycle`` opens it.
_RO_BUSY_TIMEOUT_S = 0.5


def _close_quietly(conn: sqlite3.Connection) -> None:
    try:
        conn.close()
    except sqlite3.Error:
        pass


def _self_gateway(conn: sqlite3.Connection) -> str:
    try:
        row = conn.execute("SELECT v FROM meta WHERE k = 'gateway'").fetchone()
        if row and row[0]:
            return str(row[0])
    except sqlite3.Error:
        pass
    try:
        cfg = Path(os.environ.get("HERMES_MAIL_CONFIG") or Path.home() / ".config" / "hermes-mail" / "config.json")
        return str(json.loads(cfg.read_text(encoding="utf-8-sig")).get("gateway") or "")
    except (OSError, ValueError, AttributeError):
        return ""


def _replica_bucket(task: dict[str, Any]) -> str | None:
    """'active' | 'blocked' | None, by the same rules as ``_status_filter`` for local boards. ``human_request`` may be
    the decoded dict or the stored JSON text (``_stored_needs_human`` reads both). [daveey]"""
    status = task.get("status")
    if status == "unknown" and task.get("unfinished"):
        return "active"
    if status in BLOCKED_STATUSES:
        return "blocked" if _stored_needs_human(task.get("human_request"), task.get("block_kind")) else "active"
    return "active" if status in ACTIVE_STATUSES else None


# [daveey] The ONE origin-work association for cards on other gateways. The sidebar counts
# (``kanban_active``/``kanban_blocked``, which also drive the archive guard) and the session task list
# (``session_tasks``) both come from ``_remote_cards``: the origin-ledger projection (``_remote_links``), or the
# synced chat links (``kanban_chat_links.replica_cards``) when ``kanban.sync.links_reader`` is ``replica``. Never
# read one source for the count and the other for the list: that split showed "Waiting on 14" over a 9-chip strip
# (the replica list dropped done-but-unverified cards the ledger still counted). Each link selects only its exact card. Created
# children count via their own inherited links. Gateway/profile/board/id identifies
# a card; distinct qualified profiles stay separate. Historical unqualified aliases
# do not add another count when a qualified record for the same card is available.
HUB_STALE_AFTER_S = 180.0  # same as the hub-sync plugin: no successful hub sync for this long = stale
# Scope-heartbeat freshness is per owner: ``kanban_scope_heartbeat.scope_window`` derives it from the period the
# owner's collector publishes (floor 450 s, the window every owner gets when it publishes none). A fixed 450 s
# against a loaded hub's ~440 s cycle flapped closed cards between released and "never released". [daveey]
SCOPE_HEARTBEAT_FRESH_S = _scope_hb.SCOPE_HEARTBEAT_FLOOR_S
_REMOTE_FIELDS = ("title", "status", "block_kind", "human_request", "assignee", "created_at", "started_at", "priority")
_remote_last: list[dict[str, Any]] | None = None  # last good read (process lifetime)
_remote_scope: tuple[str, ...] | None = None
_snapshot_saved: tuple[tuple[str, ...], list[dict[str, Any]]] | None = None  # last (scope, cards) written to disk


def _meta_float(conn: sqlite3.Connection, key: str) -> float | None:
    try:
        row = conn.execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
        return float(row[0]) if row and row[0] not in (None, "") else None
    except (sqlite3.Error, TypeError, ValueError):
        return None


# (replica path, gw, record key) -> (row text, decoded row) from the last replica read: the projection rebuilds on
# every kanban.db / hub.db change, and re-parsing and re-hashing thousands of unchanged rows cost ~0.3 s per
# sidebar refresh. Keyed on the exact stored text, so an edited row always decodes afresh. [daveey]
_replica_decoded: dict[tuple[str, str, str], tuple[Any, tuple[dict[str, Any], str | None, dict[str, Any]]]] = {}


def _decode_replica_task(record_key: str, data: Any) -> tuple[dict[str, Any], str | None, dict[str, Any]]:
    """``(task, legacy record digest or None, owner evidence)`` of one replica row; a pure function of its text.
    Callers treat ``task`` as read-only: it is shared across reads of an unchanged row."""
    from hermes_cli.kanban_origin_ledger import _owner_evidence
    from hermes_cli.kanban_origin_legacy import record_digest

    try:
        t = json.loads(data)
    except (TypeError, ValueError) as exc:
        raise ValueError('invalid replica task data') from exc
    if not isinstance(t, dict) or not t.get('id'):
        raise ValueError('invalid replica task data')
    digest = record_digest(t) if len(str(record_key).split('/')) == 2 else None
    evidence = _owner_evidence(t) if t.get('status') in TERMINAL_STATUSES else {}
    return t, digest, evidence


# [daveey] Last live (``now=None``) replica read: (key, read_at, None, cards, qualified, owners). The remote
# projection rebuilds on every local board write (its fingerprint covers every kanban.db), and hub.db itself is
# written ~45 times a minute (outbox, ledger, cursor), but its task rows change only when a pull lands. Re-reading
# ~6k replica rows (~23 MB of JSON) cost ~0.1 s CPU per sidebar poll for an unchanged answer. hermes-hub stamps
# every replica write with the hub feed's monotonic ``seq``, so an insert or update raises ``max(seq)`` and a
# delete lowers ``count``: ``_REPLICA_PROBE`` (index-only, ~6 ms) plus the small identity/heartbeat rows is the
# whole input. Reused while that key (scope heartbeats as their freshness verdicts at read time), the origin
# profile, the stale verdict and every ``synced_at`` are unchanged and wall time has not stepped back.
_remote_read_memo: tuple | None = None
_REPLICA_PROBE = ("SELECT count(*), count(seq), max(seq), total(seq), total(version), total(octet_length(data)) "
                  "FROM replica WHERE coll = 'tasks'")


def _read_remote_links(path: Path, now: float | None = None, *,
                       qualified_out: set | None = None,
                       owners_out: set | None = None,
                       moved_out: dict | None = None) -> list[dict[str, Any]]:
    """Replica cards linked to a session on this gateway, one row per (session, card).

    Open and unproven cards come back ``unfinished``; cards their owner attests closed
    (``_owner_attests_terminal``) come back as explicit ``released`` rows, so a durable snapshot sees them
    as present-and-closed rather than missing. A live read (``now=None``) of unchanged replica task rows is
    served from ``_remote_read_memo``; an explicit ``now`` always reads every row."""
    global _remote_read_memo
    from hermes_cli.profiles import get_active_profile_name

    live = now is None
    now = time.time() if now is None else now
    conn = _open_ro(path)
    try:
        conn.execute("BEGIN")  # one snapshot: the memo key must describe the rows it maps to
        me = _self_gateway(conn)
        if not me:
            return []
        # Old replicas can still be displayed, but cannot prove an exact revision.
        columns = {r[1] for r in conn.execute('PRAGMA table_info(replica)')}
        version_column = 'version' if 'version' in columns else 'NULL'
        info_rows = conn.execute("SELECT gw, data FROM replica WHERE coll = 'gateway' AND key = 'info'").fetchall()
        last_ok, offline = _meta_float(conn, "last_ok"), _meta_float(conn, "offline_since")
        key = None
        stale = offline is not None or last_ok is None or now - last_ok > HUB_STALE_AFTER_S
        if live and {'seq', 'version'} <= columns:
            probe = conn.execute(_REPLICA_PROBE).fetchone()
            if probe[0] == probe[1]:  # a row without seq proves nothing about its text: never memoize it
                # [daveey] ``info_rows`` reach the projection only as each scope's freshness verdict at ``now``
                # (release, moved evidence): the owner's per-cycle ``observed_at`` rewrite alone is no new answer.
                key = (str(path), me, version_column, _scope_hb.fresh_scopes(_scope_heartbeats(info_rows), now),
                       offline, probe, get_active_profile_name())
            memo = _remote_read_memo
            # [daveey] ``last_ok`` moves on every hub pull (~2 s) while the task rows rarely do; it reaches the
            # answer only through the stale verdict and each card's ``synced_at`` (``_replica_synced``), so the
            # memo compares those instead of ``last_ok`` itself.
            if (key is not None and memo is not None and memo[0] == key and memo[1] <= now
                    and memo[6] == stale and memo[7] == _replica_synced(memo[8], last_ok)):
                if qualified_out is not None:
                    qualified_out.update(memo[4])
                if owners_out is not None:
                    owners_out.update(memo[5])
                if moved_out is not None:
                    moved_out.update(memo[9])
                return list(memo[3])  # cards are read-only, as in ``_links_cache``
        rows = conn.execute(f"SELECT gw, key, data, updated_at, {version_column} FROM replica WHERE coll = 'tasks'").fetchall()
    finally:
        conn.close()
    if key is None:
        return _project_replica(path, now, me, rows, info_rows, last_ok, offline,
                                qualified_out=qualified_out, owners_out=owners_out, moved_out=moved_out)
    qualified: set = set()
    owners: set = set()
    moved: dict = {}
    seen_at: dict[str, float] = {}
    cards = _project_replica(path, now, me, rows, info_rows, last_ok, offline,
                             qualified_out=qualified, owners_out=owners, seen_out=seen_at, moved_out=moved)
    # Heartbeat cut-offs are the live verdicts in ``key``, hub staleness the ``stale`` check; a clock step back rereads.
    _remote_read_memo = (key, now, None, list(cards),
                         frozenset(qualified), frozenset(owners), stale, _replica_synced(seen_at, last_ok),
                         dict(seen_at), dict(moved))
    if qualified_out is not None:
        qualified_out.update(qualified)
    if owners_out is not None:
        owners_out.update(owners)
    if moved_out is not None:
        moved_out.update(moved)
    return cards


def _replica_synced(seen_at: dict[str, float], last_ok: float | None) -> tuple:
    """Per owner gateway, the ``synced_at`` ``_project_replica`` stamps on its cards (same expression)."""
    out = []
    for gw, seen in sorted(seen_at.items()):
        owner_seen = seen or None
        out.append((gw, min(owner_seen, last_ok) if (owner_seen and last_ok) else (owner_seen or last_ok)))
    return tuple(out)


def _project_replica(path: Path, now: float, me: str, rows: list, info_rows: list, last_ok: float | None,
                     offline: float | None, *, qualified_out: set | None = None, owners_out: set | None = None,
                     horizon_out: list[float] | None = None,
                     seen_out: dict[str, float] | None = None,
                     moved_out: dict | None = None) -> list[dict[str, Any]]:
    """``_read_remote_links`` of rows already read. ``horizon_out`` collects every wall time after ``now`` at which
    the same rows could project differently (hub-sync staleness, scope-heartbeat freshness)."""
    stale = offline is not None or last_ok is None or now - last_ok > HUB_STALE_AFTER_S
    heartbeats = _scope_heartbeats(info_rows)
    if horizon_out is not None:
        cutoffs = [beat + d for beat, window in heartbeats.values() for d in (0.0, window)]
        if not stale:
            cutoffs.append(last_ok + HUB_STALE_AFTER_S)
        horizon_out.extend(t for t in cutoffs if t > now)
    tasks: dict[tuple[str, str | None, str, str], dict[str, Any]] = {}
    raw: dict[tuple[str, str | None, str, str], dict[str, Any]] = {}
    released: set[tuple[str, str | None, str, str]] = set()
    seen_at: dict[str, float] = {}
    legacy_records: dict[tuple, dict] = {}
    global _replica_decoded
    previous_decoded, decoded = _replica_decoded, {}
    for gw, record_key, data, updated, version in rows:
        memo_key = (str(path), str(gw), str(record_key))
        hit = previous_decoded.get(memo_key)
        row = hit[1] if hit is not None and hit[0] == data else _decode_replica_task(record_key, data)
        decoded[memo_key] = (data, row)
        t, digest, evidence = row
        if isinstance(t, dict) and t.get("id"):
            seen_at[str(gw)] = max(seen_at.get(str(gw), 0.0), (updated or 0) / 1000.0)
            parts = str(record_key).split('/')
            if len(parts) == 3:
                owner_profile, board, tid = parts
            elif len(parts) == 2:
                owner_profile = None
                board, tid = parts
                legacy_records[(str(gw), None, board, tid)] = {
                    'key': record_key, 'version': version, 'digest': digest}
            else:
                raise ValueError('invalid replica task key')
            if not all(parts) or tid != str(t['id']):
                raise ValueError('replica task identity mismatch')
            key = (str(gw), owner_profile, board, tid)
            raw[key] = t
            if _owner_attests_terminal(t, str(gw), owner_profile, board, heartbeats, now, stale, evidence):
                released.add(key)
            elif owner_profile is None or t.get('status') not in LISTED_STATUSES:
                # Legacy links carry no authoritative release receipt. Preserve
                # terminal/unknown reports even on cold start; only owner-attested
                # terminal evidence or the qualified ledger projection supersedes it.
                t = {**t, 'reported_status': t.get('status'), 'status': 'unknown', 'unfinished': True}
            tasks[key] = t
    _replica_decoded = decoded
    if seen_out is not None:
        seen_out.update(seen_at)
    if owners_out is not None and not stale:
        # A fresh replica that still carries the owner row is authoritative about its links: a remembered
        # (session, card) pair absent from it is a dropped link, not an unreadable replica.
        owners_out.update((gw, board, tid) for gw, _, board, tid in tasks)
    if moved_out is not None and not stale:
        moved_out.update(_moved_evidence(tasks, heartbeats, now))
    blank_before = {key for key in tasks if key[1] is None}
    _fold_legacy_aliases(tasks, raw)
    # Only a proven-identity alias is the same card as its qualified twin (folded above, links carried
    # over). An unproven or ambiguous alias stays a separate ``unknown`` obligation: absence is never release.
    if qualified_out is not None:
        qualified_out.update((gw, board, tid) for gw, _, board, tid in blank_before - set(tasks))
    from hermes_cli.profiles import get_active_profile_name

    origin_profile = get_active_profile_name()
    found: dict[tuple[str, str], dict[str, Any]] = {}
    for (gw, owner_profile, board, tid), t in tasks.items():
        # Only this exact owner is represented by the local board inventory.
        # Other profiles on this gateway, and unqualified legacy owners, are not.
        if gw == me and owner_profile == origin_profile:
            continue
        links = t.get("sessions") if isinstance(t.get("sessions"), list) else []
        sids = {str(link['session']) for link in links
                if isinstance(link, dict) and link.get('session') and link.get('gw') == me
                and link.get('profile') == origin_profile
                and link.get('rel') in ('shepherd', 'cross_task', 'create-task', 'adopt-task')}
        if not sids:
            continue
        owner_seen = seen_at.get(gw) or None
        synced = min(owner_seen, last_ok) if (owner_seen and last_ok) else (owner_seen or last_ok)
        # Observation scope: the linked card only, never its dependency descendants.
        closed = (gw, owner_profile, board, tid) in released
        prefix = f'{gw}/{owner_profile}/{board}' if owner_profile is not None else f'{gw}/{board}'
        ref = f'{prefix}/{tid}'
        for sid in sids:
            found[(sid, ref)] = {
                **{f: t.get(f) for f in _REMOTE_FIELDS},
                "id": tid, "board": board, "gateway": gw, "key": ref, "session": sid,
                "via": "shepherd", "root": ref, "remote": True,
                "profile": owner_profile, "owner_profile": owner_profile,
                "origin_gateway": me, "origin_profile": origin_profile,
                "ownership_state": 'unknown' if owner_profile is None else 'qualified',
                "unfinished": not closed and (t.get("status") in LISTED_STATUSES or bool(t.get("unfinished"))),
                "released": closed,
                **({"closed_uncompleted": True} if closed and t.get("status") == "archived"
                   and _owner_evidence_of(t).get("verified") is not True else {}),
                "reported_status": t.get('reported_status', t.get('status')),
                "synced_at": synced, "stale": stale,
                "legacy_record": legacy_records.get((gw, owner_profile, board, tid)),
                "legacy_key": t.get('_legacy_key'),
                # [daveey] What the card waits on is recomputed at read time from these (``_remote_wait``).
                "park": _park_of(t),
                **{k: t[k] for k in ("awaiting", "wake_check") if t.get(k)},
            }
    return list(found.values())


def _owner_evidence_of(task: dict[str, Any]) -> dict[str, Any]:
    """The hash-bound owner observation of a replica record, or {} (``kanban_origin_ledger._owner_evidence``)."""
    from hermes_cli.kanban_origin_ledger import _owner_evidence
    return _owner_evidence(task) if isinstance(task, dict) else {}


def _park_of(task: dict[str, Any]) -> dict[str, Any] | None:
    """[daveey] The bounded run fields a replicated card's wait info needs (``kanban_wait_info.park_fields``)."""
    try:
        from hermes_cli import kanban_wait_info as kwi
        return kwi.park_fields(task.get("run"))
    except Exception:  # display-only
        return None


def _replica_paths() -> list[Path]:
    """The board replicas the counts read when ``kanban.sync.links_reader`` is ``replica`` (else none), so a pull
    that changes a chat's cards invalidates the cached counts like a local board write does. [daveey]"""
    try:
        from hermes_cli import kanban_db_sync as _sync
        if _sync.sync_config().get("links_reader") != "replica":
            return []
        from hermes_cli import kanban_sync_client as _ksc
        return [_ksc.shadow_path(slug) for slug, _path in _ksc.board_paths()]
    except Exception:  # health: allow BLE001 -- a fingerprint input; the TTL still bounds a missed change
        return []


def _activity_fingerprint(*, include_shadows: bool = True) -> tuple:
    """Count inputs include sync shadows; the origin-ledger projection never reads them."""
    pinned = getattr(_revalidate.inputs_pin, "fingerprints", None)
    if pinned is not None and pinned[int(include_shadows)] is not None:
        return pinned[int(include_shadows)]
    hub = _hub_db_path()
    signature = [_hub_content_signature(hub)] if hub is not None else []
    # [daveey] Each board's content revision (``kanban_activity_revision``), not its stat: bookkeeping writes
    # (sync stamps, dispatch stamps, a replica's pull stamp) rewrite the files ~61x/min with no task change.
    signature.extend((str(db), _board_content_key(db)) for db in _board_db_paths())
    if include_shadows:
        signature.extend(_shadow_fingerprint())
    return tuple(signature)


def _shadow_fingerprint() -> tuple:
    """What ``_activity_fingerprint()`` appends to the local one: each sync shadow's content key."""
    return tuple((str(db), _board_content_key(db, _revision.SHADOW_TABLES)) for db in _replica_paths())


def _board_content_key(path: Path, tables: tuple[str, ...] = _revision.LOCAL_TABLES) -> tuple:
    """Changes on every write to a row the activity readers read in ``path`` (stat fallback without the counter, or
    at once while the board is busy: this probe precedes the real read and must never spend its busy budget)."""
    return _revision.content_key(path, tables, lambda p: cached_ro(p, busy_budget=0))


def _without_local_open_duplicates(rows: list[dict[str, Any]], *, gateway: str,
                                   profile: str) -> list[dict[str, Any]]:
    """Prefer exact local open associations, without inferring receipt release.

    Terminal/missing tasks and receipt-only aliases remain obligations. This is
    display deduplication only: it never updates or deletes the durable ledger.
    """
    local = [r for r in rows if (r.get('gateway'), r.get('profile')) == (gateway, profile)]
    if not local:
        return rows
    boards = {_board_slug(p): p for p in _board_db_paths()}
    represented = set()
    # Receipt fan-out often repeats the same board/session. Read each alias once
    # per projection, without extending freshness or sharing across profiles.
    task_ids: dict[tuple[Path, str], set[str]] = {}

    def contains(path: Path, sid: str, task_id: str) -> bool:
        key = (path, sid)
        if key not in task_ids:
            task_ids[key] = {t['id'] for t in _busy.session_tasks(path, [sid], LISTED_STATUSES, _read_session_tasks)}
        return task_id in task_ids[key]

    for row in local:
        path = boards.get(row['board'])
        if path is None:
            continue
        aliases = {row['session'], row['origin_session']}
        # Require coverage of every receipt alias, not just a matching task id.
        if all(contains(path, sid, row['id']) for sid in aliases):
            represented.add((row['key'], row['session'], row['origin_session']))
    return [row for row in rows
            if (row['key'], row['session'], row['origin_session']) not in represented]


_links_lock = threading.Lock()
# (expires, scope, fingerprint, cards, origin rows): origin rows are the ledger ``project()`` rows the cards came
# from, released ones included (``remote_links_and_origin_rows``).
_links_cache: tuple[float, tuple, tuple, list[dict[str, Any]], list[dict[str, Any]]] | None = None
_origin_rows_sink = threading.local()  # .rows: set by _project_remote_links for _store_links


# The links projection's fingerprint covers every store it reads except state.db (compression alias tips), so its
# max age only bounds how late a rotated alias tip shows. Re-projecting every _CACHE_TTL_S (3 s) for that alone cost
# a busy Desktop backend ~10% of a core. [daveey]
_LINKS_MAX_AGE_S = 15.0


def _links_fingerprint() -> tuple:
    """Every store the remote inventory reads except state.db (alias tips), which the TTL bounds instead."""
    from hermes_constants import get_hermes_home

    ledger = get_hermes_home() / 'kanban' / 'origin-ledger.db'
    signature = list(_activity_fingerprint(include_shadows=False))
    for path in (ledger, Path(str(ledger) + '-wal')):
        try:
            st = path.stat()
            signature.append((str(path), st.st_ino, st.st_size, st.st_mtime_ns))
        except FileNotFoundError:
            signature.append((str(path), None))
    return tuple(signature)


def remote_links_and_origin_rows() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """[daveey] ``(_remote_links(), origin rows)`` from one cached projection: the origin rows are the ledger
    ``project()`` rows behind the cards, released rows included (the cards omit those). Plugins that need released
    rows (chat-closeout) take them here instead of projecting the ledger again on every pass."""
    return _links_entry(False, with_rows=True)


def _remote_links(stale_ok: bool = False) -> list[dict[str, Any]]:
    """Shared origin-ledger projection, with preserved legacy associations.

    One projection per scope serves every reader (counts, session task lists, plugins) for ``_LINKS_MAX_AGE_S`` while
    no store it reads has changed. Single-flight: concurrent readers wait for the running projection and share it
    instead of each rebuilding. Failures are never cached.

    ``stale_ok`` (the Desktop linked-card strip only): never wait for a rebuild once a projection exists. A reader
    gets the previous projection at once and one background rebuild replaces it, so a busy board (workers writing
    every few seconds) cannot hold the strip behind the projection lock. Every other reader keeps read-your-writes:
    a release or association it just wrote is in the answer."""
    return _links_entry(stale_ok)


def _links_entry(stale_ok: bool, *, with_rows: bool = False):
    def out(entry):
        return (list(entry[3]), list(entry[4])) if with_rows else list(entry[3])

    scope = _origin_scope()
    # [daveey] Inside ``one_annotation_snapshot`` every reader (the counts, then a plugin's waiting pass) shares the
    # first projection: a board WAL moving between them (they all do, every few seconds) would project again.
    pinned = getattr(_annotation_scope, "links", None)
    if pinned is not None and pinned[1] == scope:
        return out(pinned)
    entry = _links_lookup(scope, stale_ok)
    if getattr(_annotation_scope, "reads", None) is not None:
        _annotation_scope.links = entry
    return out(entry)


def _links_lookup(scope: tuple, stale_ok: bool) -> tuple:
    fingerprint = _links_fingerprint()
    hit = _links_cache
    if hit is not None and hit[1] == scope:
        fresh = hit[2] == fingerprint and time.monotonic() < hit[0]
        if fresh:
            return hit
        if stale_ok:
            _refresh_links_in_background(scope)
            return hit
    with _links_lock:
        hit = _links_cache
        if hit is not None and hit[1] == scope and hit[2] == fingerprint and time.monotonic() < hit[0]:
            return hit
        if stale_ok and hit is not None and hit[1] == scope:
            return hit
        return _store_links(scope, fingerprint)


def _store_links(scope: tuple, fingerprint: tuple) -> tuple:
    """Project and publish (caller holds ``_links_lock``); returns the published entry. ``fingerprint`` is taken
    before projecting, so a store change during the projection leaves the cache stale and triggers another rebuild."""
    global _links_cache
    _origin_rows_sink.rows = []
    try:
        cards = _project_remote_links()
        rows = _origin_rows_sink.rows
    finally:
        _origin_rows_sink.rows = None
    _links_cache = (time.monotonic() + _LINKS_MAX_AGE_S, scope, fingerprint, cards, rows)
    return _links_cache


_links_refreshing = False
_links_refresh_guard = threading.Lock()


def _refresh_links_in_background(scope: tuple) -> None:
    """At most one background rebuild at a time; readers keep the previous projection meanwhile."""
    global _links_refreshing
    with _links_refresh_guard:
        if _links_refreshing:
            return
        _links_refreshing = True

    def run() -> None:
        global _links_refreshing
        try:
            with _links_lock:
                fingerprint = _links_fingerprint()
                if _origin_scope() == scope:
                    _store_links(scope, fingerprint)
        except Exception:  # health: allow BLE001 -- a failed rebuild keeps the previous projection; readers must not fail
            _log.warning("kanban remote links: background rebuild failed; serving the previous projection", exc_info=True)
        finally:
            with _links_refresh_guard:
                _links_refreshing = False

    threading.Thread(target=run, daemon=True, name="kanban-remote-links").start()


def _project_remote_links() -> list[dict[str, Any]]:
    from hermes_constants import get_hermes_home
    from hermes_cli.kanban_origin_adapter import _gateway
    from hermes_cli.kanban_origin_ledger import project
    from hermes_cli.profiles import get_active_profile_name

    ledger = get_hermes_home() / 'kanban' / 'origin-ledger.db'
    if not ledger.exists():
        return _legacy_remote_links()
    replica = _hub_db_path()
    # Configured identity remains available with no replica; corrupt identity
    # must raise, not project another gateway's receipts.
    missing = get_hermes_home() / 'kanban' / 'absent-replica.db'
    gateway, profile = _gateway(replica or missing), get_active_profile_name()
    rows = project(ledger, replica=replica, gateway=gateway, profile=profile)
    sink = getattr(_origin_rows_sink, "rows", None)
    if sink is not None:
        sink.extend(rows)
    rows = _without_local_open_duplicates(rows, gateway=gateway, profile=profile)
    legacy = _legacy_remote_links()
    # A receipt proves its original and accepted tip aliases. Other aliases
    # require the canonical compression predicate in this origin's state DB.
    # Never lend current-origin provenance to historical cached associations.
    from hermes_cli.kanban_origin_adapter import _resolve_session

    tips: dict[str, str | None] = {}
    state_db = get_hermes_home() / 'state.db'
    tip_conn: list[sqlite3.Connection] = []  # one state.db connection for every lookup in this projection

    def tip(sid: str) -> str | None:
        if sid not in tips:
            try:
                if not tip_conn:
                    from hermes_cli.kanban_origin import _ro
                    tip_conn.append(_ro(state_db))
                tips[sid] = _resolve_session(state_db, sid, tip_conn[0])
            except (OSError, sqlite3.Error, ValueError):
                tips[sid] = None
        return tips[sid]

    # [daveey] Both tests below need an exact identity match, so index the receipt rows by it: scanning every
    # row per legacy card was ~2.4k x ~200 ``supersedes`` calls (~45 ms) per projection.
    by_origin: dict[tuple, list[dict[str, Any]]] = {}
    by_key: dict[tuple, list[dict[str, Any]]] = {}
    if legacy:
        for row in rows:
            by_origin.setdefault((row['gateway'], row['origin_gateway'], row['origin_profile'],
                                  row['origin_session']), []).append(row)
            by_key.setdefault((row['key'], row['origin_gateway'], row['origin_profile']), []).append(row)

    def superseded(card: dict[str, Any]) -> bool:
        from hermes_cli.kanban_origin_legacy import supersedes
        origin = (card.get('gateway'), card.get('origin_gateway'), card.get('origin_profile'), card.get('session'))
        if any(supersedes(card, row) for row in by_origin.get(origin, ())):
            return True
        for row in by_key.get((card.get('key'), card.get('origin_gateway'), card.get('origin_profile')), ()):
            aliases = (row['origin_session'], row['session'])
            if card['session'] in aliases:
                return True
            resolved = tip(card['session'])
            if resolved is not None and any(resolved == tip(sid) for sid in aliases):
                return True
        return False

    # Unknown/stale receipt rows replace duplicates, not obligations: they are
    # still unfinished. Only project() can authorize a verified live release.
    try:
        return [card for card in legacy if not superseded(card)] + [
            row for row in rows if row['unfinished']]
    finally:
        for conn in tip_conn:
            _close_quietly(conn)


def _legacy_remote_links() -> list[dict[str, Any]]:
    """``_read_remote_links`` of this gateway's replica. A failed read (corrupt/locked/vanished hub.db) is not
    "no remote work": it returns the last good answer with every row marked stale, so a session never paints
    done because the replica could not be read. No replica ever seen = not installed = []."""
    global _remote_last, _remote_scope, _snapshot_saved
    scope = _origin_scope()
    with _cache_lock:
        previous = _remote_last if _remote_scope == scope else None
    if previous is None:
        previous = _remote_snapshot()
    path = _hub_db_path()
    try:
        if path is None:
            raise FileNotFoundError("no hub.db")
        qualified: set[tuple[str, str, str]] = set()
        owners: set[tuple[str, str, str]] = set()
        moved: dict[str, Any] = {}
        cards = _read_remote_links(path, qualified_out=qualified, owners_out=owners, moved_out=moved)
    except Exception as exc:  # a broken replica must never break the sidebar
        if previous is None:
            if path is not None:
                # Cold-start failures have no safe last-known answer. Propagate so the
                # row annotator marks unknown and the task API fails, rather than []
                # masquerading as a successful empty work list.
                raise
            return []
        _log.warning("hermes-hub replica read failed, keeping last known remote cards (stale): %s", exc)
        return [{**c, "stale": True} for c in previous]
    # A folded legacy alias (``legacy_key``) is present under its qualified key: its last-known
    # unqualified row must not be resurrected as a stale obligation.
    present = {(c["session"], k) for c in cards for k in (c["key"], c.get("legacy_key")) if k}
    # Likewise a remembered unqualified row whose profile-qualified twin is live now: the replica no longer
    # carries the legacy record, so the snapshot's copy is a ghost of a card the owner already publishes (and
    # may attest closed) under its qualified key. [daveey]
    qualified_now = {(c["session"], c.get("gateway"), c.get("board"), c.get("id"))
                     for c in cards if c.get("ownership_state") == "qualified"}
    cards.extend({**c, "stale": True} for c in (previous or [])
                 if (c["session"], c["key"]) not in present
                 # Successful reads migrate graph-inferred cache entries only.
                 # Receipt-backed obligations remain in the separate ledger.
                 and c.get("via") != "descendant"
                 # Fresh replica + owner row present but this session's link gone: expire the ghost.
                 and (c.get("gateway"), c.get("board"), c.get("id")) not in owners
                 and not (c.get("ownership_state") == "unknown"
                          and (c["session"], c.get("gateway"), c.get("board"), c.get("id")) in qualified_now)
                 # Fresh replica: the named owner publishes the scope without it, another gateway owns the card.
                 and not _retire_moved(c, moved))
    if _snapshot_saved != (scope, cards):
        # The durable snapshot only speeds a cold start. Losing a write race (database is locked) must not
        # fail this read: the sidebar would mark every row kanban_unknown. The next changed read retries.
        try:
            _remote_snapshot(cards)
            _snapshot_saved = (scope, [dict(c) for c in cards])
        except sqlite3.Error as exc:
            _log.warning("origin-work snapshot not saved; retrying on the next change: %s", exc)
    with _cache_lock:
        _remote_scope = scope
        _remote_last = cards
    return cards


_cached_count_result = _count_scopes.result_locked


def _all_counts(*, include_waiting: bool = False, include_snapshot: bool = False) -> tuple:
    """Cached counts, optionally with waiting IDs from the same scoped snapshot.

    Single flight: one caller recomputes, and every caller that queued behind it takes that result. The
    fingerprint moves with every kanban write, so without this each queued sidebar request recomputed in
    turn behind ``_pairs_lock``; under load the Desktop's 4 s retries outran it and ~24 threadpool workers
    sat in this function, starving ``/api/status`` (2026-10-27). [daveey]"""
    scope = _origin_scope()
    now = time.monotonic()
    wall = time.time()
    fingerprint = _activity_fingerprint()
    with _cache_lock:
        _count_scopes.restore_locked(scope)
        if _revalidate.counts_fresh(scope, fingerprint, now):
            return _cached_count_result(include_waiting, include_snapshot)
        seen = _counts_generation
    with _counts_flight:
        with _cache_lock:
            if _cache_generation > seen and _cache is not None and _cache_scope == scope:
                return _cached_count_result(include_waiting, include_snapshot)
        return _recompute_all_counts(scope, now, fingerprint, include_waiting=include_waiting,
                                     include_snapshot=include_snapshot, wall=wall)


def _recompute_all_counts(scope, now: float, fingerprint, *, include_waiting: bool,
                          include_snapshot: bool = False, wall: float | None = None) -> tuple:
    """Read every local board and the remote links into fresh counts and publish them to the cache."""
    global _cache, _cache_scope, _cache_fingerprint, _counts_generation, _cache_read_at, _cache_revalidatable
    global _cache_generation
    active: dict[str, int] = {}
    blocked: dict[str, int] = {}
    unseen: dict[str, int] = {}
    stale: dict[str, int] = {}
    parked: dict[str, int] = {}
    held: dict[str, int] = {}
    stalled: dict[str, int] = {}
    _parked_sink.maps = (parked, held)
    _parked_sink.stalled = stalled
    local_keys: dict[str, set[tuple[str, str, bool]]] = {}
    _local_keys_sink.keys = local_keys
    _busy.begin_recompute()
    try:
        pairs = _read_local_pairs_into(active, blocked, unseen)
    finally:
        _parked_sink.maps = None
        _parked_sink.stalled = None
        _local_keys_sink.keys = None
    waiting: dict[str, set[str]] = {}
    for sid, tid in pairs:
        waiting.setdefault(sid, set()).add(tid)
    remote_keys: dict[str, set[tuple[str, str, bool]]] = {}
    for card in _remote_cards(None, LISTED_STATUSES, False):
        bucket = _replica_bucket(card)
        into = active if bucket == "active" else blocked if bucket == "blocked" else None
        if into is None:
            continue
        sid = card["session"]
        into[sid] = into.get(sid, 0) + 1
        # A replica row has no parent links; its owner promotes a todo whose parents finished on the next tick, so a
        # replicated todo waits on a parent (parked). Held needs those links, so remote cards are never held.
        # [daveey] An ``unknown`` card (done, release unverified) has no worker either: it waits like a parked one,
        # or a chat whose other cards are all parked painted no stripe at all instead of waiting.
        is_parked = bucket == "active" and card.get("status") in (*PARKED_STATUSES, "todo", "unknown")
        if is_parked:
            parked[sid] = parked.get(sid, 0) + 1
        remote_keys.setdefault(sid, set()).add((str(card.get("key")), str(bucket), is_parked))
        # Same status test as parked/blocked above: a legacy row whose live status is unknown (reported_status is
        # only its last report) is never counted stalled, so stalled can't exceed what the row shows parked.
        if card.get("status") != "unknown" and (_remote_wait(card) or {}).get("wait_state") == "stalled":
            stalled[sid] = stalled.get(sid, 0) + 1
            remote_keys.setdefault(sid, set()).add((str(card.get("key")), "stalled", False))
        if card.get("stale"):
            stale[sid] = stale.get(sid, 0) + 1
    fresh = ((active, blocked, unseen, stale), {sid: frozenset(ids) for sid, ids in waiting.items()},
             (parked, held), stalled, {sid: frozenset(keys) for sid, keys in remote_keys.items()},
             {sid: frozenset(keys) for sid, keys in local_keys.items()})
    with _cache_lock:
        # [daveey] An unchanged answer keeps the published objects, so the annotation can tell by identity that
        # nothing moved and reuse its frozen copy (``_read_annotation_parts``). Equal values, same answer.
        if _cache is not None and _cache_scope == scope:
            current = (tuple(_cache[1:5]), _waiting_cache[0], _parked_cache[0], _stalled_cache[0],
                       _remote_keys_cache[0], _local_keys_cache[0])
            fresh = tuple(old if old == new else new for old, new in zip(current, fresh))
        counts, waiting_ids, parked_held, stalled, remote_ids, local_ids = fresh
        _cache_scope = scope
        _cache = (now + _CACHE_TTL_S, *counts)
        _cache_read_at = now
        _cache_revalidatable = wall is not None and not _busy.reused() and _revalidate.revalidatable(fingerprint, wall)
        _cache_fingerprint = fingerprint
        _counts_generation += 1
        _cache_generation = _counts_generation
        _waiting_cache[0] = waiting_ids
        _parked_cache[0] = parked_held
        _stalled_cache[0] = stalled
        _remote_keys_cache[0] = remote_ids
        _local_keys_cache[0] = local_ids
        _count_scopes.remember_locked()
        return _cached_count_result(include_waiting, include_snapshot)


def _read_local_pairs_into(active: dict[str, int], blocked: dict[str, int],
                           unseen: dict[str, int]) -> set[tuple[str, str]]:
    """Fill the three count maps from every local board; return the open (session, task) pairs."""
    global _pairs_sink
    with _pairs_lock:
        _pairs_sink = set()
        try:
            _read_local_boards(active, blocked, unseen)
            return _pairs_sink
        finally:
            _pairs_sink = None


# [daveey] Per-board read memo: {board path: (content key, reads)}. A board whose read rows have not changed since
# its last read (``_board_content_key``: bookkeeping writes do not count) holds the same answer, so a write on one
# board re-reads that board only; the other boards cost a stat. Only the stall verdict depends on the clock, so the
# memo keeps its rows and re-judges them on each use. Touched only under ``_pairs_lock``.
_board_memo: dict[str, tuple[tuple, dict[str, Any]]] = {}


def _read_board(path: Path, *, parked: bool, stalled: bool) -> tuple[dict[str, Any], bool]:
    """Every count read of one board, on its shared connection in one read transaction (one snapshot; five fresh
    opens cost ~5 ms on a board with no rows): ``(reads, complete)``. ``complete`` is False when a display-only read
    failed (unseen/stalled log and count nothing, as before); such a read is not memoized, so the next pass retries."""
    global _pairs_sink
    reads: dict[str, Any] = {}
    complete = True
    with cached_ro(path) as conn:
        conn.execute("BEGIN")
        try:
            try:
                reads["unseen"] = _unseen_on(conn)
            except sqlite3.Error as exc:
                _log.debug("kanban unseen read failed for %s: %s", path, exc)
                reads["unseen"] = {}
                complete = False
            outer, _pairs_sink = _pairs_sink, set()
            try:
                reads["active"] = _counts_on(conn, ACTIVE_STATUSES)
                reads["active_pairs"], _pairs_sink = _pairs_sink, set()
                reads["blocked"] = _counts_on(conn, BLOCKED_STATUSES)
                reads["blocked_pairs"] = _pairs_sink
                reads["pairs"] = reads["active_pairs"] | reads["blocked_pairs"]
            finally:
                _pairs_sink = outer
            if parked:
                _parked_sink.pairs = (set(), set())
                try:
                    reads["parked"] = _parked_on(conn)
                    reads["parked_pairs"], reads["held_pairs"] = _parked_sink.pairs
                finally:
                    _parked_sink.pairs = None
            if stalled:
                try:
                    reads["stall_rows"] = _stall_rows_on(conn)
                except Exception as exc:  # display-only: a failed read never fails the counts
                    _log.debug("kanban stalled read failed for %s: %s", path, exc)
                    reads["stall_rows"] = (set(), {}, {})
                    complete = False
        finally:
            conn.execute("COMMIT")
    return reads, complete


def _read_local_boards(active: dict[str, int], blocked: dict[str, int], unseen: dict[str, int]) -> None:
    sink = getattr(_parked_sink, "maps", None)
    stalled = getattr(_parked_sink, "stalled", None)
    paths = _board_db_paths()
    for gone in set(_board_memo) - {str(p) for p in paths}:
        _board_memo.pop(gone, None)
    now = time.time()
    for path in paths:
        key = _revalidate.board_memo_key(path)
        hit = _board_memo.get(str(path))
        reads = hit[1] if hit is not None and hit[0] == key else None
        if reads is not None and ((sink is not None and "parked" not in reads)
                                  or (stalled is not None and "stall_rows" not in reads)):
            reads = None
        if reads is None:  # [daveey] a busy board reuses its last good counts (kanban-359833be53)
            reads, complete, fresh = _busy.board_reads(path, key, hit, sink is not None, stalled is not None, _read_board)
            if fresh:
                _board_memo.pop(str(path), None)
            if fresh and complete:
                _board_memo[str(path)] = (key, reads)
        for src, into in ((reads["unseen"], unseen), (reads["active"], active), (reads["blocked"], blocked)):
            for sid, n in src.items():
                into[sid] = into.get(sid, 0) + n
        if _pairs_sink is not None:
            _pairs_sink.update(reads["pairs"])
        stalled_pairs: set[tuple[str, str]] = set()
        if sink is not None:
            for src, into in zip(reads["parked"], sink):
                for sid, n in src.items():
                    into[sid] = into.get(sid, 0) + n
        if stalled is not None:
            try:
                for sid, n in _stalled_counts(*reads["stall_rows"], now=now, keys=stalled_pairs).items():
                    stalled[sid] = stalled.get(sid, 0) + n
            except Exception as exc:  # display-only: a failed read never fails the counts
                _log.debug("kanban stalled read failed for %s: %s", path, exc)
        keys = getattr(_local_keys_sink, "keys", None)
        if keys is not None:
            for sid, cards in _identity.local_keys(path, {**reads, "stalled_pairs": stalled_pairs},
                                                   board=_board_slug(path)).items():
                keys.setdefault(sid, set()).update(cards)


# [daveey] Extra "this chat still waits" sources (plugins): name -> fn() -> {session_id: iterable of task ids}. A
# chat-closeout plugin registers the cards of a settled batch whose close-out turn has not run yet, so the row
# stays grey "waiting" between the last notice and the closing answer instead of flashing green.
_waiting_sources: dict[str, Any] = {}


def register_waiting_source(name: str, fn) -> None:
    """Register (or replace, ``fn=None`` removes) a plugin source of waiting card ids per session. [daveey]"""
    if fn is None:
        _waiting_sources.pop(name, None)
    else:
        _waiting_sources[name] = fn


def _extra_waiting() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for name, fn in list(_waiting_sources.items()):
        try:
            for sid, ids in (fn() or {}).items():
                out.setdefault(str(sid), set()).update(str(i) for i in ids if i)
        except Exception:  # annotation catches this and stamps unknown, including cold starts
            _log.debug("waiting source %s failed", name, exc_info=True)
            raise
    return out


def waiting_on_task_ids() -> dict[str, frozenset[str]]:
    """``{session_id: open task ids}`` on this gateway's boards: queued/running cards plus those waiting on a human,
    created or shepherded by the session — what the chat is waiting on. Same cache as the counts. [daveey]"""
    return _all_counts(include_waiting=True)[4]


def active_task_counts() -> dict[str, int]:
    """``{session_id: active task count}`` across every kanban board."""
    return _all_counts()[0]


def parked_task_counts() -> tuple[dict[str, int], dict[str, int]]:
    """``({session_id: parked count}, {session_id: held count})``: the subsets of ``active_task_counts`` that no
    worker is running (see ``_read_parked``). Same cache as the counts. [daveey]"""
    _all_counts()
    with _cache_lock:
        return _parked_cache[0]


def stalled_task_counts() -> dict[str, int]:
    """``{session_id: stalled count}``: parked cards whose agent-owned wait passed ``STALL_AFTER_S``. Same cache as
    the counts. [daveey]"""
    _all_counts()
    with _cache_lock:
        return _stalled_cache[0]


def _remote_wait(card: dict[str, Any]) -> dict[str, Any] | None:
    """Wait info of a remote card row, computed now from its fields (never a stored copy, which would freeze the
    age). Stale (last-known) rows get none: they prove nothing about how long a wait has run."""
    if card.get("stale"):
        return None
    try:
        from hermes_cli import kanban_wait_info as kwi
        return kwi.describe_replica(card)
    except Exception:
        return None


def parked_waits(now: float | None = None) -> list[dict[str, Any]]:
    """[daveey] Every parked card a session created or shepherds, local and on other gateways, with its wait info
    (``kanban_wait_info``): ``{session, id, board, key, title, remote, gateway, wait}``. For the owner wake (the
    chat-closeout plugin), which starts a supervise turn in the owning chat when a card parks for an agent-owned
    reason. Local read failures skip that board; a failed remote read skips the remote cards for this
    call (logged), so the local cards still wake."""
    out: list[dict[str, Any]] = []
    for path in _board_db_paths():
        try:
            conn = _open_ro(path)
        except sqlite3.Error:
            continue
        try:
            pairs = _parked_pairs(conn)
            info = _local_wait_info(conn, {tid for _, tid in pairs}, now=now) if pairs else {}
            titles: dict[str, str] = {}
            ids = list(info)
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                titles.update((str(t), str(n or "")) for t, n in conn.execute(
                    f"SELECT id, title FROM tasks WHERE id IN ({','.join('?' for _ in chunk)})", chunk))
        except Exception as exc:
            _log.debug("kanban parked waits read failed for %s: %s", path, exc)
            continue
        finally:
            conn.close()
        board = _board_slug(path)
        for sid, tid in sorted(pairs):
            if tid in info:
                out.append({"session": sid, "id": tid, "board": board, "key": f"{board}/{tid}",
                            "title": titles.get(tid, ""), "remote": False, "gateway": None, "wait": info[tid]})
    try:
        remote = _remote_links()
    except Exception as exc:
        _log.warning("kanban parked waits: remote links unreadable, local cards only this pass: %s", exc)
        remote = []
    for card in remote:
        if card.get("stale"):
            continue
        wait = _remote_wait(card)
        if wait is None:
            continue
        out.append({"session": str(card.get("session")), "id": str(card.get("id")), "board": card.get("board"),
                    "key": str(card.get("key")), "title": str(card.get("title") or ""), "remote": True,
                    "gateway": card.get("gateway"), "wait": wait})
    return out


def blocked_task_counts() -> dict[str, int]:
    """``{session_id: blocked task count}`` across every kanban board."""
    return _all_counts()[1]


# What a session's task strip lists: the sidebar's open + blocked sets, plus
# ``review`` (also waiting on a human). [daveey]
LISTED_STATUSES = tuple(dict.fromkeys(ACTIVE_STATUSES + BLOCKED_STATUSES + ("review",)))
_TASK_FIELDS = ("id", "title", "status", "assignee", "session_id", "created_at", "started_at", "block_kind", "human_request")


def _board_slug(path: Path) -> str:
    return path.parent.name if path.parent.parent.name == "boards" else "default"


def _read_session_tasks(path: Path, ids: list[str], statuses: tuple[str, ...]) -> list[dict[str, Any]]:
    """Tasks on one board that any of ``ids`` created or is a ``tui`` subscriber
    of, in ``statuses`` — the same pairs ``_read_counts`` counts."""
    sid_ph = ",".join("?" for _ in ids)
    st_ph = ",".join("?" for _ in statuses)
    with cached_ro(path) as conn:
        conn.row_factory = sqlite3.Row
        cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
        if "session_id" not in cols:
            return []  # explicitly supported pre-session schema
        select = ", ".join(f"t.{c}" if c in cols else f"NULL AS {c}" for c in _TASK_FIELDS)
        run_start = "NULL"
        if "current_run_id" in cols and conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_runs'").fetchone():
            run_start = "(SELECT r.started_at FROM task_runs r WHERE r.id = t.current_run_id)"
        found: dict[str, dict[str, Any]] = {}
        watch = _not_watch(conn)  # [daveey] a watch park is not the chat's open card (close-out gate)
        for row in conn.execute(
                f"SELECT {select}, {run_start} AS current_run_started_at FROM tasks t "
                f"WHERE t.session_id IN ({sid_ph}) AND t.status IN ({st_ph}) AND {watch}", (*ids, *statuses)):
            found[row["id"]] = {**dict(row), "via": "created"}
        try:
            for row in conn.execute(
                    f"SELECT {select}, {run_start} AS current_run_started_at FROM kanban_notify_subs s "
                    f"JOIN tasks t ON t.id = s.task_id WHERE s.platform = 'tui' AND {_counted_sub_clause(conn)} "
                    f"AND s.chat_id IN ({sid_ph}) AND t.status IN ({st_ph}) AND {watch}", (*ids, *statuses)):
                found.setdefault(row["id"], {**dict(row), "via": "subscribed"})
        except sqlite3.Error:  # board predates notify subscriptions
            pass
        _add_card_details(conn, found)
        try:
            for tid, info in _local_wait_info(conn, found).items():
                found[tid]["wait"] = info
        except Exception as exc:  # display-only: never fails the task list
            _log.debug("kanban wait info read failed for %s: %s", path, exc)
    board = _board_slug(path)
    return [{**t, "board": board} for t in found.values()]


def _remote_cards(ids: list[str] | None, statuses: tuple[str, ...], stale_ok: bool) -> list[dict[str, Any]]:
    """Cards on other gateways linked to these chats (``ids`` None = every chat): from the synced chat links in the
    replicas when ``kanban.sync.links_reader`` is ``replica`` (RFC phase 5), else from the origin-ledger projection.
    The sidebar counts and ``session_tasks`` both read this, so they are one projection in either mode. [daveey]"""
    from hermes_cli import kanban_db_sync as _sync
    if _sync.sync_config().get("links_reader") == "replica":
        from hermes_cli import kanban_chat_links as _links
        from hermes_cli import kanban_sync_client as _ksc
        try:
            gateway = _ksc.load_identity().gateway
        except _ksc.SyncUnavailable:
            gateway = None
        if gateway:
            return _links.replica_cards(ids, gateway, statuses, stale_ok=stale_ok)
    return _remote_links(stale_ok=True) if stale_ok else _remote_links()


def session_tasks(session_ids: Iterable[str], statuses: tuple[str, ...] = LISTED_STATUSES, *,
                  stale_ok: bool = False) -> list[dict[str, Any]]:
    """Every open/blocked/review task across all boards that the given session
    ids (one compression lineage) created or shepherd — exactly the cards the
    sidebar row's ``kanban_active``/``kanban_blocked`` counts cover. Failed
    reads propagate: an unavailable board is not a successful empty task list."""
    ids = [s for s in dict.fromkeys(str(s) for s in session_ids if s)]
    if not ids:
        return []
    out: list[dict[str, Any]] = []
    for path in _board_db_paths():
        out.extend(_busy.session_tasks(path, ids, statuses, _read_session_tasks))  # busy board: last list, stale
    for t in out:
        t.setdefault("key", f"{t['board']}/{t['id']}")
    # [daveey] Cards on other gateways this lineage owns: the same rows the counts use, owner-qualified
    # (gateway/board/id), with ``stale``/``synced_at`` from the replica. One row per card.
    wanted, seen = set(ids), set()
    for card in _remote_cards(ids, statuses, stale_ok):
        belongs = card['session'] in wanted or card.get('origin_session') in wanted
        listed = card.get('status') in statuses or (card.get('status') == 'unknown' and card.get('unfinished'))
        if belongs and listed and card['key'] not in seen:
            seen.add(card["key"])
            row = {**card, "session_id": card["session"]}
            wait = _remote_wait(card)
            if wait is not None:
                row["wait"] = wait
            out.append(row)
    return out


# [daveey] A "done" card can still need the user: its handoff admits part of the
# work was dropped, deferred or never run, and no follow-up card carries it.
# Core has no opinion on what counts; plugins register a check
# (``register_done_check``) that gets each recently completed card and returns
# True when its done is suspect. Rows of sessions with such a card get
# ``kanban_attention`` (count) and paint amber instead of green. Nothing
# registered = no field, no extra reads (upstream behaviour).
ATTENTION_WINDOW_S = 24 * 3600
_ATTENTION_TTL_S = 30.0
# [daveey] Past the TTL the counts stay valid while the local boards' content revisions are unchanged, up to this age
# (it bounds the time-derived ATTENTION_WINDOW_S edge). See kanban_session_activity_attention.
_ATTENTION_MAX_AGE_S = 300.0
_done_checks: dict[str, Callable[[dict[str, Any]], bool]] = {}
# [daveey] Per origin scope, with done-check verdicts shared across scopes (see the module).
_attention = _AttentionCache(_ATTENTION_TTL_S, max_age_s=_ATTENTION_MAX_AGE_S)


def register_done_check(name: str, fn: Callable[[dict[str, Any]], bool]) -> Callable[[], None]:
    """Register ``fn(card) -> bool`` (True = this completed card needs attention). ``card`` has ``id``, ``title``,
    ``summary`` (latest run summary, else ``result``), ``completed_at``, ``board`` and ``board_path``. Re-registering a
    name replaces it; the returned callable unregisters."""
    _done_checks[name] = fn
    _attention.clear()

    def _dispose() -> None:
        if _done_checks.get(name) is fn:
            _done_checks.pop(name, None)
            _attention.clear()

    return _dispose


def _read_recent_done(path: Path, since: float) -> list[tuple[str, dict[str, Any]]]:
    """``(session_id, card)`` for done cards completed since ``since`` that a session created or shepherds."""
    return _recent_done.read_recent_done(path, since, _counted_sub_clause, _board_slug)


def attention_task_counts(now: float | None = None) -> dict[str, int]:
    """``{session_id: completed cards a registered check flags}`` over the last ``ATTENTION_WINDOW_S``. Cached;
    a check that raises counts as False."""
    if not _done_checks:
        return {}
    scope = _origin_scope()
    mono = time.monotonic()
    hit = _attention.get(scope, mono)
    if hit is not None:
        return hit
    inputs = _revalidate.attention_key()
    hit = _attention.get(scope, mono, inputs)
    if hit is not None:
        return hit
    since = (time.time() if now is None else now) - ATTENTION_WINDOW_S
    checks = list(_done_checks.values())
    paths = list(_board_db_paths())
    boards = tuple(str(p) for p in paths)
    pairs: set[tuple[str, str]] = set()
    for path in paths:
        try:
            recent = _read_recent_done(path, since)
        except sqlite3.Error as exc:
            _log.debug("kanban attention read failed for %s: %s", path, exc)
            continue
        for sid, card in recent:
            key = (boards, str(path), str(card["id"]), card.get("completed_at"), card.get("title"), card.get("summary"))
            if _attention.verdict(key, mono, lambda card=card: _run_done_checks(checks, card)):
                pairs.add((sid, key[2]))
    counts: dict[str, int] = {}
    for sid, _tid in pairs:
        counts[sid] = counts.get(sid, 0) + 1
    _attention.put(scope, mono, counts, inputs)
    return counts


def _read_annotation_parts() -> tuple:
    (active, blocked, unseen, remote_stale, waiting, parked, held, stalled,
     remote_keys) = _all_counts(include_snapshot=True)
    extra = _extra_waiting()
    try:
        attention = attention_task_counts()
    except Exception:
        _log.debug("kanban attention unavailable", exc_info=True)
        attention = {}
    raw = (active, blocked, unseen, remote_stale, waiting, parked, held, stalled, remote_keys, extra, attention)
    # [daveey] Nothing moved since the last read of this scope: return that read's frozen copy instead of freezing an
    # equal one. Safe: frozen parts are never mutated (every poll already shares one), and ``==`` per part (sets and
    # frozensets compare by members) proves the copy is exactly what freezing ``raw`` would build. The same object
    # also lets ``_annotation_copy.published`` skip its deep comparison.
    scope = _origin_scope()
    previous = _frozen_parts.get(scope)
    if previous is not None and all(old is new or old == new for old, new in zip(previous, raw)):
        return previous
    frozen = _identity.freeze_annotation(raw)
    _frozen_parts[scope] = frozen
    return frozen


# [daveey] Per origin scope, the last frozen annotation parts (``_read_annotation_parts``).
_frozen_parts: dict[tuple[str, ...], tuple] = {}


def _refresh_annotation_locked(scope: tuple[str, ...], read_at: float | None = None) -> tuple:
    """Read this scope's parts and publish them. Caller holds ``_annotate_refresh_lock``."""
    global _annotate_flight_scope, _annotate_flight_result
    _annotate_flight_scope = scope
    _annotate_flight_result = None
    read_at = time.time() if read_at is None else read_at
    parts = _read_annotation_parts()
    _bounded.record(scope, parts, read_at)
    _last_annotation[scope] = parts
    _annotate_flight_result = parts
    _annotation_copy.published(scope, parts)  # [daveey] a restart serves it while its first read runs
    return parts


def _annotation_snapshot() -> tuple | None:
    """One in-flight annotation read, shared by overlapping polls.

    Returns the last good parts when a refresh is already running, so those polls
    do not occupy an AnyIO worker for the whole waiting-source and attention pass.
    A cold start waits only its refresh budget, then returns None (unknown).
    Completed failures raise their own error; neither case invents counts.

    [daveey] One server process serves several profiles and the result is per scope.
    A cold poll that waited behind another scope's read, or behind a read that failed,
    used to get None and mark every row unknown (kanban-d0ba67426d). It now reads its
    own scope within the remaining budget; overlapping same-scope polls share one read.
    """
    global _annotate_waiters
    scope = _origin_scope()
    deadline = time.monotonic() + _REFRESH_WAIT_S
    if not _annotate_refresh_lock.acquire(blocking=False):
        _annotate_waiters += 1
        try:
            stale = _last_annotation.get(scope)
            stale = stale or _annotation_copy.seed(scope, _last_annotation, _identity.freeze_annotation)
            if stale is not None:
                return stale
            if not _annotate_refresh_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
                return None
        finally:
            _annotate_waiters -= 1
        cached = (_annotate_flight_result if _annotate_flight_scope == scope
                  else None)
        cached = cached if cached is not None else _last_annotation.get(scope)
        if cached is not None:
            _annotate_refresh_lock.release()
            return cached
        # No usable result: transfer lock ownership to a bounded read of this scope.
        return _bounded.refresh_bounded(lambda: _refresh_annotation_locked(scope), _annotate_refresh_lock,
                                        None, max(0.0, deadline - time.monotonic()))
    previous = _last_annotation.get(scope)
    if previous is None:
        # [daveey] An uncontended cold read (a fresh process such as the session-idle-check collector, or a
        # restarted backend before any poll overlapped) passed no fallback, so one read slower than the budget
        # marked every row unknown even with a recent copy on disk: ~400 hub rows under load 24
        # (t_01a19eee6379cea9). Serve that copy, dated by its own time, when the read overruns.
        fallback = _annotation_copy.seed(scope, _last_annotation, _identity.freeze_annotation)
        return _bounded.refresh_bounded(lambda: _refresh_annotation_locked(scope), _annotate_refresh_lock,
                                        fallback, max(0.0, deadline - time.monotonic()))
    try:
        parts = _revalidate.refresh_if_unchanged(scope, _refresh_annotation_locked)
    except BaseException:
        _annotate_refresh_lock.release()
        raise
    if parts is not None:
        _annotate_refresh_lock.release()
        return parts
    return _bounded.refresh_bounded(lambda: _refresh_annotation_locked(scope), _annotate_refresh_lock, previous,
                                    _REFRESH_WAIT_S)


annotation_read_at = _bounded.read_at


# [daveey] Set by ``one_annotation_snapshot``: the reads the first annotation in the scope made, for the others.
_annotation_scope = threading.local()


@contextlib.contextmanager
def one_annotation_snapshot() -> Iterator[None]:
    """[daveey] Every ``annotate_kanban_activity`` call in this block (this thread) stamps from one read of the boards,
    the remote links (every ``_remote_links`` reader in the block, plugins included) and the waiting sources. The sidebar annotates its recents, cron and messaging slices in turn;
    a board write between them recomputed every count (~130 ms) up to three times per response, and the slices
    could disagree about the same session. A failed read is not shared: the next call in the block retries."""
    if getattr(_annotation_scope, "reads", None) is not None:
        yield
        return
    _annotation_scope.reads, _annotation_scope.attention, _annotation_scope.links = [], None, None
    try:
        yield
    finally:
        _annotation_scope.reads = _annotation_scope.attention = _annotation_scope.links = None


_mark_kanban_unknown = _identity.mark_unknown


def annotate_kanban_activity(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stamp ``kanban_active``, ``kanban_blocked``, ``kanban_waiting_on`` (local open card ids), ``kanban_unseen`` (finished cards whose result the
    session has not been handed yet) and ``kanban_attention`` (done cards a registered check flags) on each session row
    (summed over its whole compression lineage). Never raises: the list must
    render without them."""
    if not rows:
        return rows
    # Conservative lower bound: counts may come from the short monotonic cache.
    # Never re-date a cached zero as a new owner observation.
    observed_at = time.time() - _CACHE_TTL_S
    shared = getattr(_annotation_scope, "reads", None)
    attention = None
    try:
        if shared:
            *parts, copy_at = shared[0]
            active, blocked, unseen, remote_stale, waiting, parked, held, stalled, remote_keys, extra = parts
        else:
            parts = _annotation_snapshot()
            copy_at = _annotation_copy.observed_at(parts)  # [daveey] a previous process's copy keeps its own date
            if copy_at is None:
                copy_at = annotation_read_at(parts)  # a previous read served while the next one runs keeps its date
            if parts is None:
                # [daveey] Not a read error (those raise with their own traceback): one line, no synthetic
                # traceback for self-heal to chase.
                _log.warning("kanban annotation unavailable (no snapshot); rows marked kanban_unknown")
                return _mark_kanban_unknown(rows)
            (active, blocked, unseen, remote_stale, waiting, parked, held, stalled, remote_keys, extra,
             attention) = parts
            if shared is not None:
                shared.append((active, blocked, unseen, remote_stale, waiting, parked, held, stalled, remote_keys,
                               extra, copy_at))
                _annotation_scope.attention = attention
    except Exception:
        # [daveey] A failed read is "unknown", never "no work": stamp ``kanban_unknown`` (the sidebar keeps the
        # row off done) and drop the counts rather than present them as fresh.
        _log.warning("kanban activity unavailable; rows marked kanban_unknown", exc_info=True)
        return _mark_kanban_unknown(rows)
    if attention is None:
        attention = getattr(_annotation_scope, "attention", None)
    if attention is None:
        try:
            attention = attention_task_counts()
        except Exception:
            _log.debug("kanban attention unavailable", exc_info=True)
            attention = {}
        if shared is not None:
            _annotation_scope.attention = attention
    observed_at = observed_at if copy_at is None else min(observed_at, copy_at)
    for row in rows:
        row["kanban_observed_at"] = observed_at
        row.pop("kanban_unknown", None)
        ids = list(_identity.row_ids(row))
        dup = _identity.lineage_duplicates(remote_keys, ids)
        for key, counts in (("kanban_active", active), ("kanban_blocked", blocked), ("kanban_unseen", unseen),
                            ("kanban_attention", attention), ("kanban_remote_stale", remote_stale)):
            n = sum(counts.get(sid, 0) for sid in ids) - dup.get(key, 0)
            if n:
                row[key] = n
            else:
                row.pop(key, None)
        # [daveey] The open card ids the chat waits on (lineage-wide, capped): the sidebar's "waiting on N cards".
        open_ids = {tid for sid in ids for tid in waiting.get(sid, ())}
        # Plugin sources (pending close-outs) must not recount local OR scoped remote obligations.
        pending, extra_ids = _identity.closeout_extra(open_ids, {t for sid in ids for t in extra.get(sid, ())},
                                                      [remote_keys.get(sid, ()) for sid in ids])
        if pending:
            row["kanban_active"] = int(row.get("kanban_active") or 0) + len(pending)
        # [daveey] Parked/held are subsets of kanban_active. kanban_parked is stamped (even 0) whenever there is open
        # work, so the desktop can tell "none parked" from a backend that predates the field.
        n_parked = sum(parked.get(sid, 0) for sid in ids) - dup.get("kanban_parked", 0)
        n_held = sum(held.get(sid, 0) for sid in ids) - dup.get("kanban_held", 0)
        if row.get("kanban_active"):
            row["kanban_parked"] = n_parked
        else:
            row.pop("kanban_parked", None)
        if n_held:
            row["kanban_held"] = n_held
        else:
            row.pop("kanban_held", None)
        # [daveey] Stalled: parked cards whose agent-owned wait passed 2 h (amber, not grey waiting or red needs-you).
        n_stalled = sum(stalled.get(sid, 0) for sid in ids) - dup.get("kanban_stalled", 0)
        if n_stalled and row.get("kanban_active"):
            row["kanban_stalled"] = n_stalled
        else:
            row.pop("kanban_stalled", None)
        ids_waiting = sorted(open_ids | extra_ids)
        if ids_waiting:
            row["kanban_waiting_on"] = ids_waiting[:WAITING_ON_CAP]
        else:
            row.pop("kanban_waiting_on", None)
    return rows


def _reset_cache_for_tests(keep_remote: bool = False) -> None:
    global _cache, _remote_last, _links_cache, _snapshot_saved, _remote_read_memo
    global _annotate_flight_scope, _annotate_flight_result, _annotate_waiters
    _remote_read_memo = None
    _hub_signature.reset()
    _annotate_flight_scope = None
    _annotate_flight_result = None
    _annotate_waiters = 0
    _last_annotation.clear()
    _frozen_parts.clear()
    _bounded.reset()
    _annotation_copy.reset()
    with _pairs_lock:
        _board_memo.clear()
    _busy.reset()
    with _links_lock:
        _links_cache = None
    with _cache_lock:
        _cache = None
        _count_scopes.reset_locked()
        _attention.clear()
        if not keep_remote:
            _remote_last = None
            _snapshot_saved = None
        _waiting_cache[0] = {}
        _parked_cache[0] = ({}, {})
        _stalled_cache[0] = {}
        _local_keys_cache[0] = {}
        _remote_keys_cache[0] = {}
    with _RO_CACHE_LOCK:
        held = [entry[0] for entry in _RO_CACHE.values()]
        _RO_CACHE.clear()
    for conn in held:
        _close_quietly(conn)
