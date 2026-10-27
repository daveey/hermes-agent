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

import contextlib
import json
import logging
import re
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
_cache_lock = threading.Lock()
_cache: tuple[float, dict[str, int], dict[str, int], dict[str, int], dict[str, int]] | None = None
_cache_scope: tuple[str, ...] | None = None
_cache_fingerprint: tuple = ()
_waiting_cache: list[dict[str, frozenset[str]]] = [{}]
# (parked, held) per session from the same snapshot as ``_cache`` (see ``_read_parked``); the sink is set while
# ``_all_counts`` reads local boards, like ``_pairs_sink``. [daveey]
_parked_cache: list[tuple[dict[str, int], dict[str, int]]] = [({}, {})]
_parked_sink = threading.local()  # .maps: (parked, held) or None; per thread, so concurrent reads never mix
_pairs_lock = threading.Lock()
# _all_counts single flight: held for a whole recompute; the generation bumps when one publishes.
_counts_flight = threading.Lock()
_counts_generation = 0
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
    return f"(t.status IN ({ph}) OR ({parked} AND NOT {human}))", statuses


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
    finally:
        conn.close()
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


def _read_parked(path: Path) -> tuple[dict[str, int], dict[str, int]]:
    """``(parked, held)`` per session over the same (session, task) pairs ``_read_counts`` counts as active.

    A ``todo`` card with every parent finished (or none) is about to be promoted, so it stays live. A ``todo`` card
    whose ancestors include a card blocked on a validated human request is held, not parked. Boards without
    ``task_links`` classify by status alone."""
    conn = _open_ro(path)
    try:
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
    finally:
        conn.close()

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
    return parked, held


# Terminal kinds that produce a visible notice (mirrors tui_gateway.session_notifications; silent kinds such as
# archived/unblocked never leave a notice waiting).
UNSEEN_KINDS = ("completed", "blocked", "gave_up", "crashed", "timed_out", "status")


def _read_unseen(path: Path) -> dict[str, int]:
    """``tui`` subscriptions per chat whose cursor trails a terminal event: a result the session has not been
    handed yet (its backend is gone, so no poller claims it until the unloaded-session sweeper does). [daveey]"""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.5)
    try:
        ph = ",".join("?" for _ in UNSEEN_KINDS)
        rows = conn.execute(
            f"SELECT s.chat_id, COUNT(*) FROM kanban_notify_subs s WHERE s.platform = 'tui' AND s.chat_id != '' "
            f"AND EXISTS (SELECT 1 FROM task_events e WHERE e.task_id = s.task_id AND e.id > s.last_event_id "
            f"AND e.kind IN ({ph})) GROUP BY s.chat_id", UNSEEN_KINDS).fetchall()
    except sqlite3.Error:  # board predates notify subscriptions
        return {}
    finally:
        conn.close()
    return {str(sid): int(n) for sid, n in rows}


# Remote ownership comes from create/adopt receipts in the origin ledger. Replica
# session links observe the linked card only, never its dependency graph. Created
# children inherit their own origin subscription/receipt; dependency children may
# be unrelated downstream work and MUST NOT be walked. Ledger release stays strict.
def _hub_db_path() -> Path | None:
    base = os.environ.get("HERMES_HUB_STATE") or str(Path.home() / ".local" / "state" / "hermes-hub")
    path = Path(base) / "hub.db"
    return path if path.is_file() else None


