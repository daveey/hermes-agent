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


def _profile_home(profile: Optional[str]) -> Optional[Path]:
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


def sweep_once() -> list[dict]:
    """One pass over every profile store: wake each cold chat that holds an undelivered completion, then deliver.
    Returns one record per owner it acted on. Never raises."""
    server = _server()
    out: list[dict] = []
    for profile in server._readopt_wake_profiles():
        try:
            db = pa.open_profile_db(_profile_home(profile))
        except (sqlite3.Error, OSError):
            logger.warning("producer admission: store of profile %s unavailable", profile, exc_info=True)
            continue
        if db is None:
            continue
        try:
            for owner in pa.pending_owners(db):
                out.append(_sweep_owner(server, db, profile, owner))
        except Exception:
            logger.warning("producer admission sweep failed for profile %s", profile, exc_info=True)
        finally:
            db.close()
    return out


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
