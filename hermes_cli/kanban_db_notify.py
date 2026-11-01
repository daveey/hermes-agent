"""Notification subscriptions consumed by the gateway kanban-notifier: per-(task, platform, chat, thread) rows with delivery metadata, unseen-event cursors and purge of stale done-task subs.

Split out of ``hermes_cli.kanban_db``; origin-resident helpers are reached
late-bound via ``_kb`` (import-cycle breaking) so monkeypatching
``kanban_db.<name>`` keeps working.
"""

from __future__ import annotations

from hermes_cli import kanban_lockguard as _kanban_lockguard
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any
from typing import Iterable
from typing import Mapping
from typing import Optional
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hermes_cli.kanban_db import Event


# Notifier reaction to a terminal event: "notify" = passive adapter.send only
# (default); "notify+wake" = send AND wake the destination agent; "wake" = wake only.
_NOTIFY_DELIVERY_MODES = ("notify", "notify+wake", "wake")
# Subscription provenance (``delivery_metadata.reason``). Owning subs belong to the chat that created the
# card (``create``: kanban_create's auto-sub; ``origin``: the cross-gateway origin binding) and are inherited
# by its children; observer subs only watch that one card and are not (``kanban_db._inherit_notify_subs``).
# ``shepherd`` = explicit notify-subscribe (incl. hermes-hub's shepherd action), ``touch`` = comment/unblock/link.
# [daveey]
OWNING_SUB_REASONS = ("create", "origin")
OBSERVER_SUB_REASONS = ("shepherd", "touch")

_SCALAR_TYPES = (str, int, float, bool)

# Subscription primary key predicate; every per-row statement below binds
# ``(task_id, platform, chat_id, thread_id or "")`` against it.
_SUB_KEY_WHERE = "WHERE task_id = ? AND platform = ? AND chat_id = ? AND thread_id = ?"


# A ``local-<sha256>`` session id is a headless LOCAL creation receipt (``hermes_state_keys.local_identity``): a
# Kanban worker run, cron job or one-shot, never an interactive chat. Its notices are never read and a wake is
# refused (``session.resume`` 4132 for workers), so it is never a notify destination. [daveey]
_HEADLESS_SESSION = re.compile(r"local-[0-9a-f]{64}")
_HEADLESS_GLOB = "local-" + "[0-9a-f]" * 64


def is_headless_session(chat_id: Any) -> bool:
    """True for a headless receipt session id, bare (``tui``) or ``<gw>/<profile>/<session>`` (``hub``)."""
    return bool(_HEADLESS_SESSION.fullmatch(str(chat_id or "").rsplit("/", 1)[-1]))


def _sub_key(task_id: str, platform: str, chat_id: str, thread_id: Optional[str]) -> tuple:
    return (task_id, platform, chat_id, thread_id or "")


def _encode_notify_delivery_metadata(metadata: Optional[Mapping[str, Any]]) -> Optional[str]:
    """Serialize platform send metadata stored on notification subscriptions."""
    if not isinstance(metadata, Mapping):
        return None
    clean = {
        str(key): value
        for key, value in metadata.items()
        if value is not None and isinstance(value, _SCALAR_TYPES)
    }
    if not clean:
        return None
    return json.dumps(clean, sort_keys=True, separators=(",", ":"))


def _decode_notify_delivery_metadata(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    if not raw:
        return {}
    try:
        data = json.loads(str(raw))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): value for key, value in data.items() if isinstance(value, _SCALAR_TYPES)}


