"""Deliver trusted producer completions (``hermes_state_producer_admission``) into the inline chat that owns them.
[daveey]

Only the owning runtime delivers, through the existing notice channel:

- ``deliver_pending(sid, session)``: the chat's own notification poller calls it every kanban poll. It claims the
  idle turn (``_notif_claim_turn``: a busy owner keeps the notice queued until idle; nothing starts after the
  user's Stop), refuses while another process owns the chat (``_kb_owner_refusal``, then the active-session slot
  WITHOUT taking over a detached owner's lease), writes ONE ``process_complete`` notice row on the
  conversation's live tip and marks it committed in the same txn, then runs it as a synthesized turn that adopts
  that row (``_submit_user_row``). A refused dispatch leaves it committed: the next pass re-stages the same row.
- ``sweep_once()``: a Desktop/serve backend's sweeper wakes a cold chat holding a queued completion with an
  in-process ``session.resume`` (the kanban unloaded-chat path); a resume refused because another backend holds
  the chat leaves the row queued for that owner.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

import hermes_state_producer_admission as pa
from agent.memory_provider import spawn_context_thread
from agent.local_work_agent import scope as work_scope
from hermes_cli import loop_accounting as _la

logger = logging.getLogger(__name__)

INTERVAL_S = 15.0
_sweeper_lock = threading.Lock()
_sweeper_started = False


def _server():
    from tui_gateway import server
    return server


def _notice_message(row: dict) -> tuple[dict, dict]:
    from agent.message_metadata import stamp_message_timestamp, stamp_message_uid
    metadata = {"display_text": row["text"], "producer_admission": row["producer"],
                "notification_category": "result"}
    staged = stamp_message_timestamp({"role": "user", "content": row["text"], "display_kind": pa.DISPLAY_KIND,
                                      "display_metadata": metadata})
    stamp_message_uid(staged)
    return staged, metadata


def _restaged(row: dict, message: dict) -> tuple[dict, dict]:
    import json
    metadata = message.get("display_metadata")
    if isinstance(metadata, str):
        with contextlib.suppress(ValueError):
            metadata = json.loads(metadata)
    if not isinstance(metadata, dict):
        metadata = {"display_text": row["text"], "producer_admission": row["producer"]}
    staged = {"role": "user", "content": message["content"], "display_kind": pa.DISPLAY_KIND,
              "display_metadata": metadata, "timestamp": message.get("timestamp"),
              "message_uid": message.get("message_uid"), "_row_id": message["id"]}
    return staged, metadata


def deliver_pending(sid: str, session: dict) -> str:
    """Deliver this chat's oldest undelivered producer completion as one turn. Returns a verdict for tests and
    logs: ``none``, ``busy``, ``refused``, ``moved``, ``started``, ``dispatch_refused``, ``settled`` or
    ``error``. Never raises."""
    server = _server()
    if session.get("_finalized") or session.get("_closing") or session.get("_lease_taken_over"):
        return "none"
    try:
        with server._session_db(session) as db:
            if db is None:
                return "none"
            target = server._submit_row_target_key(session)
            from tui_gateway.notification_idle import pending_producers
            rows = pending_producers(db, session, target)
    except Exception:
        logger.warning("producer admission: pending read failed", exc_info=True)
        return "error"
    if not rows:
        return "none"
    if not server._notif_claim_turn(session):
        return "busy"
    claimed = True
    try:
        if server._kb_owner_refusal(sid, session) is not None:
            return "refused"
        if server._ensure_active_session_slot(sid, session, allow_takeover=False) is not None:
            server._notif_note_owner_refusal(session, "SESSION_NOT_OWNED")
            return "refused"
        row = rows[0]
        from hermes_state_errors import CompressionSessionClosedError, SessionTurnLeaseLostError
        with server._session_db(session) as db:
            if db is None:
                return "none"
            target = server._submit_row_target_key(session)
            if row["status"] == pa.COMMITTED:
                if pa.turn_followed(db, row):
                    pa.mark_dispatched(db, row["producer"])
                    return "settled"
                message = pa.committed_message(db, row)
                if message is None:
                    return "error"
                staged, metadata = _restaged(row, message)
                target = row["delivered_session_id"]
            else:
                staged, metadata = _notice_message(row)
                try:
                    verdict, _row_id = pa.commit_delivery(db, row["producer"], target, staged)
                except (SessionTurnLeaseLostError, CompressionSessionClosedError):
                    return "busy"
                if verdict != "written":  # moved, or another deliverer settled it first
                    return "moved" if verdict == "moved" else "settled"
            with contextlib.suppress(Exception):
                from hermes_state_outcome import refresh_outcomes_quietly
                refresh_outcomes_quietly([target], "notice", db=db)
        from agent.context_compressor import _DB_PERSISTED_MARKER
        staged[_DB_PERSISTED_MARKER] = True
        staged[server._SUBMIT_ROW_SESSION_KEY] = target
        with session["history_lock"]:
            session["_submit_user_row"] = staged
            # Nothing irreversible ran before the first tool: a restart then continues the turn (prompt.admit).
            session["_marker_until_tool"] = True
        claimed = False  # _notif_submit owns the claim now (it releases on refusal)
        with work_scope("sweep", "producer_admission._notif_submit"):
            accepted = server._notif_submit(f"__producer__{int(time.time() * 1000)}", sid, session,
                                            staged["content"], "producer completion dispatch failed",
                                            display_kind=pa.DISPLAY_KIND, display_metadata=metadata)
        if not accepted:
            with session["history_lock"]:
                if session.get("_submit_user_row") is staged:
                    session.pop("_submit_user_row", None)
            return "dispatch_refused"
        with server._session_db(session) as db:
            if db is not None:
                pa.mark_dispatched(db, row["producer"])
        return "started"
    except Exception:
        logger.warning("producer admission: delivery failed for %s", session.get("session_key"), exc_info=True)
        return "error"
    finally:
        if claimed:
            server._notif_release_turn(session)


def _profile_home(profile: Optional[str]) -> Path:
    from hermes_constants import get_default_hermes_root
    root = Path(get_default_hermes_root())
    return root if not profile else root / "profiles" / profile


def _live(server, key: str, profile: Optional[str]):
    home = None if not profile else str(_profile_home(profile))
    with contextlib.suppress(Exception):
        live = server._find_live_session_by_key(key, profile_home=home)
        if live is not None:
            return live
    return None


# Change gate (design t_01a1d563324d7e8e §3 card J). Opening every profile's SessionDB each pass was 13 of 17
# samples of this loop (~0.7 s CPU per 15 s pass on the hub). Each profile store keeps ONE read-only observer
# connection; a pass reads its ``PRAGMA data_version`` (moves on every commit by any other connection, this
# process's writers included) and file identity. A store is skipped only when that key is unchanged AND its last
# pass found no pending owner: a pending row is due again every pass (a refused resume or a busy owner retries on
# the next tick), so it never waits on a commit. path -> ((dev, ino), conn); path -> (key, had_pending).
_observers: dict[str, tuple[tuple[int, int], sqlite3.Connection]] = {}
_last_pass: dict[str, tuple[tuple, bool]] = {}
_observers_lock = threading.Lock()
_PENDING_OWNERS_SQL = "SELECT DISTINCT owner FROM producer_admissions WHERE status IN (?, ?)"


def _drop_observer(path: str) -> None:
    entry = _observers.pop(path, None)
    _last_pass.pop(path, None)
    if entry is not None:
        with contextlib.suppress(sqlite3.Error):
            entry[1].close()


def reset_observers() -> None:
    """Close every kept observer connection (tests, shutdown)."""
    with _observers_lock:
        for path in list(_observers):
            _drop_observer(path)
        _last_pass.clear()


def _observe(path: Path) -> tuple[tuple, sqlite3.Connection]:
    """``(key, conn)`` for ``path``'s store: key = (dev, ino, data_version) on the kept observer connection,
    reopened when the file was replaced. Raises OSError/sqlite3.Error when the store cannot be read."""
    from hermes_state_holders import read_only_db_uri
    st = path.stat()
    identity = (st.st_dev, st.st_ino)
    entry = _observers.get(str(path))
    if entry is not None and entry[0] != identity:
        _drop_observer(str(path))
        entry = None
    if entry is None:
        # Tracked: a long-held fd must be visible to has_live_connection()/offline_file_access() (restores, header
        # byte probes), like every other state.db opener.
        from hermes_cli.sqlite_safe_read import connect_tracked
        conn = connect_tracked(read_only_db_uri(path), tracking_path=path, uri=True, check_same_thread=False,
                               isolation_level=None, timeout=0.5)
        entry = _observers[str(path)] = (identity, conn)
    version = entry[1].execute("PRAGMA data_version").fetchone()[0]
    return (*identity, version), entry[1]


def _pending_owners_on(conn: sqlite3.Connection) -> list[str]:
    """``pa.pending_owners`` on the observer connection (autocommit: reads the latest committed state)."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='producer_admissions'").fetchone() is None:
        return []
    return [r[0] for r in conn.execute(_PENDING_OWNERS_SQL, pa.PENDING).fetchall()]


