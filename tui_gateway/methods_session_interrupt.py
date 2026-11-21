"""Interrupt / steer / redirect session handlers (``methods_session`` split).

Moved verbatim from ``tui_gateway/methods_session.py`` (file-line ratchet #68779): the
bodies close over server.py globals through ``method_ctx.bind_module`` exactly as before —
``@method`` registration, ``_session_arg`` wrapping and module publication all still run
from the parent's ``register()`` via ``HandlerRegistry``.
"""

import re

from .method_ctx import HandlerRegistry, bind_module

_registry = HandlerRegistry()
method = _registry.method

def _resume_wake_after_interrupt() -> None:
    """Re-arm a wake lease held by the interrupt caller or a voice capture.

    ``_wake_resume_if_owner`` no-ops unless that object holds the lease, so an
    in-progress capture owned by someone else is not stolen. Interrupt already
    silenced TTS before it can return an error; this matches that cut. A
    ``not_interrupted`` hosted-task mismatch must not call it.
    """
    with _voice_sid_lock:
        voice_owner = _voice_wake_owner
    seen = []
    for owner in (_caller_transport(), voice_owner):
        if owner is None or any(owner is item for item in seen):
            continue
        seen.append(owner)
        _wake_resume_if_owner(owner)


# ── stop attribution [daveey] ────────────────────────────────────────
# A client Stop (session.interrupt) records who sent it; the interrupted turn then persists a visible
# ``turn_stopped`` notice. Without it a turn stopped by a remote client (e.g. the Redde iPhone app) ended on
# a hidden empty row and the chat simply went quiet, reading as "stuck".
_STOP_NOTICE_KIND = "turn_stopped"


def _stop_issuer_label(transport) -> str:
    """The login that sent a Stop (``basic:redde`` -> ``redde``), or ``Desktop`` for the local credential.

    Read from the transport's own login, not ``_transport_auth_user_id``: since the upstream merge that
    names the row OWNER (the host owner's OS subject for every login), which would label every remote
    Stop ``uid:<n>`` instead of the person who pressed it."""
    from gateway.session_identity import is_authenticated_identity
    identity = getattr(transport, "auth_identity", None)
    if not is_authenticated_identity(identity):
        return "Desktop"
    # An SSH-attached Desktop logs in with its per-connection ownership id (a 32-hex ``desktop-ssh`` subject,
    # one per ``~/.hermes/desktop-ssh/<id>``): that is a credential, not a person, so name the client.
    if str((identity or {}).get("provider") or "").strip() == _SSH_DESKTOP_PROVIDER:
        return "Desktop"
    return str((identity or {}).get("user_id")).strip()


# The client gesture that sent a Stop (``stop_button``, ``esc``, ``tile``, ``slash``, ``delete``,
# ``plugin:<id>``). Optional and open-ended: an unknown value is kept (made log-safe), a missing one logs ``-``.
_SSH_DESKTOP_PROVIDER = "desktop-ssh"
_STOP_ORIGIN_MAX = 64
_STOP_ORIGIN_UNSAFE = re.compile(r"[^A-Za-z0-9_.:-]")


def _stop_origin(params: dict) -> str | None:
    raw = params.get("origin")
    if not isinstance(raw, str) or not raw.strip():
        return None
    return _STOP_ORIGIN_UNSAFE.sub("_", raw.strip())[:_STOP_ORIGIN_MAX] or None


# Plain Stop gestures (desktop ``lib/stop-origin.ts``; None = a client that sends no origin, e.g. the TUI's Esc).
# Only these leave an idle session's update-held queue in place (``_interrupt_session_turn``): ``delete`` is the
# chat's explicit deletion, ``rewind``/``send_now`` interrupt as part of another operation, and a plugin's Stop
# keeps the ordinary clear.
_HELD_QUEUE_KEEPING_ORIGINS = frozenset({None, "stop_button", "esc", "tile", "tile:esc", "slash", "transcript"})