def _open_ro(path: Path, check_same_thread: bool = True) -> sqlite3.Connection:
    """Read-only open that survives a WAL db whose -wal/-shm were just removed (plain ``mode=ro`` then fails);
    never creates the file."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.5, check_same_thread=check_same_thread)
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
        return conn
    except sqlite3.OperationalError:
        conn = sqlite3.connect(f"file:{path}?mode=rw", uri=True, timeout=0.5, check_same_thread=check_same_thread)
        conn.execute("PRAGMA query_only = ON")
        return conn


# Linked-card reads run on every sidebar refresh across ~16 boards and replicas; a fresh connection pays schema
# parse + WAL-index setup + close each time (~2.5 ms a board, the whole latency budget). The dashboard is long-lived,
# so keep one read-only connection per file, shared across the threadpool (one per thread would hold ~3 fds per
# board per worker). An idle autocommit reader holds no snapshot, so it never blocks writers or checkpoints.
_RO_CACHE: dict[str, tuple[sqlite3.Connection, tuple[int, int], threading.Lock]] = {}
_RO_CACHE_LOCK = threading.Lock()


@contextlib.contextmanager
def cached_ro(path: Path) -> Iterator[sqlite3.Connection]:
    """A shared read-only connection to ``path``, held exclusively for the ``with`` body. Reopened when the file is
    replaced (new inode: a rebuilt replica) or after any sqlite error. Raises ``sqlite3.OperationalError`` when
    the file is missing, like ``_open_ro``."""
    key = str(path)
    try:
        st = os.stat(path)
    except OSError as exc:
        raise sqlite3.OperationalError(f"unable to open database file {path}: {exc}") from exc
    ident = (st.st_dev, st.st_ino)
    stale = None
    with _RO_CACHE_LOCK:
        entry = _RO_CACHE.get(key)
        if entry is not None and entry[1] != ident:
            stale = _RO_CACHE.pop(key)[0]
            entry = None
        if entry is None:
            entry = (_open_ro(path, check_same_thread=False), ident, threading.Lock())
            _RO_CACHE[key] = entry
    if stale is not None:
        _close_quietly(stale)
    conn, _ident, lock = entry
    with lock:  # bodies read every result they start: an unfinished cursor would pin a WAL snapshot
        conn.row_factory = None
        try:
            yield conn
        except sqlite3.Error:
            with _RO_CACHE_LOCK:
                if _RO_CACHE.get(key) is entry:
                    del _RO_CACHE[key]
            _close_quietly(conn)
            raise


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
    """'active' | 'blocked' | None, by the same rules as ``_status_filter`` for local boards."""
    from hermes_cli.kanban_human_request import needs_human

    status = task.get("status")
    if status == "unknown" and task.get("unfinished"):
        return "active"
    if status in BLOCKED_STATUSES:
        return "blocked" if needs_human(task.get("human_request"), kind=task.get("block_kind")) else "active"
    return "active" if status in ACTIVE_STATUSES else None


# [daveey] The ONE origin-work association for cards on other gateways. The sidebar counts
# (``kanban_active``/``kanban_blocked``, which also drive the archive guard) and the session task list
# (``session_tasks``) both come from ``_remote_links``, so they can never disagree about which remote
# cards a session observes or owns. Each link selects only its exact card. Created
# children count via their own inherited links. Gateway/profile/board/id identifies
# a card; distinct qualified profiles stay separate. Historical unqualified aliases
# do not add another count when a qualified record for the same card is available.
HUB_STALE_AFTER_S = 180.0  # same as the hub-sync plugin: no successful hub sync for this long = stale
# The owner collector publishes each scope heartbeat about every 180s (sawtooth 0..~200s as replicated), so a
# 180s window flapped: a closed card was released half the time. Allow ~2.5 collector periods. [daveey]
SCOPE_HEARTBEAT_FRESH_S = 450.0
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


TERMINAL_STATUSES = ("done", "archived")


def _scope_heartbeats(info_rows: list[tuple[str, str]]) -> dict[tuple[str, str, str], float]:
    """``(gateway, profile, board) -> observed_at`` for every scope its owner collector read cleanly.

    Ambiguous scopes (listed twice) and scopes not in state ``ok`` are absent: they prove nothing."""
    seen: dict[tuple[str, str, str], list] = {}
    for gw, raw in info_rows:
        try:
            info = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(info, dict) or info.get('gateway') != gw or not isinstance(info.get('task_scopes'), list):
            continue
        for scope in info['task_scopes']:
            if isinstance(scope, dict) and isinstance(scope.get('profile'), str) and isinstance(scope.get('board'), str):
                seen.setdefault((str(gw), scope['profile'], scope['board']), []).append(scope)
    return {key: scopes[0].get('observed_at') for key, scopes in seen.items()
            if len(scopes) == 1 and scopes[0].get('state') == 'ok'
            and type(scopes[0].get('observed_at')) in (int, float)}


def _owner_attests_terminal(task: dict[str, Any], gw: str, owner_profile: str | None, board: str,
                            heartbeats: dict[tuple[str, str, str], float], now: float, stale: bool,
                            evidence: dict[str, Any]) -> bool:
    """The owner itself, recently, published this card as closed.

    Release needs all of: a fresh hub sync; a profile-qualified record whose own identity fields match its
    key; a terminal status inside the owner collector's hash-bound observation (``origin_observation``, so a
    replica row edited or truncated in transit proves nothing); and that collector's per-scope heartbeat for
    this exact (profile, board) within ``SCOPE_HEARTBEAT_FRESH_S``, so an offline owner's last word is not taken
    as current. Absence of a row is never release. This is the same rule a local board applies to its own
    cards (``done``/``archived`` are not counted), applied only to evidence the owner signed. Pre-ledger
    cards had no other release path, so every one of them stayed open forever. ``evidence`` is the task's
    ``_owner_evidence`` (``_decode_replica_task``). [daveey]
    """
    if stale or owner_profile is None or task.get('status') not in TERMINAL_STATUSES:
        return False
    if task.get('profile') != owner_profile or task.get('board') != board or task.get('gateway') != gw:
        return False
    if not evidence:
        return False
    # A card bound through the origin ledger keeps the ledger's stricter release: its owner must
    # have verified the completion (lifecycle event, run, contract and goal receipts), not just closed it.
    if task.get('origin_bindings') and evidence.get('verified') is not True:
        return False
    beat = heartbeats.get((gw, owner_profile, board))
    return beat is not None and 0 <= now - beat <= SCOPE_HEARTBEAT_FRESH_S


def _fold_legacy_aliases(tasks: dict[tuple, dict[str, Any]], raw: dict[tuple, dict[str, Any]]) -> None:
    """Merge each unqualified ``board/id`` record into its proven profile-qualified twin, in place.

    Hub-sync used to key records ``board/id``; it now publishes ``profile/board/id`` and the old rows linger,
    so every pre-qualification card was both counted twice and stuck ``unknown`` forever. A legacy record is
    folded only when exactly one profile on the same owner gateway publishes that board/id AND both records
    carry the same non-null ``created_at`` and ``created_by`` (identity, not an ID match). Its shepherd links
    move onto the twin, so no session loses the association; anything ambiguous stays a separate ``unknown``
    obligation. [daveey]
    """
    qualified: dict[tuple[str, str, str], list[tuple]] = {}
    for key in tasks:
        gw, owner_profile, board, tid = key
        if owner_profile is not None:
            qualified.setdefault((gw, board, tid), []).append(key)
    for key in [k for k in tasks if k[1] is None]:
        gw, _, board, tid = key
        twins = qualified.get((gw, board, tid), [])
        if len(twins) != 1:
            continue
        old, new = raw[key], raw[twins[0]]
        if (old.get('created_at') is None or old.get('created_at') != new.get('created_at')
                or old.get('created_by') != new.get('created_by')):
            continue
        twin = tasks[twins[0]]
        links = [*(twin.get('sessions') if isinstance(twin.get('sessions'), list) else []),
                 *(link for link in (old.get('sessions') if isinstance(old.get('sessions'), list) else [])
                   if isinstance(link, dict) and link.get('rel') == 'shepherd')]
        tasks[twins[0]] = {**twin, 'sessions': links, '_legacy_key': f'{gw}/{board}/{tid}'}
        del tasks[key]


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


def _read_remote_links(path: Path, now: float | None = None, *,
                       qualified_out: set | None = None) -> list[dict[str, Any]]:
    """Replica cards linked to a session on this gateway, one row per (session, card).

    Open and unproven cards come back ``unfinished``; cards their owner attests closed
    (``_owner_attests_terminal``) come back as explicit ``released`` rows, so a durable snapshot sees them
    as present-and-closed rather than missing."""
    conn = _open_ro(path)
    try:
        me = _self_gateway(conn)
        if not me:
            return []
        # Old replicas can still be displayed, but cannot prove an exact revision.
        version_column = 'version' if any(r[1] == 'version' for r in conn.execute('PRAGMA table_info(replica)')) else 'NULL'
        rows = conn.execute(f"SELECT gw, key, data, updated_at, {version_column} FROM replica WHERE coll = 'tasks'").fetchall()
        info_rows = conn.execute("SELECT gw, data FROM replica WHERE coll = 'gateway' AND key = 'info'").fetchall()
        last_ok, offline = _meta_float(conn, "last_ok"), _meta_float(conn, "offline_since")
    finally:
        conn.close()
    now = time.time() if now is None else now
    stale = offline is not None or last_ok is None or now - last_ok > HUB_STALE_AFTER_S
    heartbeats = _scope_heartbeats(info_rows)
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
                "reported_status": t.get('reported_status', t.get('status')),
                "synced_at": synced, "stale": stale,
                "legacy_record": legacy_records.get((gw, owner_profile, board, tid)),
                "legacy_key": t.get('_legacy_key'),
            }
    return list(found.values())


def _activity_fingerprint() -> tuple:
    """Invalidate on cross-process commits, including WAL writes and board replacement."""
    paths = _board_db_paths()
    hub = _hub_db_path()
    if hub is not None:
        paths.append(hub)
    signature = []
    for db in paths:
        for path in (db, Path(str(db) + "-wal")):
            try:
                st = path.stat()
                signature.append((str(path), st.st_ino, st.st_size, st.st_mtime_ns))
            except FileNotFoundError:
                signature.append((str(path), None))
    return tuple(signature)


def _remote_snapshot(cards: list[dict[str, Any]] | None = None) -> list[dict[str, Any]] | None:
    """Durable last-known origin obligations, separate from the replaceable hub replica.

    A transaction replaces the snapshot atomically; failed writes propagate rather than
    claiming a durable association. This is a projection cache, not a second transport.
    """
    from hermes_cli import kanban_db

    path = kanban_db.kanban_home() / "origin-work.db"
    if cards is None:
        if not path.exists():
            return None
        conn = _open_ro(path)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=0.5)
    try:
        scope = json.dumps(_origin_scope())
        if cards is not None:
            with conn:
                conn.execute("CREATE TABLE IF NOT EXISTS scoped_snapshot (scope TEXT PRIMARY KEY, data TEXT NOT NULL)")
                conn.execute("INSERT OR REPLACE INTO scoped_snapshot VALUES (?, ?)", (scope, json.dumps(cards)))
            return cards
        # Historical snapshots omitted the origin profile. Never adopt or erase
        # their obligations merely because a new scoped projection is available.
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='snapshot'").fetchone():
            old = conn.execute("SELECT data FROM snapshot WHERE id=1").fetchone()
            if old is None or json.loads(old[0]) != []:
                raise ValueError("unqualified origin-work snapshot requires explicit reconciliation")
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='scoped_snapshot'").fetchone():
            return None
        row = conn.execute("SELECT data FROM scoped_snapshot WHERE scope=?", (scope,)).fetchone()
        if row is None:
            return None
        saved = json.loads(row[0])
        if not isinstance(saved, list) or any(not isinstance(c, dict) or not c.get("session") or not c.get("id") for c in saved):
            raise ValueError("invalid origin-work snapshot")
        return saved
    finally:
        conn.close()


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
            task_ids[key] = {t['id'] for t in _read_session_tasks(path, [sid], LISTED_STATUSES)}
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
_links_cache: tuple[float, tuple, tuple, list[dict[str, Any]]] | None = None  # (expires, scope, fingerprint, cards)


def _links_fingerprint() -> tuple:
    """Every store the remote inventory reads except state.db (alias tips), which the TTL bounds instead."""
    from hermes_constants import get_hermes_home

    ledger = get_hermes_home() / 'kanban' / 'origin-ledger.db'
    signature = list(_activity_fingerprint())
    for path in (ledger, Path(str(ledger) + '-wal')):
        try:
            st = path.stat()
            signature.append((str(path), st.st_ino, st.st_size, st.st_mtime_ns))
        except FileNotFoundError:
            signature.append((str(path), None))
    return tuple(signature)


def _remote_links(stale_ok: bool = False) -> list[dict[str, Any]]:
    """Shared origin-ledger projection, with preserved legacy associations.

    One projection per scope serves every reader (counts, session task lists, plugins) for ``_CACHE_TTL_S`` while
    no store it reads has changed. Single-flight: concurrent readers wait for the running projection and share it
    instead of each rebuilding. Failures are never cached.

    ``stale_ok`` (the Desktop linked-card strip only): never wait for a rebuild once a projection exists. A reader
    gets the previous projection at once and one background rebuild replaces it, so a busy board (workers writing
    every few seconds) cannot hold the strip behind the projection lock. Every other reader keeps read-your-writes:
    a release or association it just wrote is in the answer."""
    scope, fingerprint = _origin_scope(), _links_fingerprint()
    hit = _links_cache
    if hit is not None and hit[1] == scope:
        fresh = hit[2] == fingerprint and time.monotonic() < hit[0]
        if fresh:
            return list(hit[3])
        if stale_ok:
            _refresh_links_in_background(scope)
            return list(hit[3])
    with _links_lock:
        hit = _links_cache
        if hit is not None and hit[1] == scope and hit[2] == fingerprint and time.monotonic() < hit[0]:
            return list(hit[3])
        if stale_ok and hit is not None and hit[1] == scope:
            return list(hit[3])
        return _store_links(scope, fingerprint)


def _store_links(scope: tuple, fingerprint: tuple) -> list[dict[str, Any]]:
    """Project and publish (caller holds ``_links_lock``). ``fingerprint`` is taken before projecting, so a store
    change during the projection leaves the cache stale and triggers another rebuild."""
    global _links_cache
    cards = _project_remote_links()
    _links_cache = (time.monotonic() + _CACHE_TTL_S, scope, fingerprint, cards)
    return list(cards)


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
    rows = _without_local_open_duplicates(rows, gateway=gateway, profile=profile)
    legacy = _legacy_remote_links()
    # A receipt proves its original and accepted tip aliases. Other aliases
    # require the canonical compression predicate in this origin's state DB.
    # Never lend current-origin provenance to historical cached associations.
    from hermes_cli.kanban_origin_adapter import _resolve_session

    tips: dict[str, str | None] = {}

    def tip(sid: str) -> str | None:
        if sid not in tips:
            try:
                tips[sid] = _resolve_session(get_hermes_home() / 'state.db', sid)
            except (OSError, sqlite3.Error, ValueError):
                tips[sid] = None
        return tips[sid]

    def superseded(card: dict[str, Any]) -> bool:
        from hermes_cli.kanban_origin_legacy import supersedes
        for row in rows:
            if supersedes(card, row):
                return True
            if (card.get('key') != row['key']
                    or card.get('origin_gateway') != row['origin_gateway']
                    or card.get('origin_profile') != row['origin_profile']):
                continue
            aliases = (row['origin_session'], row['session'])
            if card['session'] in aliases:
                return True
            resolved = tip(card['session'])
            if resolved is not None and any(resolved == tip(sid) for sid in aliases):
                return True
        return False

    # Unknown/stale receipt rows replace duplicates, not obligations: they are
    # still unfinished. Only project() can authorize a verified live release.
    return [card for card in legacy if not superseded(card)] + [
        row for row in rows if row['unfinished']]


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
        cards = _read_remote_links(path, qualified_out=qualified)
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
                 and not (c.get("ownership_state") == "unknown"
                          and (c["session"], c.get("gateway"), c.get("board"), c.get("id")) in qualified_now))
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


def _all_counts(*, include_waiting: bool = False) -> tuple:
    """Cached counts, optionally with waiting IDs from the same scoped snapshot.

    Single flight: one caller recomputes, and every caller that queued behind it takes that result. The
    fingerprint moves with every kanban write, so without this each queued sidebar request recomputed in
    turn behind ``_pairs_lock``; under load the Desktop's 4 s retries outran it and ~24 threadpool workers
    sat in this function, starving ``/api/status`` (2026-10-27). [daveey]"""
    scope = _origin_scope()
    now = time.monotonic()
    fingerprint = _activity_fingerprint()
    with _cache_lock:
        if _cache is not None and _cache_scope == scope and now < _cache[0] and fingerprint == _cache_fingerprint:
            counts = _cache[1:5]
            return (*counts, dict(_waiting_cache[0])) if include_waiting else counts
        seen = _counts_generation
    with _counts_flight:
        with _cache_lock:
            if _counts_generation != seen and _cache is not None and _cache_scope == scope:
                counts = _cache[1:5]
                return (*counts, dict(_waiting_cache[0])) if include_waiting else counts
        return _recompute_all_counts(scope, now, fingerprint, include_waiting=include_waiting)


def _recompute_all_counts(scope, now: float, fingerprint, *, include_waiting: bool) -> tuple:
    """Read every local board and the remote links into fresh counts and publish them to the cache."""
    global _cache, _cache_scope, _cache_fingerprint, _counts_generation
    active: dict[str, int] = {}
    blocked: dict[str, int] = {}
    unseen: dict[str, int] = {}
    stale: dict[str, int] = {}
    parked: dict[str, int] = {}
    held: dict[str, int] = {}
    _parked_sink.maps = (parked, held)
    try:
        pairs = _read_local_pairs_into(active, blocked, unseen)
    finally:
        _parked_sink.maps = None
    waiting: dict[str, set[str]] = {}
    for sid, tid in pairs:
        waiting.setdefault(sid, set()).add(tid)
    for card in _remote_links():
        bucket = _replica_bucket(card)
        into = active if bucket == "active" else blocked if bucket == "blocked" else None
        if into is None:
            continue
        sid = card["session"]
        into[sid] = into.get(sid, 0) + 1
        # A replica row has no parent links; its owner promotes a todo whose parents finished on the next tick, so a
        # replicated todo waits on a parent (parked). Held needs those links, so remote cards are never held.
        if bucket == "active" and card.get("status") in (*PARKED_STATUSES, "todo"):
            parked[sid] = parked.get(sid, 0) + 1
        if card.get("stale"):
            stale[sid] = stale.get(sid, 0) + 1
    with _cache_lock:
        _cache_scope = scope
        _cache = (now + _CACHE_TTL_S, active, blocked, unseen, stale)
        _cache_fingerprint = fingerprint
        _counts_generation += 1
        _waiting_cache[0] = {sid: frozenset(ids) for sid, ids in waiting.items()}
        _parked_cache[0] = (parked, held)
    counts = (active, blocked, unseen, stale)
    return (*counts, {sid: frozenset(ids) for sid, ids in waiting.items()}) if include_waiting else counts


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


def _read_local_boards(active: dict[str, int], blocked: dict[str, int], unseen: dict[str, int]) -> None:
    sink = getattr(_parked_sink, "maps", None)
    for path in _board_db_paths():
        try:
            for sid, n in _read_unseen(path).items():
                unseen[sid] = unseen.get(sid, 0) + n
        except sqlite3.Error as exc:
            _log.debug("kanban unseen read failed for %s: %s", path, exc)
        for statuses, into in ((ACTIVE_STATUSES, active), (BLOCKED_STATUSES, blocked)):
            for sid, n in _read_counts(path, statuses).items():
                into[sid] = into.get(sid, 0) + n
        if sink is not None:
            board_parked, board_held = _read_parked(path)
            for src, into in zip((board_parked, board_held), sink):
                for sid, n in src.items():
                    into[sid] = into.get(sid, 0) + n


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


def blocked_task_counts() -> dict[str, int]:
    """``{session_id: blocked task count}`` across every kanban board."""
    return _all_counts()[1]


# What a session's task strip lists: the sidebar's open + blocked sets, plus
# ``review`` (also waiting on a human). [daveey]
LISTED_STATUSES = tuple(dict.fromkeys(ACTIVE_STATUSES + BLOCKED_STATUSES + ("review",)))
_TASK_FIELDS = ("id", "title", "status", "assignee", "session_id", "created_at", "started_at")


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
        for row in conn.execute(
                f"SELECT {select}, {run_start} AS current_run_started_at FROM tasks t "
                f"WHERE t.session_id IN ({sid_ph}) AND t.status IN ({st_ph})", (*ids, *statuses)):
            found[row["id"]] = {**dict(row), "via": "created"}
        try:
            for row in conn.execute(
                    f"SELECT {select}, {run_start} AS current_run_started_at FROM kanban_notify_subs s "
                    f"JOIN tasks t ON t.id = s.task_id WHERE s.platform = 'tui' AND {_counted_sub_clause(conn)} "
                    f"AND s.chat_id IN ({sid_ph}) AND t.status IN ({st_ph})", (*ids, *statuses)):
                found.setdefault(row["id"], {**dict(row), "via": "subscribed"})
        except sqlite3.Error:  # board predates notify subscriptions
            pass
        _add_card_details(conn, found)
    board = _board_slug(path)
    return [{**t, "board": board} for t in found.values()]


_PR_URL_RE = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")
_NOTE_CHARS = 200


def _first_line(text: Any) -> str:
    line = str(text or "").strip().split("\n", 1)[0].strip()
    return line[:_NOTE_CHARS]


def _add_card_details(conn: sqlite3.Connection, found: dict[str, dict[str, Any]]) -> None:
    """Per listed card, what the linked-card rows show: the latest event (``activity_kind``/``activity_at``), a
    one-line ``note`` (the newest run summary or comment) and the newest GitHub PR ``pr_url`` found in its
    comments, result or run summaries. One query per table for all listed cards; missing tables are skipped."""
    if not found:
        return
    ids = list(found)
    ph = ",".join("?" for _ in ids)

    def rows(sql: str) -> list[sqlite3.Row]:
        try:
            return conn.execute(sql, ids).fetchall()
        except sqlite3.Error:
            return []

    for r in rows(f"SELECT task_id, kind, MAX(created_at) AS at FROM task_events WHERE task_id IN ({ph}) GROUP BY task_id"):
        found[r["task_id"]].update(activity_kind=r["kind"], activity_at=r["at"])
    notes: dict[str, tuple[Any, str]] = {}
    prs: dict[str, tuple[Any, str]] = {}

    def offer(task_id: str, at: Any, text: Any) -> None:
        at = at or 0
        line = _first_line(text)
        if line and at >= notes.get(task_id, (-1, ""))[0]:
            notes[task_id] = (at, line)
        for url in _PR_URL_RE.findall(str(text or "")):
            if at >= prs.get(task_id, (-1, ""))[0]:
                prs[task_id] = (at, url)

    for r in rows(f"SELECT task_id, COALESCE(ended_at, started_at) AS at, summary FROM task_runs "
                  f"WHERE task_id IN ({ph}) AND summary IS NOT NULL"):
        offer(r["task_id"], r["at"], r["summary"])
    for r in rows(f"SELECT task_id, created_at AS at, body FROM task_comments WHERE task_id IN ({ph})"):
        offer(r["task_id"], r["at"], r["body"])
    for r in rows(f"SELECT id AS task_id, completed_at AS at, result FROM tasks WHERE id IN ({ph}) AND result IS NOT NULL"):
        for url in _PR_URL_RE.findall(str(r["result"] or "")):
            if (r["at"] or 0) >= prs.get(r["task_id"], (-1, ""))[0]:
                prs[r["task_id"]] = (r["at"] or 0, url)
    for task_id, (_, line) in notes.items():
        found[task_id]["note"] = line
    for task_id, (_, url) in prs.items():
        found[task_id]["pr_url"] = url


def _remote_cards(ids: list[str], statuses: tuple[str, ...], stale_ok: bool) -> list[dict[str, Any]]:
    """Cards on other gateways linked to these chats: from the synced chat links in the replicas when
    ``kanban.sync.links_reader`` is ``replica`` (RFC phase 5), else from the origin-ledger projection."""
    from hermes_cli import kanban_db_sync as _sync
    if _sync.sync_config().get("links_reader") == "replica":
        from hermes_cli import kanban_chat_links as _links
        from hermes_cli import kanban_sync_client as _ksc
        try:
            gateway = _ksc.load_identity().gateway
        except _ksc.SyncUnavailable:
            gateway = None
        if gateway:
            return _links.replica_cards(ids, gateway, statuses)
    return _remote_links(stale_ok=stale_ok)


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
        out.extend(_read_session_tasks(path, ids, statuses))
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
            out.append({**card, "session_id": card["session"]})
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
_done_checks: dict[str, Callable[[dict[str, Any]], bool]] = {}
_attention_cache: tuple[float, dict[str, int]] | None = None
_attention_scope: tuple[str, ...] | None = None


def register_done_check(name: str, fn: Callable[[dict[str, Any]], bool]) -> Callable[[], None]:
    """Register ``fn(card) -> bool`` (True = this completed card needs attention). ``card`` has ``id``, ``title``,
    ``summary`` (latest run summary, else ``result``), ``completed_at``, ``board`` and ``board_path``. Re-registering a
    name replaces it; the returned callable unregisters."""
    global _attention_cache
    _done_checks[name] = fn
    _attention_cache = None

    def _dispose() -> None:
        global _attention_cache
        if _done_checks.get(name) is fn:
            _done_checks.pop(name, None)
            _attention_cache = None

    return _dispose


def _read_recent_done(path: Path, since: float) -> list[tuple[str, dict[str, Any]]]:
    """``(session_id, card)`` for done cards completed since ``since`` that a session created or shepherds."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.5)
    conn.row_factory = sqlite3.Row
    try:
        has_runs = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_runs'").fetchone() is not None
        summary = ("COALESCE((SELECT r.summary FROM task_runs r WHERE r.task_id = t.id AND r.summary IS NOT NULL "
                   "AND r.summary != '' ORDER BY r.id DESC LIMIT 1), t.result)") if has_runs else "t.result"
        fields = f"t.id AS id, t.title AS title, t.completed_at AS completed_at, {summary} AS summary"
        cond = "t.status = 'done' AND t.completed_at >= ?"
        pairs = [(str(r["session_id"]), dict(r)) for r in conn.execute(
            f"SELECT t.session_id AS session_id, {fields} FROM tasks t WHERE t.session_id IS NOT NULL "
            f"AND t.session_id != '' AND {cond}", (since,))]
        try:
            pairs.extend((str(r["chat_id"]), dict(r)) for r in conn.execute(
                f"SELECT s.chat_id AS chat_id, {fields} FROM kanban_notify_subs s JOIN tasks t ON t.id = s.task_id "
                f"WHERE s.platform = 'tui' AND s.chat_id != '' AND {_counted_sub_clause(conn)} AND {cond}",
                (since,)))
        except sqlite3.Error:  # board predates notify subscriptions
            pass
    finally:
        conn.close()
    board = _board_slug(path)
    out = []
    for sid, card in pairs:
        card.pop("session_id", None)
        card.pop("chat_id", None)
        out.append((sid, {**card, "board": board, "board_path": str(path)}))
    return out