def add_notify_sub(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
    user_id: Optional[str] = None,
    user_id_alt: Optional[str] = None,
    chat_type: Optional[str] = None,
    notifier_profile: Optional[str] = None,
    delivery_mode: Optional[str] = None,
    delivery_metadata: Optional[Mapping[str, Any]] = None,
) -> None:
    """Register a gateway source wanting terminal-state notifications for
    ``task_id``; idempotent on (task, platform, chat, thread).

    ``user_id_alt`` (Signal UUID, Feishu union_id, ...) and ``chat_type`` are
    replayed on active wake: ``build_session_key`` prefers the alt id, so
    omitting it would key the wake into a different session. ``None`` keeps an
    existing row's value. ``delivery_mode``: ``None`` leaves an existing row
    untouched, an explicit valid value is last-write-wins, unknown falls back
    to ``"notify"``. ``delivery_metadata`` merges supplied routing anchors
    into an existing row so re-subscribing never discards them. New subs start
    caught up (``last_event_id`` =
    ``MAX(task_events.id)``) so the notifier never replays history at boot.
    """
    from hermes_cli.kanban_placement import is_placeholder_session
    if platform in ("hub", "tui") and (is_placeholder_session(chat_id) or is_headless_session(chat_id)):
        return
    valid_mode = delivery_mode if delivery_mode in _NOTIFY_DELIVERY_MODES else None
    # api_server is stateless: the adapter has no send(), the wake self-post IS
    # the delivery. A plain 'notify' default would leave those subs with no
    # delivery mechanism at all. Explicit modes still win.
    insert_mode = valid_mode or ("notify+wake" if platform == "api_server" else "notify")
    key = _sub_key(task_id, platform, chat_id, thread_id)
    with _kb.write_txn(conn):
        existing = conn.execute(
            "SELECT delivery_metadata FROM kanban_notify_subs " + _SUB_KEY_WHERE,
            key,
        ).fetchone()
        existing_metadata = _decode_notify_delivery_metadata(existing["delivery_metadata"]) if existing else {}
        merged_metadata = dict(existing_metadata)
        if delivery_metadata:
            merged_metadata.update(delivery_metadata)
            # Provenance only ever upgrades: re-subscribing the creating chat as a shepherd must not turn
            # its owning sub into an observer that children stop inheriting. [daveey]
            if (existing_metadata.get("reason") in OWNING_SUB_REASONS
                    and merged_metadata.get("reason") not in OWNING_SUB_REASONS):
                merged_metadata["reason"] = existing_metadata["reason"]
        metadata_json = _encode_notify_delivery_metadata(merged_metadata) if merged_metadata else None
        conn.execute(
            """
            INSERT OR IGNORE INTO kanban_notify_subs
                (task_id, platform, chat_id, thread_id, user_id, user_id_alt,
                 chat_type, notifier_profile, delivery_mode, delivery_metadata,
                 created_at, last_event_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    COALESCE((SELECT MAX(id) FROM task_events WHERE task_id = ?), 0))
            """,
            (
                *key, user_id, user_id_alt, chat_type or "dm", notifier_profile,
                insert_mode, metadata_json, int(time.time()), task_id,
            ),
        )
        # chat_type / delivery_mode are last-write-wins; delivery metadata
        # preserves existing routing fields while supplied fields overwrite them.
        # user_id, user_id_alt and notifier_profile only self-heal legacy rows lacking one.
        for column, value, fill_only in (
            ("chat_type", chat_type, False),
            ("user_id", user_id, True),
            ("user_id_alt", user_id_alt, True),
            ("notifier_profile", notifier_profile, True),
            ("delivery_mode", valid_mode, False),
            ("delivery_metadata", metadata_json, False),
        ):
            if not value:
                continue
            guard = f" AND ({column} IS NULL OR {column} = '')" if fill_only else ""
            conn.execute(
                f"UPDATE kanban_notify_subs SET {column} = ? " + _SUB_KEY_WHERE + guard,
                (value, *key),
            )


def _notify_profile_filter(
    notifier_profiles: Optional[Iterable[str]],
    *,
    include_unowned: bool,
) -> tuple[str, list[str]]:
    """Build an optional SQL predicate for notification profile ownership."""
    if notifier_profiles is None:
        return "", []

    profiles = sorted({str(p).strip() for p in notifier_profiles if str(p).strip()})
    clauses: list[str] = []
    params: list[str] = []
    if profiles:
        clauses.append("notifier_profile IN (" + ",".join("?" for _ in profiles) + ")")
        params.extend(profiles)
    if include_unowned:
        clauses.append("notifier_profile IS NULL OR notifier_profile = ''")
    if not clauses:
        return "0", []
    return "(" + ") OR (".join(clauses) + ")", params


def list_notify_subs(
    conn: sqlite3.Connection,
    task_id: Optional[str] = None,
    *,
    notifier_profiles: Optional[Iterable[str]] = None,
    include_unowned: bool = False,
    platform: Optional[str] = None,
    chat_id: Optional[str] = None,
) -> list[dict]:
    """List subscriptions, optionally restricted to notifier profile owners.

    No ``notifier_profiles`` -> all subscriptions. Gateway notifiers pass the
    profiles they own so they cannot claim another gateway's events;
    ``include_unowned`` (dispatch owner) covers legacy rows without a stamp.
    ``platform`` (case-insensitive) and ``chat_id`` (exact) filter in SQL, as
    :func:`count_notify_subs` does: a per-chat poller that fetched every row and
    filtered in Python stepped thousands of rows each poll, re-taking the GIL per
    row, from every open chat at once. [daveey]
    """
    owner_where, owner_params = _notify_profile_filter(
        notifier_profiles, include_unowned=include_unowned,
    )
    where: list[str] = []
    params: list[Any] = []
    if task_id is not None:
        where.append("task_id = ?")
        params.append(task_id)
    if owner_where:
        where.append(owner_where)
        params.extend(owner_params)
    for clause, value in (("LOWER(platform) = LOWER(?)", platform), ("chat_id = ?", chat_id)):
        if value is not None:
            where.append(clause)
            params.append(value)
    sql = "SELECT * FROM kanban_notify_subs"
    if where:
        sql += " WHERE " + " AND ".join(f"({clause})" for clause in where)
    out: list[dict] = []
    for row in conn.execute(sql, params).fetchall():
        item = dict(row)
        if "delivery_metadata" in item:
            item["delivery_metadata"] = _decode_notify_delivery_metadata(item.get("delivery_metadata"))
        out.append(item)
    return out