# [daveey] A Stop gesture (never ``delete``, a rewind, a plugin or an origin-less close) on a chat whose only holder
# is this host's gateway running a turn Hermes started (``gateway_background_holder``): a typed send there is queued
# behind that turn (``methods_prompt._queue_behind_background_turn``), so Stop is how the user ends the turn sooner.
_BACKGROUND_STOP_ORIGINS = frozenset({"stop_button", "esc", "tile", "tile:esc", "slash", "transcript"})


#: How long Stop waits for the take-over's answer before it reports; a later failure is still shown (an error event).
BACKGROUND_STOP_ACK_S = 5.0


def _stop_gateway_background_turn(rid, sid: str, session: dict):
    """Take the chat over from the gateway's background turn (``session.take_over``, which ends that turn).

    Returns None to leave the ordinary Stop (this backend runs the turn, another holder, or the gateway's durable
    ledger holds work for the chat: a typed prompt already queued there, whose FIFO a take-over must not race),
    True when the take-over succeeded or is still running after ``BACKGROUND_STOP_ACK_S`` (a later failure then
    emits an ``error`` event the Desktop shows), or the take-over's error dict when it failed within that time."""
    import threading
    with session["history_lock"]:
        if session.get("running"):
            return None
    if _automation_holder_kind(session) is None or _canonical_ledger_refusal(session) is not None:
        return None
    from tui_gateway.gateway_background_holder import gateway_background_holder
    if gateway_background_holder(session, foreign_owner=_foreign_turn_owner) is None:
        return None
    handler = _methods.get("session.take_over")
    if handler is None:
        return None
    key = session.get("session_key") or sid
    outcome: dict = {}
    mu, done = threading.Lock(), threading.Event()

    def run() -> None:
        error = None
        try:
            answer = handler(f"{rid}-background-stop", {"session_id": sid}) or {}
            error = answer.get("error") if isinstance(answer, dict) else None
        except Exception as exc:  # health: allow BLE001 -- worker thread boundary: logged with traceback
            logger.warning("Stop could not take %s over from the gateway's background turn", key, exc_info=True)
            error = {"message": f"Stop could not end the background turn: {type(exc).__name__}"}
        logger.info("Stop of the gateway's background turn in %s: %s", key, error or "taken over")
        with mu:
            outcome["error"] = error
            late = outcome.get("late", False)
            done.set()
        if late and error:
            message = (error.get("message") if isinstance(error, dict) else None) or str(error)
            _emit("error", sid, {"message": f"Stop could not end the background turn in this chat: {message}"})

    from agent.memory_provider import spawn_context_thread
    spawn_context_thread(run, name=f"background-stop-{str(key)[:24]}").start()
    logger.info("Stop in %s: taking the chat over from the gateway's background turn", key)
    done.wait(BACKGROUND_STOP_ACK_S)
    with mu:
        if not done.is_set():
            outcome["late"] = True
            return True
    return outcome.get("error") or True


def _note_stop_issuer(sid: str, session: dict, transport, origin: str | None = None) -> None:
    """Remember who stopped the running turn (and with which gesture) and log it; an idle session queues
    nothing. A Stop without an origin clears an earlier one, so a gesture never labels a later Stop."""
    label = _stop_issuer_label(transport)
    with session["history_lock"]:
        running = bool(session.get("running"))
        if running:
            session["_stop_issuer"] = label
            if origin:
                session["_stop_origin"] = origin
            else:
                session.pop("_stop_origin", None)
    logger.info("session.interrupt: ui_session=%s session_key=%s by=%s peer=%s running=%s origin=%s", sid,
                session.get("session_key") or "", label, getattr(transport, "_peer", "-"), running,
                origin or "-")