def attention_task_counts(now: float | None = None) -> dict[str, int]:
    """``{session_id: completed cards a registered check flags}`` over the last ``ATTENTION_WINDOW_S``. Cached;
    a check that raises counts as False."""
    global _attention_cache, _attention_scope
    if not _done_checks:
        return {}
    scope = _origin_scope()
    mono = time.monotonic()
    with _cache_lock:
        if _attention_cache is not None and _attention_scope == scope and mono < _attention_cache[0]:
            return _attention_cache[1]
    since = (time.time() if now is None else now) - ATTENTION_WINDOW_S
    checks = list(_done_checks.values())
    verdicts: dict[tuple[str, str], bool] = {}
    pairs: set[tuple[str, str]] = set()
    for path in _board_db_paths():
        try:
            recent = _read_recent_done(path, since)
        except sqlite3.Error as exc:
            _log.debug("kanban attention read failed for %s: %s", path, exc)
            continue
        for sid, card in recent:
            key = (str(path), str(card["id"]))
            if key not in verdicts:
                flagged = False
                for check in checks:
                    try:
                        if check(card):
                            flagged = True
                            break
                    except Exception:
                        _log.debug("kanban done check failed on %s", card.get("id"), exc_info=True)
                verdicts[key] = flagged
            if verdicts[key]:
                pairs.add((sid, key[1]))
    counts: dict[str, int] = {}
    for sid, _tid in pairs:
        counts[sid] = counts.get(sid, 0) + 1
    with _cache_lock:
        _attention_scope = scope
        _attention_cache = (mono + _ATTENTION_TTL_S, counts)
    return counts


