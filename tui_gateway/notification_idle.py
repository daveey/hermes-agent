"""Per-session notification read memos tied to published SQLite commits. [daveey]

Only observational reads are memoized. A key is taken BEFORE reading, so a commit
racing the read invalidates the next poll. Errors never establish an idle proof.
"""
from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import threading
from pathlib import Path

logger = logging.getLogger(__name__)


def pending_producers(db, session, target):
    """Read pending rows unless this owner has an unchanged, proven-empty input."""
    import hermes_state_producer_admission as pa
    stamp = state_revision(db)
    empty_key = (target, stamp)
    if stamp is not None and session.get("_notif_producer_empty") == empty_key:
        return []
    session.pop("_notif_producer_empty", None)
    if not target or db.get_session(target) is None:
        return []
    rows = pa.pending_for_owner(db, pa.owner_of(db, target))
    if not rows and stamp is not None:
        session["_notif_producer_empty"] = empty_key
    return rows


@contextlib.contextmanager
def collection_home(session, launch_home):
    """Local SQLite collection needs a home, not provider secrets or a terminal policy."""
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    token = set_hermes_home_override(str(session.get("profile_home") or launch_home()))
    try:
        yield
    finally:
        reset_hermes_home_override(token)


def board_quiet(path, session_key):
    """Only a successful empty read is a proof; foreign in-flight claims need clock/liveness retries."""
    from pathlib import Path
    from tui_gateway.notification_reader import reading
    from tui_gateway.session_notifications import _KANBAN_NOTIFY_KINDS
    placeholders = ",".join("?" for _ in _KANBAN_NOTIFY_KINDS)
    try:
        with reading(Path(path)) as (conn, _retained):
            row = conn.execute(
                "SELECT 1 FROM kanban_notify_subs AS s WHERE LOWER(platform) = 'tui' AND chat_id = ? "
                "AND (inflight_from IS NOT NULL OR EXISTS (SELECT 1 FROM task_events AS e "
                "WHERE e.task_id = s.task_id AND e.id > s.last_event_id "
                f"AND e.kind IN ({placeholders}))) LIMIT 1",
                (session_key, *_KANBAN_NOTIFY_KINDS),
            ).fetchone()
            return row is None
    except Exception:
        logger.debug("Kanban empty proof unavailable", exc_info=True)
        return False


# [daveey] Board path -> (reader, data_version, events scanned up to, newest notify-kind event id, tui subs by
# chat, subscription key). data_version moves on every commit, and the main board commits ~40 times a minute with
# heartbeats, claims and run bookkeeping that no chat poller reads, so a commit gate re-polled every board for every
# chat on nearly every tick (t_01a1cee98b798343). What a chat's poll can claim is fixed by two inputs: the newest
# deliverable-kind event id (AUTOINCREMENT, so any new one is above it) and that chat's own tui subscription
# rows (cursor, in-flight mark). Read both once per commit for every chat in this process.
_SIGS: dict[str, tuple] = {}
_SIGS_LOCK = threading.Lock()
_SIGS_MAX = 64
_SUBS_SQL = ("SELECT chat_id, task_id, platform, thread_id, last_event_id, inflight_from "
             "FROM kanban_notify_subs WHERE LOWER(platform) = 'tui' ORDER BY chat_id, task_id, platform, thread_id")
# The annotation revision is global, even for one table's coverage. Use a
# subscription-only counter, including inflight_from, in the rows' snapshot.



def _subs_revision(conn, path):
    """Subscription revision in the caller's snapshot; absent coverage reads literally."""
    import os
    from hermes_cli.kanban_notify_revision import read_revision
    st = os.stat(path)
    return read_revision(conn, (str(path), st.st_dev, st.st_ino))


# A private (unpublished) read asked for one chat needs only that chat's rows; same order as its slice of
# _SUBS_SQL. Fetching all of them (4k rows on the hub, one sqlite3 step and GIL handoff each) to return one
# slice was most of the tui pollers' work under contention (t_01a20286f9f3f262).
_CHAT_SUBS_SQL = ("SELECT chat_id, task_id, platform, thread_id, last_event_id, inflight_from "
                  "FROM kanban_notify_subs WHERE chat_id = ? AND LOWER(platform) = 'tui' "
                  "ORDER BY task_id, platform, thread_id")
# Both subscription reads hand their rows to Python in ONE result row, as a JSON array built inside SQLite. A
# changed subscription revision re-reads all ~4k tui rows on the hub, and each stepped row was a GIL handoff that
# waited out the serve's convoy: live py-spy put 490 of 714 tui-notif-poller ``_notify_content`` samples on that
# fetch after the private-read cut (t_01a20286f9f3f262). The columns are TEXT/INTEGER/NULL, which JSON carries
# exactly, so this skips ``hermes_state_page_pack``'s per-cell type tags (5x the CPU of a plain fetch for these
# rows; this costs about 1.2x). A value JSON cannot carry (a BLOB) reads plainly instead.
_SUBS_PACK = ("WITH _subs(a, b, c, d, e, f) AS MATERIALIZED ({}) "
              "SELECT json_group_array(json_array(a, b, c, d, e, f)) FROM _subs")


