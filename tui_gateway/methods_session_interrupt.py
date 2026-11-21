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


# Also the user's own forwarded prompt, caught before its user row is written: so no "not started by Hermes".
BACKGROUND_STOP_TYPED_MESSAGE = ("The turn running in this chat wasn't started automatically, so Stop left it "
                                 "running. Stop it from the window that started it, or wait for it to finish.")
BACKGROUND_STOP_QUEUED_MESSAGE = ("The background turn in this chat could not be stopped from here (the gateway "
                                  "running it is older). Your message runs when that turn ends.")


def _stop_gateway_background_turn(rid, sid: str, session: dict) -> bool:
    """A Stop on a chat whose turn Hermes started in ANOTHER process. Only cheap checks run here (this backend runs
    no turn, the chat's newest turn is automated); the holder discovery and the end itself run on a worker
    (``_end_background_turn``) and report by events, so Stop never blocks the RPC reader. The ordinary Stop runs
    either way; True only says the worker was started."""
    with session["history_lock"]:
        if session.get("running"):
            return False
    if _automation_holder_kind(session) is None:
        return False
    queued = _canonical_ledger_refusal(session) is not None  # a typed prompt already waits in the gateway's FIFO
    from agent.memory_provider import spawn_context_thread
    key = session.get("session_key") or sid
    spawn_context_thread(lambda: _end_background_turn(rid, sid, session, queued),
                         name=f"background-stop-{str(key)[:24]}").start()
    logger.info("Stop in %s: ending the gateway's background turn off the RPC thread", key)
    return True


def _end_background_turn(rid, sid: str, session: dict, queued: bool) -> str:
    """Worker: end exactly the holder's turn. Returns what it did (tests); every failure reaches the chat as an
    ``error`` event.

    - The holder is the gateway the chat's forward dials, running an automated turn (``:auto=`` holder), and that
      gateway honours end requests (``turn_end_request.CAPABILITY``): record the request for that exact holder
      (``SessionDB.request_turn_end``, compare-and-set). Its turn ends with an ordinary interrupt and releases
      the lease, so a prompt queued behind it runs next, once. Whether or not a prompt is queued.
    - A capable gateway running an UNSTAMPED turn: somebody typed it; nothing is ended, the chat is told so.
    - An older gateway (not capable): with nothing queued, ``session.take_over`` (as before); with a prompt queued
      in its FIFO a take-over would race that queue, so the chat is told the message runs when the turn ends.
    - Anything else (no such holder any more, another holder): nothing is ended."""
    from agent.turn_end_request import CAPABILITY, holder_auto_kind
    from tui_gateway.gateway_background_holder import gateway_background_holder
    key = str(session.get("session_key") or sid)
    try:
        gateway = gateway_background_holder(session, foreign_owner=_foreign_turn_owner)
        owner = _foreign_turn_owner(session) if gateway is not None else None
    except Exception:  # health: allow BLE001 -- worker thread boundary: logged with traceback, told to the chat
        logger.warning("Stop: could not find who runs %s", key, exc_info=True)
        gateway = owner = None
    holder = str((owner or {}).get("holder") or "")
    if gateway is None or not holder:
        # The ordinary Stop already ran for this chat; there is no background turn left to end.
        logger.info("Stop in %s: no background turn of this host's gateway holds it now; nothing to end", key)
        return "no_holder"
    capable = CAPABILITY in (getattr(gateway.endpoint, "capabilities", None) or ())
    if capable and holder_auto_kind(holder) is None:
        # A gateway that stamps automated turns ran this one unstamped: somebody typed it. Never ended from here.
        _emit("error", sid, {"message": BACKGROUND_STOP_TYPED_MESSAGE})
        return "typed_turn"
    if capable:
        try:
            with _session_db(session) as db:
                recorded = bool(db.request_turn_end(key, holder, reason="stop"))
        except Exception:  # health: allow BLE001 -- worker thread boundary: logged with traceback, told to the chat
            logger.warning("Stop: could not record the end request for %s", key, exc_info=True)
            recorded = False
        if recorded:
            logger.info("Stop in %s: asked the gateway's background turn to end (holder %s)", key, holder)
            return "end_requested"
        _emit("error", sid, {"message": "Stop could not reach the background turn in this chat; it may have "
                                        "just ended. Try Stop again if it is still running."})
        return "end_request_failed"
    if queued:  # only an older (not capable) gateway reaches here
        _emit("error", sid, {"message": BACKGROUND_STOP_QUEUED_MESSAGE})
        return "queued_not_interrupted"
    handler = _methods.get("session.take_over")
    try:
        answer = handler(f"{rid}-background-stop", {"session_id": sid}) if handler else None
    except Exception:  # health: allow BLE001 -- worker thread boundary: logged with traceback, told to the chat
        logger.warning("Stop could not take %s over from the gateway's background turn", key, exc_info=True)
        answer = {"error": {"message": "the take-over failed"}}
    error = answer.get("error") if isinstance(answer, dict) else {"message": "no take-over handler"}
    if error:
        message = (error.get("message") if isinstance(error, dict) else None) or str(error)
        _emit("error", sid, {"message": f"Stop could not end the background turn in this chat: {message}"})
        return "takeover_failed"
    logger.info("Stop in %s: took the chat over from an older gateway's background turn", key)
    return "taken_over"


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
        # [daveey] An ADDITION to the ordinary Stop below (which still cancels this chat's automation replays and
        # post-turn review): a worker ends the gateway's background turn and reports by events. ``background_turn``
        # in the answer is only a hint that it was started.
        hint = ({"background_turn": True} if origin in _BACKGROUND_STOP_ORIGINS
                and _stop_gateway_background_turn(rid, sid, session) else {})
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
            return _ok(rid, {"status": "interrupted", "turn_isolation": True, **hint})
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
            return _ok(rid, {"status": "interrupted", **hint})
        # Retire the crash-recovery marker NOW: until the run thread's finally, a backend exit looks like a crash
        # and session.resume auto-continues the turn the user just stopped (the extra key covers compression
        # rotating session_key mid-turn).
        from tui_gateway.update_pause import user_stop_wins
        user_stop_wins(session)  # [daveey] a Stop of the running turn beats the update pause its hook already paid
        with session["history_lock"]:
            active_marker_key = str(session.pop("_active_turn_marker_key", "") or "")
        _retire_turn_marker(session, active_marker_key)
        return _ok(rid, {"status": "interrupted", **hint})
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
