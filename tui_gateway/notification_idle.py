"""Per-session notification read memos tied to published SQLite commits. [daveey]

Only observational reads are memoized. A key is taken BEFORE reading, so a commit
racing the read invalidates the next poll. Errors never establish an idle proof.
"""
from __future__ import annotations

import contextlib
import logging

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
    from hermes_cli.kanban_session_activity import cached_ro
    from tui_gateway.session_notifications import _KANBAN_NOTIFY_KINDS
    placeholders = ",".join("?" for _ in _KANBAN_NOTIFY_KINDS)
    try:
        with cached_ro(Path(path)) as conn:
            row = conn.execute(
                "SELECT 1 FROM kanban_notify_subs AS s WHERE platform = 'tui' AND chat_id = ? "
                "AND (inflight_from IS NOT NULL OR EXISTS (SELECT 1 FROM task_events AS e "
                "WHERE e.task_id = s.task_id AND e.id > s.last_event_id "
                f"AND e.kind IN ({placeholders}))) LIMIT 1",
                (session_key, *_KANBAN_NOTIFY_KINDS),
            ).fetchone()
            return row is None
    except Exception:
        logger.debug("Kanban empty proof unavailable", exc_info=True)
        return False


def state_revision(db):
    """A retained read-only observer sees both this process's and foreign commits."""
    from tui_gateway.kanban_replica_observer import fingerprint

    path = getattr(db, "db_path", None)
    writer = getattr(db, "_conn", None)
    if path is None or (writer is not None and writer.in_transaction):
        return None
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