def _row_ids(row: dict[str, Any]) -> Iterable[str]:
    seen: set[str] = set()
    for sid in (row.get("id"), row.get("_lineage_root_id"), *(row.get("_lineage_ids") or ())):
        if sid and sid not in seen:
            seen.add(sid)
            yield sid


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
    try:
        active, blocked, unseen, remote_stale, waiting = _all_counts(include_waiting=True)
        with _cache_lock:  # the snapshot that call just read or wrote: never a second projection
            parked, held = _parked_cache[0]
        extra = _extra_waiting()
    except Exception:
        # [daveey] A failed read is "unknown", never "no work": stamp ``kanban_unknown`` (the sidebar keeps the
        # row off done) and drop the counts rather than present them as fresh.
        _log.warning("kanban activity unavailable; rows marked kanban_unknown", exc_info=True)
        for row in rows:
            for key in ("kanban_active", "kanban_blocked", "kanban_unseen", "kanban_attention", "kanban_remote_stale",
                        "kanban_parked", "kanban_held"):
                row.pop(key, None)
            row.pop("kanban_observed_at", None)
            row.pop("kanban_waiting_on", None)
            row["kanban_unknown"] = True
        return rows
    try:
        attention = attention_task_counts()
    except Exception:
        _log.debug("kanban attention unavailable", exc_info=True)
        attention = {}
    for row in rows:
        row["kanban_observed_at"] = observed_at
        row.pop("kanban_unknown", None)
        ids = list(_row_ids(row))
        for key, counts in (("kanban_active", active), ("kanban_blocked", blocked), ("kanban_unseen", unseen),
                            ("kanban_attention", attention), ("kanban_remote_stale", remote_stale)):
            n = sum(counts.get(sid, 0) for sid in ids)
            if n:
                row[key] = n
            else:
                row.pop(key, None)
        # [daveey] The open card ids the chat waits on (lineage-wide, capped): the sidebar's "waiting on N cards".
        open_ids = {tid for sid in ids for tid in waiting.get(sid, ())}
        # Plugin sources (pending close-outs): count the ids the boards don't already count as open.
        pending = {tid for sid in ids for tid in extra.get(sid, ())} - open_ids
        if pending:
            row["kanban_active"] = int(row.get("kanban_active") or 0) + len(pending)
        # [daveey] Parked/held are subsets of kanban_active. kanban_parked is stamped (even 0) whenever there is open
        # work, so the desktop can tell "none parked" from a backend that predates the field.
        n_parked = sum(parked.get(sid, 0) for sid in ids)
        n_held = sum(held.get(sid, 0) for sid in ids)
        if row.get("kanban_active"):
            row["kanban_parked"] = n_parked
        else:
            row.pop("kanban_parked", None)
        if n_held:
            row["kanban_held"] = n_held
        else:
            row.pop("kanban_held", None)
        ids_waiting = sorted(open_ids | pending)
        if ids_waiting:
            row["kanban_waiting_on"] = ids_waiting[:WAITING_ON_CAP]
        else:
            row.pop("kanban_waiting_on", None)
    return rows


def _reset_cache_for_tests(keep_remote: bool = False) -> None:
    global _cache, _attention_cache, _remote_last, _links_cache, _snapshot_saved
    with _links_lock:
        _links_cache = None
    with _cache_lock:
        _cache = None
        _attention_cache = None
        if not keep_remote:
            _remote_last = None
            _snapshot_saved = None
        _waiting_cache[0] = {}
        _parked_cache[0] = ({}, {})
    with _RO_CACHE_LOCK:
        held = [entry[0] for entry in _RO_CACHE.values()]
        _RO_CACHE.clear()
    for conn in held:
        _close_quietly(conn)