def count_notify_subs(
    db_path: Optional[Path] = None,
    *,
    board: Optional[str] = None,
    notifier_profiles: Optional[Iterable[str]] = None,
    include_unowned: bool = False,
    platform: Optional[str] = None,
    chat_id: Optional[str] = None,
    thread_id: Optional[str] = None,
) -> int:
    """Count ``kanban_notify_subs`` rows via a read-only connection — the
    notifier's cheap zero-subscription early exit. Unlike :func:`connect` it
    never creates the file, runs init/migration or opens writable; WAL rows are
    still visible so a fresh sub is never missed. Missing DB / missing table
    counts as zero; platform matches case-insensitively (as notifier routing),
    chat/thread exactly. Raises :class:`sqlite3.Error` if the DB exists but is
    unreadable — callers pick their own fallback.
    """
    path = db_path if db_path is not None else _kb.kanban_db_path(board=board)
    if not path.exists():
        return 0
    owner_where, owner_params = _notify_profile_filter(
        notifier_profiles, include_unowned=include_unowned,
    )
    clauses: list[str] = []
    params: list[Any] = []
    if owner_where:
        clauses.append(f"({owner_where})")
        params.extend(owner_params)
    for clause, value in (
        ("LOWER(platform) = LOWER(?)", platform),
        ("chat_id = ?", chat_id),
        ("thread_id = ?", thread_id),
    ):
        if value is not None:
            clauses.append(clause)
            params.append(value)
    query = "SELECT COUNT(*) FROM kanban_notify_subs"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    conn = _kanban_lockguard.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        try:
            row = conn.execute(query, params).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return 0
            raise
        return int(row[0]) if row else 0
    finally:
        conn.close()


def remove_notify_sub(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
) -> bool:
    # Pure composition primitive: reconciliation validates receipt + row snapshot
    # and retires the subscription in one outer transaction.
    with _kb.write_txn(conn, allow_nested=True):
        cur = conn.execute(
            "DELETE FROM kanban_notify_subs " + _SUB_KEY_WHERE,
            _sub_key(task_id, platform, chat_id, thread_id),
        )
    return cur.rowcount > 0


# Origin bindings run in the creating call; a legacy (unstamped) 'hub' sub written later than this after its
# card's creation is a hermes-hub shepherd. Also the fallback for rows still unstamped at inherit time. [daveey]
LEGACY_ORIGIN_BIND_WINDOW_S = 120

_UNSTAMPED_SQL = ("(CASE WHEN delivery_metadata IS NULL THEN 1 WHEN NOT json_valid(delivery_metadata) THEN 1 "
                  "WHEN json_type(delivery_metadata) != 'object' THEN 1 "
                  "WHEN json_extract(delivery_metadata, '$.reason') IS NULL THEN 1 ELSE 0 END) = 1")