def _record_stop_notice(session: dict, status: str) -> dict | None:
    """After a turn ends: if a client Stop ended it, append + persist the ``turn_stopped`` notice. Always
    consumes the recorded issuer so a Stop that raced a finishing turn never labels a later one."""
    with session["history_lock"]:
        label = session.pop("_stop_issuer", None)
        origin = session.pop("_stop_origin", None)
    if not label or status != "interrupted":
        return None
    text = f"Stopped by {label}"
    content = f"[Turn stopped by {label} before it finished.]"
    metadata = {"display_text": text, "stopped_by": label}
    if origin:
        metadata["stop_origin"] = origin
    entry: dict = {"role": "user", "content": content, "display_kind": _STOP_NOTICE_KIND,
                   "display_metadata": metadata}
    with session["history_lock"]:
        session.setdefault("history", []).append(entry)
        session["history_version"] = int(session.get("history_version", 0)) + 1
    try:
        agent = session.get("agent")
        db = getattr(agent, "_session_db", None) if agent is not None else None
        if db is None:
            _ensure_session_db_row(session)
        with (contextlib.nullcontext(db) if db is not None else _session_db(session)) as db:
            if db is not None:
                from agent.context_compressor import _DB_PERSISTED_MARKER
                from agent.message_metadata import stamp_message_uid
                from hermes_state_errors import SessionTurnLeaseLostError
                target = _submit_row_target_key(session)
                try:
                    entry["_row_id"] = db.append_message(
                        session_id=target, role="user", content=content,
                        display_kind=_STOP_NOTICE_KIND, display_metadata=dict(metadata),
                        message_uid=stamp_message_uid(entry))
                except SessionTurnLeaseLostError:
                    # The turn is parked for an update: a row now would void its checkpoint, and the in-memory
                    # history alone is rebuilt from the DB on resume. Park the notice on the durable spool (it
                    # lands after the resume) and mark the live entry persisted so no flush writes it twice.
                    from gateway.shutdown_flush import defer_row_for_parked_turn
                    if defer_row_for_parked_turn(target, {k: v for k, v in entry.items() if not k.startswith("_")}) is None:
                        raise
                    logger.info("session.interrupt: stop notice for %s queued: turn parked for an update", target)
                entry[_DB_PERSISTED_MARKER] = True
    except Exception:
        logger.warning("failed to persist stop notice", exc_info=True)
    return entry


# ── interrupt / steer / redirect ─────────────────────────────────────
def _note_user_input(session: dict) -> None:
    """Mark the running turn as touched by the user (also covers compute-host turns, whose agent
    flag never reaches this process); popped by the turn's own finally."""
    with session["history_lock"]:
        if session.get("running"):
            session["_turn_user_input"] = True


def _recover_unreachable_compute_host_turn(sid: str, session: dict, inflight_at_interrupt,
                                           kept_update_held_queue: bool = False) -> None:
    """Stop on a compute host that is gone (broken pipe, respawn exhausted, #82603).

    The failed dial raised before ``_interrupt_session_turn`` purged the queue, and no ``turn.end`` will ever
    clear ``running``, so every later submit would queue forever. Clear the turn only when the supervisor owes
    no completion for it and the inflight dict is still the one this Stop saw (a successor turn keeps its own).
    The probe runs outside ``history_lock``: compute-host calls never run under it.
    """
    try:
        pending = _get_compute_host_supervisor().has_pending_turn(sid)
    except Exception as exc:  # health: allow BLE001 -- an unreadable supervisor is not proof the turn ended; fail closed
        logger.warning("session.interrupt: compute-host pending-turn probe failed for %s: %s", sid, exc)
        pending = True
    with session["history_lock"]:
        # A Stop the helper already decided keeps the update-held queue (``outcome``; a kept Stop sends no
        # interrupt frame, so only a later step can have raised) leaves it: there was no turn to recover.
        if not kept_update_held_queue:
            from tui_gateway.admission_retry import discard_update_queue_marker
            discard_update_queue_marker(session)
            session["_turn_cancel_requested"] = True
            session["queued_prompt"] = None
            session.pop("queued_prompts", None)
            session["_queued_prompt_generation"] = int(session.get("_queued_prompt_generation", 0)) + 1
        inflight_now = session.get("inflight_turn")
        if not pending and session.get("running") and inflight_now is not None and inflight_now is inflight_at_interrupt:
            session["running"] = False
            _clear_inflight_turn(session)
    _clear_pending(sid)
    with contextlib.suppress(Exception):
        _announce_cancelled_gateway_approvals(session, "interrupt", session_id=sid)
        from tools.approval import resolve_gateway_approval
        resolve_gateway_approval(session["session_key"], "deny", resolve_all=True)