def _subs_rows(conn, sql, params=()):
    try:
        packed = conn.execute(_SUBS_PACK.format(sql), params).fetchone()[0]
    except sqlite3.OperationalError:
        return [tuple(r) for r in conn.execute(sql, params).fetchall()]
    return [tuple(r) for r in json.loads(packed)]


def _notify_content(conn, version, held, path=None, only_chat=None):
    """``only_chat``: a private read for one chat; its result is never published, so it reads only that chat's
    subscription rows and skips the memo-only subscription revision."""
    from tui_gateway.session_notifications import _KANBAN_NOTIFY_KINDS
    present = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name IN "
                                          "('task_events', 'kanban_notify_subs')")}
    if len(present) != 2:
        return None  # not a board (yet): fall back to the commit revision
    conn.execute("BEGIN")  # one snapshot for the event scan and the subscription rows
    try:
        revision = None
        if path is not None and only_chat is None:
            with contextlib.suppress(Exception):  # unknown coverage: read the rows
                revision = _subs_revision(conn, path)
        top = int(conn.execute("SELECT MAX(id) FROM task_events").fetchone()[0] or 0)
        start, newest = (held[2], held[3]) if held is not None and held[2] <= top else (0, 0)
        if top > start:
            marks = ",".join("?" for _ in _KANBAN_NOTIFY_KINDS)
            row = conn.execute(f"SELECT MAX(id) FROM task_events WHERE id > ? AND id <= ? AND kind IN ({marks})",
                               (start, top, *_KANBAN_NOTIFY_KINDS)).fetchone()
            newest = max(newest, int(row[0] or 0))
        subs_key = revision  # includes inflight_from; no per-commit subscription scan
        by_chat = held[4] if subs_key is not None and held is not None and held[5] == subs_key else None
        if by_chat is not None:
            rows = ()
        elif only_chat is not None:
            rows = _subs_rows(conn, _CHAT_SUBS_SQL, (only_chat,))
        else:
            rows = _subs_rows(conn, _SUBS_SQL)
    finally:
        conn.execute("COMMIT")
    if by_chat is None:
        grouped: dict = {}
        for row in rows:
            grouped.setdefault(row[0], []).append(tuple(row))
        by_chat = {chat: tuple(subs) for chat, subs in grouped.items()}
    return conn, version, top, newest, by_chat, subs_key


def board_signature(path, chat_id=None):
    """What a poll of ``path`` for ``chat_id`` could claim: equal signatures mean no new deliverable event and no
    change to that chat's subscription rows, so a bookkeeping commit (heartbeat, claim of another card, run
    stamps) keeps it. A board without the notify tables keys on its commit revision. Raises like any read."""
    from tui_gateway.notification_reader import reading
    key = str(path)
    with reading(Path(path)) as (conn, retained):
        version = conn.execute("PRAGMA data_version").fetchone()[0]
        with _SIGS_LOCK:
            held = _SIGS.get(key)
        if held is not None and held[0] is not conn:
            held = None  # another reader (reopened, replaced file): its scan offsets prove nothing here
        if held is None or held[1] != version:
            # A private read is never published: it needs only the asking chat's rows.
            held = _notify_content(conn, version, held, path, None if retained else chat_id)
            if held is None:
                return conn, version
            if retained:
                with _SIGS_LOCK:
                    if len(_SIGS) >= _SIGS_MAX:
                        _SIGS.clear()
                    _SIGS[key] = held
    subs = held[4] if chat_id is None else held[4].get(chat_id, ())
    return "notify", held[3], subs


def reset_board_signatures() -> None:
    from tui_gateway.notification_reader import close_all
    close_all()
    with _SIGS_LOCK:
        _SIGS.clear()


def state_revision(db):
    """A retained read-only observer sees both this process's and foreign commits."""
    from tui_gateway.kanban_replica_observer import fingerprint

    path = getattr(db, "db_path", None)
    lock = getattr(db, "_lock", None)
    if path is None or lock is None or not lock.acquire(blocking=False):
        return None  # a busy/unknown writer cannot prove committed absence
    try:
        writer = getattr(db, "_conn", None)
        # SQLite 3.54 locks sqlite3_get_autocommit; CPython's in_transaction
        # getter retains the GIL. Racing a write with a Python authorizer can
        # invert GIL/connection-mutex ownership. Properties need the writer lock too.
        if writer is not None and writer.in_transaction:
            return None
    finally:
        lock.release()
    return fingerprint(path)


def schedule_manager(session, kind, factory):
    """Reuse only proven absent schedules; active schedules keep their clock driver.

    Do not memoize a manager's swallowed read failure as absence. Prove the raw
    metadata absent separately; exceptions propagate to the poller's retry path.
    """
    from hermes_cli.goals import _get_session_db

    key = session.get("session_key") or ""
    db = _get_session_db()
    stamp = state_revision(db) if db is not None else None
    identity = (key, stamp, factory)
    cached = session.setdefault("_notif_schedule_memo", {})
    previous = cached.get(kind)
    if stamp is not None and previous is not None and previous[0] == identity:
        return previous[1]
    absent = db is not None and stamp is not None and db.get_meta(f"{kind}:{key}") is None
    manager = factory(session_id=key)
    if absent and manager.state is None:
        cached[kind] = identity, manager
    else:
        cached.pop(kind, None)
    return manager