def stamp_legacy_hub_sub_provenance(conn: sqlite3.Connection) -> int:
    """One-shot backfill: give every unstamped 'hub' sub a ``reason`` (``origin`` or ``shepherd``). [daveey]

    Before provenance stamping, the origin binding and hermes-hub shepherd subs were both bare 'hub' rows, and
    inheritance copied them to children with the CHILD's creation time, so a copied observer looked exactly
    like a fresh origin binding and kept spreading down the program. Resolve each row through the card graph:
    a row whose chat also sits on one of the card's parents or its creator task, written no later than the row,
    is an inherited copy and takes that ancestor's resolved reason; any other row is the card's own, ``origin``
    when written within ``LEGACY_ORIGIN_BIND_WINDOW_S`` of the card's creation, else ``shepherd``. Known
    ambiguity: where old code collapsed a copied observer and the same chat's origin binding into one row
    (``INSERT OR IGNORE``), it is stamped ``shepherd`` (under-inherits, never over-inherits). Idempotent and
    cheap once done (no unstamped rows left). Returns the number of rows stamped."""
    rows = conn.execute(
        "SELECT s.task_id, s.chat_id, s.thread_id, s.created_at AS sub_at, t.created_at AS task_at "
        "FROM kanban_notify_subs s LEFT JOIN tasks t ON t.id = s.task_id "
        f"WHERE s.platform = 'hub' AND {_UNSTAMPED_SQL.replace('delivery_metadata', 's.delivery_metadata')}"
    ).fetchall()
    if not rows:
        return 0
    pending = {(r["task_id"], r["chat_id"]): r for r in rows}
    task_ids = sorted({r["task_id"] for r in rows})
    ancestors: dict[str, set[str]] = {}

    def _ancestors(task_id: str) -> set[str]:
        if task_id not in ancestors:
            found = {r[0] for r in conn.execute("SELECT parent_id FROM task_links WHERE child_id = ?", (task_id,))}
            for (payload,) in conn.execute(
                    "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'created' ORDER BY id LIMIT 1",
                    (task_id,)):
                try:
                    creator = (json.loads(payload or "{}") or {}).get("creator_task_id")
                except (TypeError, ValueError, AttributeError):
                    creator = None
                if creator:
                    found.add(str(creator))
            found.discard(task_id)
            ancestors[task_id] = found
        return ancestors[task_id]

    resolved: dict[tuple[str, str], str] = {}

    def _row(task_id: str, chat_id: str) -> Optional[tuple[Optional[str], int]]:
        """``(reason or None, created_at)`` of the card's hub row for ``chat_id``; None without one."""
        key = (task_id, chat_id)
        if key in pending:
            return None if key not in resolved else (resolved[key], int(pending[key]["sub_at"] or 0))
        row = conn.execute(
            "SELECT delivery_metadata, created_at FROM kanban_notify_subs WHERE task_id = ? AND platform = 'hub' "
            "AND chat_id = ? ORDER BY thread_id LIMIT 1", (task_id, chat_id)).fetchone()
        if row is None:
            return None
        return (_decode_notify_delivery_metadata(row["delivery_metadata"]).get("reason") or "origin",
                int(row["created_at"] or 0))

    def _resolve(task_id: str, chat_id: str, seen: frozenset) -> Optional[str]:
        key = (task_id, chat_id)
        if key in resolved:
            return resolved[key]
        if key not in pending:
            got = _row(task_id, chat_id)
            return got[0] if got else None
        row = pending[key]
        sub_at = int(row["sub_at"] or 0)
        # Only an ancestor row written no later than this one can be its source: a copy is written after the
        # row it copies. A later same-chat row on an ancestor is an independent shepherd of that ancestor.
        inherited = []
        for anc in sorted(_ancestors(task_id)):
            if anc in seen:
                continue
            reason = _resolve(anc, chat_id, seen | {task_id})
            got = _row(anc, chat_id)
            if reason is not None and got is not None and got[1] <= sub_at:
                inherited.append(reason)
        if inherited:
            reason = "shepherd" if all(r in OBSERVER_SUB_REASONS for r in inherited) else "origin"
        else:
            bound_at_creation = (row["task_at"] is not None and row["sub_at"] is not None
                                 and sub_at <= int(row["task_at"]) + LEGACY_ORIGIN_BIND_WINDOW_S)
            reason = "origin" if bound_at_creation else "shepherd"
        resolved[key] = reason
        return reason

    with _kb.write_txn(conn, allow_nested=True):
        for task_id in task_ids:
            for (tid, chat_id) in [k for k in pending if k[0] == task_id]:
                _resolve(tid, chat_id, frozenset())
        for (task_id, chat_id), reason in resolved.items():
            conn.execute(
                "UPDATE kanban_notify_subs SET delivery_metadata = CASE "
                "WHEN delivery_metadata IS NULL THEN json_object('reason', ?) "
                "WHEN NOT json_valid(delivery_metadata) THEN json_object('reason', ?) "
                "WHEN json_type(delivery_metadata) = 'object' THEN json_set(delivery_metadata, '$.reason', ?) "
                "ELSE json_object('reason', ?) END "
                f"WHERE task_id = ? AND platform = 'hub' AND chat_id = ? AND {_UNSTAMPED_SQL}",
                (reason, reason, reason, reason, task_id, chat_id),
            )
    return len(rows)