@method("session.interrupt")
def _(rid, params: dict) -> dict:
    _tts_stream_stop()  # keypress barge-in also silences streaming TTS (voice is process-global)
    resume_wake = True
    try:
        session, err = _sess_nowait(params, rid)
        if err:
            return err
        if expected := _str_param(params, "expected_hosted_task_id"):
            with session["history_lock"]:
                task = session.get("_hosted_room_task")
                if not (session.get("running") and isinstance(task, dict) and task.get("task_id") == expected):
                    resume_wake = False
                    return _ok(rid, {"status": "not_interrupted", "interrupted": False})
        sid = str(params.get("session_id") or "")
        origin = _stop_origin(params)
        keep_held = origin in _HELD_QUEUE_KEEPING_ORIGINS
        _note_user_input(session)
        _note_stop_issuer(sid, session, current_transport(), origin)
        if origin in _BACKGROUND_STOP_ORIGINS and (stopped := _stop_gateway_background_turn(rid, sid, session)):
            if isinstance(stopped, dict):
                data = stopped.get("data") if isinstance(stopped.get("data"), dict) else None
                return _err(rid, 4090, str(stopped.get("message") or "Stop could not end the background turn"),
                            data)
            return _ok(rid, {"status": "interrupted", "background_turn": True})
        if _session_uses_compute_host(session):
            with session["history_lock"]:
                inflight_at_interrupt = session.get("inflight_turn")
            outcome: dict = {}
            try:
                _interrupt_session_turn(sid, session, request_id=f"interrupt-{rid}", keep_update_held_queue=keep_held,
                                        outcome=outcome, cancel_automation_replays=keep_held)
            except Exception as exc:  # health: allow BLE001 -- the bridge raises transport-specific errors unknown here; logged, and Stop recovers the session instead of failing
                logger.warning("session.interrupt: compute-host interrupt failed for %s: %s", sid, exc)
                _recover_unreachable_compute_host_turn(sid, session, inflight_at_interrupt,
                                                       bool(outcome.get("kept_update_held_queue")))
            return _ok(rid, {"status": "interrupted", "turn_isolation": True})
        # No agent wait: a turn parked on the deferred build already honors _turn_cancel_requested. Waiting here
        # failed Stop with 5032 once the build failed or outlived 30s (and started a build for a lazy session), so
        # the Desktop, which interrupts before session.close, could not delete such a session (#133262).
        outcome = {}
        _interrupt_session_turn(sid, session, keep_update_held_queue=keep_held, outcome=outcome,
                                cancel_automation_replays=keep_held)
        if outcome.get("kept_update_held_queue"):
            # Decided with the keep, under the same lock (never re-read here: the hold can lift and a drain start
            # the kept turn, with its own marker and key, after the helper returned). The kept queue's durable
            # replay is the session_key marker (reason update_hold_queue): the next backend must still send it
            # after the update, so only an earlier turn's stale key is cleared.
            stale_key = str(outcome.get("stale_marker_key") or "")
            if stale_key and stale_key != str(session.get("session_key") or ""):
                clear_turn_marker(_session_home(session), stale_key)
            return _ok(rid, {"status": "interrupted"})
        # Retire the crash-recovery marker NOW: until the run thread's finally, a backend exit looks like a crash
        # and session.resume auto-continues the turn the user just stopped (the extra key covers compression
        # rotating session_key mid-turn).
        from tui_gateway.update_pause import user_stop_wins
        user_stop_wins(session)  # [daveey] a Stop of the running turn beats the update pause its hook already paid
        with session["history_lock"]:
            active_marker_key = str(session.pop("_active_turn_marker_key", "") or "")
        _retire_turn_marker(session, active_marker_key)
        return _ok(rid, {"status": "interrupted"})
    finally:
        if resume_wake:
            try:
                _resume_wake_after_interrupt()
            except Exception:
                logger.debug("session.interrupt wake resume failed", exc_info=True)