def _gate(path: Path) -> Optional[tuple]:
    """None to skip ``path`` this pass; else ``(key, owners)`` where ``owners`` is None when the observer could not
    answer (the caller then takes today's full path). Called under ``_observers_lock``."""
    try:
        key, conn = _observe(path)
    except (OSError, sqlite3.Error):
        _drop_observer(str(path))
        return (None, None)
    last = _last_pass.get(str(path))
    if last is not None and last[0] == key and not last[1]:
        return None
    try:
        return key, _pending_owners_on(conn)
    except sqlite3.Error:
        _drop_observer(str(path))
        return (None, None)


@_la.accounted("producer_admission.sweep", rows=True)
def sweep_once() -> list[dict]:
    """One pass over every profile store: wake each cold chat that holds an undelivered completion, then deliver.
    Returns one record per owner it acted on. Never raises. A store whose commit key is unchanged since a pass
    that found nothing pending is skipped without opening it (see ``_observers``)."""
    server = _server()
    out: list[dict] = []
    profiles = [(profile, _profile_home(profile)) for profile in server._readopt_wake_profiles()]
    with _observers_lock:
        live = {str(home / "state.db") for _profile, home in profiles}
        for path in [p for p in _observers if p not in live]:
            _drop_observer(path)
    for profile, home in profiles:
        path = home / "state.db"
        with _la.pass_("producer_admission.profile") as acct, _observers_lock:
            gate = _gate(path)
            if gate is None:
                acct.skip()
                continue
            key, owners = gate
            acct.change(key is not None)
            if owners is not None and not owners:
                _last_pass[str(path)] = (key, False)
                continue
            acct.fallback(owners is None)
        had_pending = _sweep_profile(server, profile, home, out)
        if key is not None:
            with _observers_lock:
                _last_pass[str(path)] = (key, had_pending)
    return out