def purge_stale_done_notify_subs(conn: sqlite3.Connection, *, max_age_days: int = 30) -> int:
    """Delete notify subs whose task sat in ``done``/``blocked`` untouched for
    longer than ``max_age_days`` (``<= 0`` disables); returns rows deleted.

    Subs survive ``done`` because a reopened task must still notify its origin,
    which accumulates forever on never-archiving boards. ``blocked`` is
    abandoned (unlike ``backlog``/``ready``) so it reaps on the same clock. Age
    = latest event, else ``completed_at``, else ``created_at`` — any activity,
    including a reopen, exempts the sub.

    The notifier keeps subscriptions alive through ``done`` because a completed task can be reopened (review
    corrections, continuation) and the reopened cycle must still notify its origin session. On boards that
    never archive, that retention would otherwise accumulate subscription rows forever — each one scanned
    every notifier tick. This GC bounds that: a task that has been ``done`` with no new events for the
    retention window is treated as settled and its subscriptions are purged. ``blocked`` tasks
    (circuit-breaker trips, dead workers) are reaped on the same clock — they are abandoned, not idle,
    unlike a ``backlog``/``ready`` card that is merely waiting for pickup (#100955).
    """
    try:
        days = int(max_age_days)
    except (TypeError, ValueError):
        days = 30
    if days <= 0:
        return 0
    cutoff = int(time.time()) - days * 86400
    with _kb.write_txn(conn):
        cur = conn.execute(
            "DELETE FROM kanban_notify_subs WHERE task_id IN ("
            " SELECT t.id FROM tasks t"
            " WHERE t.status IN ('done', 'blocked')"
            " AND COALESCE("
            "  (SELECT MAX(e.created_at) FROM task_events e"
            "   WHERE e.task_id = t.id),"
            "  t.completed_at, t.created_at, 0"
            " ) < ?)",
            (cutoff,),
        )
    return int(cur.rowcount or 0)


def _notify_cursor(
    conn: sqlite3.Connection, task_id: str, platform: str, chat_id: str, thread_id: Optional[str],
) -> Optional[int]:
    """``last_event_id`` of one subscription row, or ``None`` when unsubscribed."""
    row = conn.execute(
        "SELECT last_event_id FROM kanban_notify_subs " + _SUB_KEY_WHERE,
        _sub_key(task_id, platform, chat_id, thread_id),
    ).fetchone()
    return None if row is None else int(row["last_event_id"])


def unseen_events_for_sub(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
    kinds: Optional[Iterable[str]] = None,
) -> tuple[int, list[Event]]:
    """Return ``(new_cursor, events)`` with ``id > last_event_id``. The cursor
    is NOT advanced here; call :func:`advance_notify_cursor` after delivery.
    """
    cursor = _notify_cursor(conn, task_id, platform, chat_id, thread_id)
    if cursor is None:
        return 0, []
    kind_list = list(kinds) if kinds else None
    q = (
        "SELECT * FROM task_events WHERE task_id = ? AND id > ? "
        + ("AND kind IN (" + ",".join("?" * len(kind_list)) + ") " if kind_list else "")
        + "ORDER BY id ASC"
    )
    params: list[Any] = [task_id, cursor]
    if kind_list:
        params.extend(kind_list)
    rows = conn.execute(q, params).fetchall()
    out = [_kb.Event.from_row(r) for r in rows]
    max_id = max([cursor, *(int(r["id"]) for r in rows)])
    return max_id, out


def claim_unseen_events_for_sub(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
    kinds: Optional[Iterable[str]] = None,
    durable: bool = False,
) -> tuple[int, int, list[Event]]:
    """Atomically claim unseen events for one subscription.

    Returns ``(old_cursor, new_cursor, events)``; when events are returned the
    row's ``last_event_id`` has already been advanced inside ``BEGIN IMMEDIATE``,
    so concurrent gateway watchers on the same board DB serialize on SQLite's
    writer lock and only the first claims a given event range. Callers send the
    events, then leave the cursor or call :func:`rewind_notify_cursor` on
    delivery failure.

    ``durable=True`` (the tui chat consumers) also records the claim as IN FLIGHT, owned by this process
    (``inflight_from`` = first unacknowledged cursor, ``inflight_pid``/``inflight_start``): the claimer calls
    :func:`ack_notify_claim` once the notice is durably in the chat's transcript. A claim whose holder process
    died before that ack is rewound (:func:`recover_orphaned_notify_claims`, also run here) and delivered again,
    so a serve replaced while its chat was mid-turn no longer loses the notice it held in memory. While another
    LIVE process holds an unacknowledged claim on the row, nothing is claimed here. [daveey]
    """
    with _kb.write_txn(conn):
        key = _sub_key(task_id, platform, chat_id, thread_id)
        if durable:
            row = conn.execute(
                "SELECT inflight_from, inflight_pid, inflight_start FROM kanban_notify_subs " + _SUB_KEY_WHERE, key,
            ).fetchone()
            if row is not None and row["inflight_from"] is not None and not _holder_is_me(row):
                if _holder_alive(row):
                    cursor = _notify_cursor(conn, task_id, platform, chat_id, thread_id) or 0
                    return cursor, cursor, []
                _rewind_orphan(conn, key, row)
        old_cursor = _notify_cursor(conn, task_id, platform, chat_id, thread_id)
        if old_cursor is None:
            return 0, 0, []
        new_cursor, events = unseen_events_for_sub(
            conn, task_id=task_id, platform=platform, chat_id=chat_id,
            thread_id=thread_id, kinds=kinds,
        )
        if not events:
            return old_cursor, old_cursor, []
        _cas_cursor(conn, key, new_cursor, old_cursor)
        if durable:
            pid, start = _my_holder()
            conn.execute(
                "UPDATE kanban_notify_subs SET inflight_from = COALESCE(inflight_from, ?), inflight_pid = ?, "
                "inflight_start = ? " + _SUB_KEY_WHERE, (int(old_cursor), pid, start, *key),
            )
        return old_cursor, new_cursor, events