def _apply_correction(rid, session: dict, verb: str, text: str, accepted_status: str) -> dict:
    """``agent.<verb>(text)``; on acceptance record it on the live turn (mid-turn resume rebuilds the bubble)
    and purge queued self-copies so post-turn drain cannot re-fire the old prompt."""
    try:
        accepted = getattr(session["agent"], verb)(text)
    except Exception as exc:
        return _err(rid, 5000, f"{verb} failed: {exc}")
    if accepted:
        with session["history_lock"]:
            _record_inflight_correction(session, text)
            # #84417: steer does not cancel the live original, but a server queue self-copy of that original
            # must still not re-fire after settle (same class as redirect).
            # #84417: purge server-queue self-duplicates of the live original so post-turn drain cannot
            # restart the pre-correction prompt.
            _drop_queued_duplicates_of_inflight_user(session)
            session["last_active"] = time.time()
    return _ok(rid, {"status": accepted_status if accepted else "rejected", "text": text})


def _correction_method(name: str, verb: str, accepted_status: str, supported, unsupported: str):
    """steer/redirect RPC: ``params.text`` (4002, checked before the session) into a live session;
    ``supported(agent)`` gates 4010."""
    @method(name)
    def _(rid, params: dict) -> dict:
        if not (text := (params.get("text") or "").strip()):
            return _err(rid, 4002, "text is required")
        session, err = _sess_nowait(params, rid)
        if err:
            return err
        _note_user_input(session)
        agent = session.get("agent")
        # Redirect during the turn-build window (running=True, agent None): queue for the next turn instead of
        # a misleading 4010 the client swallows into a lost follow-up.
        if verb == "redirect" and agent is None and session.get("running"):
            _enqueue_prompt(session, text, current_transport() or _stdio_transport)
            session["last_active"] = time.time()
            return _ok(rid, {"status": "queued", "text": text})
        # Compression in flight: queue instead of steering/redirecting. A correction that
        # reaches the provider mid-compression aborts the compression (explicit_interrupt)
        # — the follow-up kills the turn that would answer it (#61042). Queued here, it
        # drains when compression finishes (the Discord-gateway contract; mirrors the
        # interrupt→queue demotion in gateway/run_busy.py for the channel busy path).
        if _session_compression_in_flight(session):
            _enqueue_prompt(session, text, current_transport() or _stdio_transport)
            session["last_active"] = time.time()
            return _ok(rid, {"status": "queued", "text": text})
        if not supported(agent):
            return _err(rid, 4010, unsupported)
        # An idle agent accepts steer() but only the next turn drains it, spliced after an old tool
        # row (#64578). 'rejected' makes the client queue it as a normal next prompt.
        if verb == "steer" and not session.get("running"):
            return _ok(rid, {"status": "rejected", "text": text})
        return _apply_correction(rid, session, verb, text, accepted_status)


# Inject text into the next tool result without interrupting (AIAgent.steer(): no new user turn, no role
# alternation violation).
_correction_method("session.steer", "steer", "queued", lambda agent: hasattr(agent, "steer"),
                   "agent does not support steer")
# Redirect the active model turn while preserving valid work/context.
_correction_method("session.redirect", "redirect", "redirected",
                   lambda agent: getattr(agent, "_supports_active_turn_redirect", False) is True
                   and hasattr(agent, "redirect"), "agent does not support active-turn redirect")


def register(server) -> None:
    """Publish this module's helpers onto ``server`` (rebound to its globals) and install handlers."""
    bind_module(globals(), server, skip=("_",))
    from . import methods_session_takeover  # cross-process "Take over here" + its revocation watcher
    methods_session_takeover.register(server)