def _sweep_profile(server, profile: Optional[str], home: Path, out: list) -> bool:
    """Today's full pass over one store. True unless it proved nothing is pending (an error counts as pending,
    so the next pass retries)."""
    try:
        db = pa.open_profile_db(home)
    except (sqlite3.Error, OSError):
        logger.warning("producer admission: store of profile %s unavailable", profile, exc_info=True)
        return True
    if db is None:
        return False
    try:
        owners = pa.pending_owners(db)
        for owner in owners:
            out.append(_sweep_owner(server, db, profile, owner))
        return bool(owners)
    except Exception:
        logger.warning("producer admission sweep failed for profile %s", profile, exc_info=True)
        return True
    finally:
        db.close()


def _sweep_owner(server, db, profile: Optional[str], owner: str) -> dict:
    record = {"profile": profile, "owner": owner}
    if db.get_session(owner) is None:
        for row in pa.pending_for_owner(db, owner):
            pa.mark_missing(db, row["producer"])
        return {**record, "verdict": "missing"}
    tip = db.get_compression_tip(owner) or owner
    live = _live(server, tip, profile)
    if live is None:
        # A turn paused for an update is its restarting backend's to continue (update_pause); resuming the chat
        # here would run it in a process its viewers are not attached to. The row stays queued for the next pass.
        from tui_gateway.update_pause import successor_owes_continuation
        if any(successor_owes_continuation(_profile_home(profile), k) for k in {owner, tip}):
            logger.info("producer admission: %s is paused for a Hermes update and its backend is restarting; "
                        "left to that backend's continuation", tip)
            return {**record, "verdict": "paused_turn"}
        handler = server._methods.get("session.resume")
        if handler is None:
            return {**record, "verdict": "no_resume"}
        params = {"session_id": tip, "omit_messages": True, **({"profile": profile} if profile else {})}
        with work_scope("sweep", "producer_admission.session.resume"):
            env = handler(f"producer-wake-{int(time.time() * 1000)}", params) or {}
        if isinstance(env, dict) and env.get("error"):
            logger.info("producer admission: resume of %s refused (%s); it stays queued", tip, env.get("error"))
            return {**record, "verdict": "resume_refused"}
        live = _live(server, tip, profile)
        if live is None:
            return {**record, "verdict": "not_live"}
    sid, session = live
    if not session.get("agent") and not session.get("agent_error"):
        with work_scope("sweep", "producer_admission.agent_build"):
            server._start_agent_build(sid, session)
            if server._wait_agent(session, f"producer-wake-{sid}", timeout=120.0):
                return {**record, "verdict": "build_failed"}
    return {**record, "verdict": deliver_pending(sid, session)}


LEASE_NAME = "producer-admission"


def run_pass(lease) -> bool:
    """One tick: the cold-chat sweep only while this process holds the ``producer-admission`` host loop lease
    (design t_01a1d45d75851804 §2b; ``HERMES_HOST_LOOP_LEASE=0`` = every process). The per-chat poller's
    ``deliver_pending`` is unchanged. True when the sweep ran."""
    if not lease.held():
        return False
    try:
        sweep_once()
    finally:
        lease.beat()
    return True


def _loop(stop: threading.Event) -> None:
    from hermes_cli.host_loop_lease import HostLoopLease, process_role
    lease = HostLoopLease(LEASE_NAME, process_role(), interval=INTERVAL_S)
    stop.wait(INTERVAL_S)
    try:
        while not stop.is_set():
            try:
                run_pass(lease)
            except Exception:
                logger.warning("producer admission sweep failed", exc_info=True)
            stop.wait(INTERVAL_S)
    finally:
        lease.release()


def start_producer_admission_sweeper() -> Optional[threading.Event]:
    """Start the once-per-process cold-chat sweeper (idempotent)."""
    global _sweeper_started
    with _sweeper_lock:
        if _sweeper_started:
            return None
        _sweeper_started = True
    stop = threading.Event()
    spawn_context_thread(_loop, args=(stop,), name="producer-admission-sweeper").start()
    return stop