# ---------------------------------------------------------------------------
# In-flight (claimed, not yet durably delivered) notice claims. [daveey]
# ---------------------------------------------------------------------------

_MY_HOLDER: Optional[tuple[int, Optional[int]]] = None


def _my_holder() -> tuple[int, Optional[int]]:
    """``(pid, start-time fingerprint)`` of this process (PID-reuse guard), cached per pid."""
    import os
    global _MY_HOLDER
    pid = os.getpid()
    if _MY_HOLDER is None or _MY_HOLDER[0] != pid:
        _MY_HOLDER = (pid, _process_start(pid))
    return _MY_HOLDER


def _process_start(pid: int) -> Optional[int]:
    try:
        from gateway.status import _get_process_start_time
        return _get_process_start_time(int(pid))
    except Exception:
        return None


def _holder_is_me(row) -> bool:
    return row["inflight_pid"] is not None and int(row["inflight_pid"]) == _my_holder()[0]


def _holder_alive(row) -> bool:
    """True unless the holder process is provably gone (dead pid, or a recycled pid with another start time)."""
    from gateway.status import _pid_exists

    pid = row["inflight_pid"]
    if pid is None:
        return False
    try:
        pid = int(pid)
        if pid <= 0 or not _pid_exists(pid):
            return False
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # exists, owned by someone else
    except (OSError, ValueError, TypeError, OverflowError):
        return False
    recorded = row["inflight_start"]
    if recorded is None:
        return True
    current = _process_start(int(pid))
    if current is None:
        return True
    try:
        from gateway.status import start_time_fingerprints_match
        return start_time_fingerprints_match(recorded, current)
    except Exception:
        return int(recorded) == int(current)


def _rewind_orphan(conn: sqlite3.Connection, key: tuple, row) -> bool:
    """Hand an orphaned claim's range back (cursor -> ``inflight_from``), CAS on the dead holder."""
    cur = conn.execute(
        "UPDATE kanban_notify_subs SET last_event_id = MIN(last_event_id, inflight_from), inflight_from = NULL, "
        "inflight_pid = NULL, inflight_start = NULL " + _SUB_KEY_WHERE
        + " AND inflight_from = ? AND inflight_pid IS ?",
        (*key, row["inflight_from"], row["inflight_pid"]),
    )
    if cur.rowcount:
        import logging
        logging.getLogger(__name__).warning(
            "kanban notice claim for %s -> %s/%s was never delivered (holder pid %s is gone); rewound to event %s "
            "for redelivery", key[0], key[1], key[2], row["inflight_pid"], row["inflight_from"])
    return cur.rowcount > 0


def recover_orphaned_notify_claims(
    conn: sqlite3.Connection, *, platform: Optional[str] = None, chat_id: Optional[str] = None,
) -> int:
    """Rewind every in-flight claim whose holder process is gone; returns how many. Never touches a claim held
    by this process or by a live one."""
    where = ["inflight_from IS NOT NULL"]
    params: list[Any] = []
    if platform is not None:
        where.append("LOWER(platform) = LOWER(?)")
        params.append(platform)
    if chat_id is not None:
        where.append("chat_id = ?")
        params.append(chat_id)
    try:
        rows = conn.execute(
            "SELECT task_id, platform, chat_id, thread_id, inflight_from, inflight_pid, inflight_start "
            "FROM kanban_notify_subs WHERE " + " AND ".join(where), params,
        ).fetchall()
    except sqlite3.OperationalError:
        return 0  # pre-migration board
    orphans = [r for r in rows if not _holder_is_me(r) and not _holder_alive(r)]
    if not orphans:
        return 0
    n = 0
    with _kb.write_txn(conn):
        for r in orphans:
            n += _rewind_orphan(conn, _sub_key(r["task_id"], r["platform"], r["chat_id"], r["thread_id"]), r)
    return n


def ack_notify_claim(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
    upto: int,
    from_cursor: Optional[int] = None,
) -> None:
    """Every event of this subscription up to ``upto`` is durably delivered: clear the in-flight marker (or move
    it to ``upto`` when a later claim is still outstanding). ``from_cursor`` (the delivered range's start) skips
    the ack while an EARLIER claim is still outstanding (``inflight_from < from_cursor``), so its marker is kept.
    A sub whose task was archived meanwhile is removed now (its removal waited for this ack)."""
    key = _sub_key(task_id, platform, chat_id, thread_id)
    guard = "" if from_cursor is None else " AND inflight_from >= ?"
    with _kb.write_txn(conn):
        conn.execute(
            "UPDATE kanban_notify_subs SET "
            "inflight_pid = CASE WHEN last_event_id <= ? THEN NULL ELSE inflight_pid END, "
            "inflight_start = CASE WHEN last_event_id <= ? THEN NULL ELSE inflight_start END, "
            "inflight_from = CASE WHEN last_event_id <= ? THEN NULL ELSE MAX(inflight_from, ?) END "
            + _SUB_KEY_WHERE + " AND inflight_from IS NOT NULL" + guard,
            (int(upto), int(upto), int(upto), int(upto), *key,
             *(() if from_cursor is None else (int(from_cursor),))),
        )
        row = conn.execute(
            "SELECT s.inflight_from, t.status FROM kanban_notify_subs s LEFT JOIN tasks t ON t.id = s.task_id "
            "WHERE s.task_id = ? AND s.platform = ? AND s.chat_id = ? AND s.thread_id = ?", key,
        ).fetchone()
        if row is not None and row["inflight_from"] is None and row["status"] == "archived":
            conn.execute("DELETE FROM kanban_notify_subs " + _SUB_KEY_WHERE, key)


def notify_claim_in_flight(
    conn: sqlite3.Connection, *, task_id: str, platform: str, chat_id: str, thread_id: Optional[str] = None,
) -> bool:
    row = conn.execute(
        "SELECT inflight_from FROM kanban_notify_subs " + _SUB_KEY_WHERE,
        _sub_key(task_id, platform, chat_id, thread_id),
    ).fetchone()
    return row is not None and row["inflight_from"] is not None


def _cas_cursor(conn: sqlite3.Connection, key: tuple, new_cursor: int, expected: int) -> sqlite3.Cursor:
    """Move ``last_event_id`` only if it still equals ``expected``."""
    return conn.execute(
        "UPDATE kanban_notify_subs SET last_event_id = ? " + _SUB_KEY_WHERE + " AND last_event_id = ?",
        (int(new_cursor), *key, int(expected)),
    )


def advance_notify_cursor(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
    new_cursor: int,
) -> None:
    with _kb.write_txn(conn):
        conn.execute(
            "UPDATE kanban_notify_subs SET last_event_id = ? " + _SUB_KEY_WHERE,
            (int(new_cursor), *_sub_key(task_id, platform, chat_id, thread_id)),
        )


def record_notify_ping(
    conn: sqlite3.Connection, *, task_id: str, platform: str, chat_id: str,
    thread_id: Optional[str] = None, event_id: int,
) -> None:
    """Checkpoint a sent ping independently of the retryable wake cursor."""
    with _kb.write_txn(conn):
        conn.execute(
            "UPDATE kanban_notify_subs SET last_ping_event_id = MAX(last_ping_event_id, ?) "
            + _SUB_KEY_WHERE,
            (int(event_id), *_sub_key(task_id, platform, chat_id, thread_id)),
        )


def rewind_notify_cursor(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
    claimed_cursor: int,
    old_cursor: int,
) -> bool:
    """Undo a claim when delivery fails. The CAS guard only rewinds if no later
    notifier advanced the row, so retries never clobber newer progress.
    """
    with _kb.write_txn(conn):
        key = _sub_key(task_id, platform, chat_id, thread_id)
        cur = _cas_cursor(conn, key, old_cursor, claimed_cursor)
        if cur.rowcount > 0:
            # The handed-back range is no longer in flight; an earlier, still-held claim keeps its marker.
            try:
                conn.execute(
                    "UPDATE kanban_notify_subs SET "
                    "inflight_pid = CASE WHEN inflight_from < ? THEN inflight_pid END, "
                    "inflight_start = CASE WHEN inflight_from < ? THEN inflight_start END, "
                    "inflight_from = CASE WHEN inflight_from < ? THEN inflight_from END "
                    + _SUB_KEY_WHERE + " AND inflight_from IS NOT NULL",
                    (int(old_cursor), int(old_cursor), int(old_cursor), *key),
                )
            except sqlite3.OperationalError:
                pass  # pre-migration board
    return cur.rowcount > 0


def _inherit_notify_subs(
    conn: sqlite3.Connection, child_id: str, parents: Iterable[str], *,
    created_at: Optional[int] = None,
) -> None:
    """Moved from ``kanban_db`` (file-size cap). Copy parents' notify subscriptions to a child, cursor caught up to the
    child's current event so a late ``link_tasks`` never replays history.

    Single owner of inheritance (create_task, link_tasks, decompose). It must
    copy EVERY routing/delivery column: dropping ``chat_type`` made DM-originated
    completions wake a fresh group session instead of the originating DM.

    Omitting columns here silently degrades routing: a DM-originated child completion falls back to
    chat_type='group' and wakes a fresh group-scoped session instead of the originating DM (issue #73030).

    Only OWNING subscriptions are inherited (``_owning_sub_clause``). Observer subs (``shepherd``: explicit
    ``notify-subscribe`` incl. hermes-hub's shepherd action; ``touch``: a comment/unblock/link) watch THAT card,
    not the program under it: copying them made every chat that ever watched a shepherd card own all of its
    descendants (runners-panel chat d48ade owned 4 foreign cards via t_82f80db4's 44 subs). [daveey]
    """
    parent_ids = tuple(dict.fromkeys(p for p in parents if p))
    if not parent_ids:
        return
    row = conn.execute(
        "SELECT COALESCE(MAX(id), 0) AS cursor FROM task_events WHERE task_id = ?", (child_id,),
    ).fetchone()
    cursor = int(row["cursor"] if row is not None else 0)
    placeholders = ",".join("?" * len(parent_ids))
    conn.execute(
        f"""
        INSERT OR IGNORE INTO kanban_notify_subs
            (task_id, platform, chat_id, thread_id, user_id, user_id_alt,
             chat_type, notifier_profile, delivery_mode, delivery_metadata,
             created_at, last_event_id)
        SELECT ?, s.platform, s.chat_id, s.thread_id, s.user_id, s.user_id_alt,
               COALESCE(s.chat_type, 'dm'), s.notifier_profile,
               COALESCE(s.delivery_mode, 'notify'), s.delivery_metadata, ?, ?
          FROM kanban_notify_subs s
          LEFT JOIN tasks p ON p.id = s.task_id
         WHERE s.task_id IN ({placeholders}) AND {_owning_sub_clause()}
        """,
        (child_id, int(created_at if created_at is not None else time.time()), cursor, *parent_ids),
    )


def _owning_sub_clause() -> str:
    """SQL predicate (aliases ``s`` = sub, ``p`` = its task) for a subscription a child inherits.

    Stamped provenance (``delivery_metadata.reason``) decides: ``shepherd``/``touch`` are observers and never
    inherit; ``create``/``origin`` (and any other value) do. Legacy 'hub' rows are stamped once at board open
    (``kanban_db_notify.stamp_legacy_hub_sub_provenance``); a 'hub' row still unstamped (written by an older
    process since) counts as the origin binding only when written at creation (``LEGACY_ORIGIN_BIND_WINDOW_S``).
    Every other unstamped row keeps the old inherit-everything behaviour (gateway DM subs carry no reason)."""
    observers = ", ".join(f"'{r}'" for r in OBSERVER_SUB_REASONS)
    reason = ("CASE WHEN json_valid(s.delivery_metadata) "
              "THEN json_extract(s.delivery_metadata, '$.reason') END")
    # A headless receipt session (worker/cron transcript) is never a destination, whatever its reason: a row
    # written before ``add_notify_sub`` refused them must not spread to children (``is_headless_session``).
    headless = (f"(s.platform IN ('hub', 'tui') AND (s.chat_id GLOB '{_HEADLESS_GLOB}' "
                f"OR s.chat_id GLOB '*/{_HEADLESS_GLOB}'))")
    return (f"COALESCE({reason} IN ({observers}), 0) = 0 AND NOT {headless} AND ({reason} IS NOT NULL "
            f"OR s.platform != 'hub' OR s.created_at <= COALESCE(p.created_at, 0) + {LEGACY_ORIGIN_BIND_WINDOW_S})")


# Late-bound origin namespace (see module docstring); imported LAST so this
# module is fully populated before ``kanban_db`` imports from it.
from hermes_cli import kanban_db as _kb
