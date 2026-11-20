"""Auto-continue: resume a turn killed by a process/machine death, plus queued-prompt drain and
busy-submit handling. Bodies are rebound onto server.py's globals at install time
(method_ctx.bind_module), so they reference server.py globals bare."""

from __future__ import annotations

import contextlib
import os

from .method_ctx import bind_module

# A concluded turn (success, handled error, interrupt) clears its durable marker (turn_marker.py) in _run_prompt_submit's
# finally; only a process death leaves it behind, so a marker at session.resume proves the turn never finished AND the
# client never saw a terminal frame. Fresh: re-submit automatically (as the messaging gateway does). Stale: clear it
# and let the partial transcript speak.
# If the interruption is fresh, re-submit the interrupted prompt automatically (the messaging gateway has
# done this for restart-interrupted sessions since #27856); if it's stale, clear the marker and let the
# recovered partial transcript speak for itself — the user can ask to continue manually. The bounds (15 min and
# 2 attempts by default, ``desktop.auto_continue``) are parsed by turn_marker.auto_continue_bounds.
# [daveey] Waits before each replay of a turn refused for a retryable reason (a stale ownership lease during a core
# update, a rate limit). One per attempt; ``desktop.auto_continue.max_attempts`` can only lower the count.
_REFUSED_RETRY_DELAYS_S = (45.0, 180.0)
# A queued envelope accepted behind the core-update admission hold carries why it waits and since when
# (``_queue_submit_behind_update_hold``); the same keys go out on the prompt.submit reply and the snapshot.
QUEUED_REASON_KEY = "queued_reason"
QUEUED_STARTED_AT_KEY = "queued_started_at"
UPDATE_HOLD_QUEUED_REASON = "core_update"


def _resume_reuse_live_locked(ctx, sid: str, session: dict) -> dict:
    """Warm resume recovery, with the caller holding _session_resume_lock."""
    if (refusal := _reattach_refusal(ctx.rid, sid, session)) is not None:
        return refusal
    _cancel_ws_orphan_reap(sid)
    payload = _live_session_payload(sid, session, cols=ctx.cols, touch=True, omit_messages=ctx.omit_messages,
                                    transport=current_transport() or _stdio_transport,
                                    inline_images=ctx.inline_images)
    payload["resumed"] = ctx.target
    # A boot sweep can race Desktop's mount or update hold. Retry on warm reuse too,
    # but don't make stale code busy again before its replacement can take over.
    if not ctx.lazy and not session.get("running") and not session.get("resume_hydrating"):
        from tui_gateway.update_pause import _detect_skew
        if _detect_skew() is None:
            # Caller holds _session_resume_lock: one short update-lock try, never the busy-lock retry.
            from hermes_cli.backend_retirement import single_lock_try
            with single_lock_try():
                continuation = _maybe_schedule_auto_continue(sid, session, ctx.target)
            if continuation is not None:
                payload["auto_continue"] = continuation
    if ctx.defer_history:
        payload.update(messages=[], hydrating=bool(session.get("resume_hydrating")),
                       message_count=int(session.get("resume_message_count") or payload["message_count"]))
    # A lazy watch session never owns a run loop — overlay the child-run registry.
    if session.get("agent") is None and _child_run_active(ctx.target, ctx.profile_home):
        payload.update(running=True, status="streaming")
    return _ok(ctx.rid, payload)


def _auto_continue_config() -> tuple[bool, float, int]:
    """(enabled, freshness window in seconds, max attempts) from ``desktop.auto_continue`` in config.yaml."""
    from tui_gateway.turn_marker import auto_continue_bounds
    return auto_continue_bounds(_load_cfg())


def _session_home(session: dict) -> Path:
    """The HERMES_HOME the session's durable state lives in (profile-aware)."""
    return Path(session.get("profile_home") or _hermes_home)


def _retire_turn_marker(session: dict, *keys: str, concluding_turn: frozenset | None = None) -> None:
    """Drop the crash marker right before the terminal frame (not at turn-thread end: post-turn work outlives the
    client's answer, and quitting in that window would leave a marker that re-runs a finished turn). Extra ``keys``
    cover a session_key that compression rotated mid-turn. A turn stopped by backend shutdown keeps its marker
    (``_stop_turns_before_exit`` flags it) so the next boot auto-continues it like a crash."""
    if session.get("_exit_keeps_turn_marker") or session.get("_refused_turn_marker_key"):
        return
    # [daveey] A core-update pause keeps its marker: the next backend (or the in-place watcher) continues it.
    if session.get("_update_pause_marker_key") or session.get("_paused_marker_carried"):
        return
    home = _session_home(session)
    for key in dict.fromkeys((*keys, str(session.get("session_key") or ""))):
        if key:
            if concluding_turn is None:
                clear_turn_marker(home, key)
            else:
                clear_turn_marker(home, key, concluding_turn=concluding_turn)


def _hold_refused_turn(session: dict, marker_key: str, display_kind, error_surface) -> None:
    """[daveey] Keep the marker of a turn refused for a retryable reason, so the crash path replays it instead of the
    user's message ending on "send it again": ``_schedule_refused_turn_retry`` replays it here, the boot sweep replays
    it if the backend goes first. Both keep the auto-continue guards. Only a person's turn or its replay; a later
    refused turn joins the earlier prompt, because one marker carries one replay."""
    if not (error_surface or {}).get("retryable") or display_kind not in (None, "auto_continue"):
        return
    if session.get("source") == "bot_room":
        return
    home = _session_home(session)
    marker = read_turn_marker(home, marker_key) if marker_key else None
    enabled, _fresh, max_attempts = _auto_continue_config()
    if not enabled or marker is None or not marker.get("auto_continue", True):
        return
    attempts = marker["attempts"]
    if attempts >= min(max_attempts, len(_REFUSED_RETRY_DELAYS_S)) or _active_goal_manager(session) is not None:
        return  # a goal has its own bounded retry (``_goal_followup_after_turn``)
    prompt, earlier = marker["prompt"], session.pop("_refused_turn_earlier", None)
    if earlier:
        prompt = f"{earlier}\n\n{prompt}"
        record_turn_start(home, marker_key, prompt, attempts=attempts)
    delay = _REFUSED_RETRY_DELAYS_S[attempts]
    session.update(_refused_turn_marker_key=marker_key, _refused_turn_prompt=prompt, _refused_turn_delay=delay,
                   _auto_continue_retry_after=time.monotonic() + delay - 1.0)


def _schedule_refused_turn_retry(sid: str, session: dict, marker_key: str,
                                 concluding: frozenset = frozenset(), *, outcome_sid: str | None = None) -> None:
    """Start the wait ``_hold_refused_turn`` armed. A skewed backend leaves the marker for its replacement's boot
    sweep; a replay that can no longer run (the marker aged out) settles the chat's outcome. ``concluding``: the
    refused turn's own evidence (``turn_outcome.concluding_turn_identity``); a turn the user starts during the wait
    is a live turn the refresh must not paint over."""
    delay = session.pop("_refused_turn_delay", None)
    if delay is None:
        return

    if outcome_sid is None:
        from tui_gateway.turn_outcome import turn_sid
        outcome_sid = turn_sid(session, session.get("agent"))

    def fire() -> None:
        from tui_gateway.update_pause import _detect_skew
        if _detect_skew() is not None:
            return
        if _maybe_schedule_auto_continue(sid, session, marker_key) is None \
                and read_turn_marker(_session_home(session), marker_key) is None:
            from tui_gateway.turn_outcome import refresh_turn_outcome
            refresh_turn_outcome(session, concluding_turn=concluding, session_id=outcome_sid)

    timer = threading.Timer(delay, fire)
    timer.daemon = True
    timer.start()


def _auto_continue_note(prompt: str) -> str:
    # Same opening as the gateway's recovery notes (transcript tooling recognizes both). The prompt is embedded: a hard
    # crash persists nothing else of the turn.
    return (f"{_AUTO_CONTINUE_NOTE_PREFIX} — the app or its backend process stopped before the turn could finish. "
            "Some of the work may already be complete; check the current state before redoing anything, then "
            f"finish the task. The interrupted request was:]\n\n{prompt}")


# [daveey] Surfaces a client-less continuation adopts from the stored row (t_01a183149a00c941). No client is
# attached when a boot sweep or the in-place watcher continues a turn, so the env fallback says nothing about
# where the user reads it; anything else (missing, unknown) keeps today's default.
_STORED_CONTINUATION_SOURCES = frozenset({"desktop", "tui"})


def _stored_continuation_source(row) -> str | None:
    """The stored ``sessions.source`` a continuation should run as, or None to keep the current default."""
    source = str((row or {}).get("source") or "").strip() if isinstance(row, dict) else ""
    return source if source in _STORED_CONTINUATION_SOURCES else None


def _read_stored_continuation_source(session: dict, session_key: str) -> str | None:
    """``_stored_continuation_source`` of the session's state.db row (profile-aware); None on any read failure."""
    db = getattr(session.get("agent"), "_session_db", None)
    owns = False
    try:
        if db is None:
            db, owns = _profile_session_db(session.get("profile_home"))
        return _stored_continuation_source(db.get_session(session_key) if db is not None else None)
    except Exception:
        logger.debug("auto-continue: stored source of %s unreadable", session_key, exc_info=True)
        return None
    finally:
        if owns:
            _release_db(db)


def _adopt_stored_continuation_source(sid: str, session: dict, session_key: str) -> None:
    """Run the continuation as the session's stored surface: set the record's source, and rebuild an agent that
    was already built for another platform (the in-place watcher continues the agent that ran the paused turn)."""
    stored = _read_stored_continuation_source(session, session_key)
    if stored is None:
        return
    previous = _session_source(session)
    if previous != stored:
        logger.info("auto-continue for %s runs as stored source %s (record had %s)", session_key, stored, previous)
        session["source"] = stored
    agent = session.get("agent")
    if agent is None or getattr(agent, "platform", stored) == stored:
        return
    tokens = _set_session_context(sid, cwd=_session_cwd(session))
    try:
        _rebuild_session_agent(sid, session, session_id=session_key, platform_override=stored,
                               context_cwd_is_launch_artifact=_context_cwd_is_launch_artifact(session))
    except Exception:
        logger.warning("auto-continue for %s: agent rebuild for source %s failed; continuing on platform %s",
                       session_key, stored, getattr(agent, "platform", "?"), exc_info=True)
    finally:
        _clear_session_context(tokens)


from tui_gateway.turn_marker import UPDATE_HOLD_QUEUE_MAX_AGE_S, UPDATE_HOLD_QUEUE_REASON


def _maybe_schedule_auto_continue(sid: str, session: dict, session_key: str) -> dict | None:
    """Kick off a continuation turn for a crash-interrupted session (session.resume cold paths). Returns a descriptor
    for the resume payload when scheduled, else None. The turn runs on a background thread after the deferred agent
    build via _run_prompt_submit, so the client that just resumed streams it."""
    # Hosted room turns are recovered by their durable task/lease state machine; generic auto-continue would bypass
    # its execution generation and duplicate work.
    if session.get("source") == "bot_room":
        return None
    home = _session_home(session)
    if (marker := read_turn_marker(home, session_key)) is None:
        return None
    if not marker.get("auto_continue", True):
        return None  # The mailbox owns recovery and receipt identity for imported turns.
    # Ownership, not forensics: a sibling backend sharing this HERMES_HOME can be mid-turn on this very session, so
    # its live marker says "someone is working on it", never "someone crashed". Leave the marker for its writer —
    # clearing it would cancel the live turn's own account of itself. See #94778.
    writer_state = marker_writer_state(marker)
    if writer_state == "alive" and marker.get("writer_pid") != os.getpid():
        logger.info("auto-continue for %s held back: marker writer pid %s is still alive (reader pid %s); the "
                    "marker is ownership evidence and the owner clears it", session_key,
                    marker.get("writer_pid"), os.getpid())
        return None
    from tui_gateway.update_pause import drain_retry_pending
    from tui_gateway.turn_marker import freshness_since
    if drain_retry_pending(session, marker):
        return None
    enabled, freshness_secs, max_attempts = _auto_continue_config()
    age = time.time() - freshness_since(marker)
    # [daveey] A background-job receipt is a delivery, not a turn to redo: it keeps longer than a turn.
    from tui_gateway.exit_job_receipts import JOBS_REASON, JOBS_RECEIPT_MAX_AGE_S, jobs_stopped_note
    receipt = marker.get("reason") == JOBS_REASON
    # An update-hold queued prompt waited out the drain window + apply: its own bound (turn_marker.py).
    max_age = (JOBS_RECEIPT_MAX_AGE_S if receipt
               else max(freshness_secs, UPDATE_HOLD_QUEUE_MAX_AGE_S) if marker.get("reason") == UPDATE_HOLD_QUEUE_REASON
               else freshness_secs)
    if not enabled or age > max_age or marker["attempts"] >= max_attempts:
        clear_turn_marker(home, session_key)  # stale/disabled/crash-looping: a manual message continues
        return None
    with session["history_lock"]:
        if (session.get("_auto_continue_scheduled")
                or (marker.get("reason") == "core_update_pause" and session.get("running"))
                # A warm resume (renderer reload) of the backend that queued it: the live queue still holds the
                # prompt and drains it when the hold lifts; replaying the marker too sent it twice.
                or (marker.get("reason") == UPDATE_HOLD_QUEUE_REASON and session.get("queued_prompt"))
                or time.monotonic() < float(session.get("_auto_continue_retry_after") or 0)
                or session.get("_closing") or session.get("_finalized") or session.get("resume_hydrating")):
            return None
        session["_auto_continue_scheduled"] = True
    # [daveey] A core-update pause is not a crash: no attempt is used and the note says the step finished.
    paused = marker.get("reason") == "core_update_pause"
    # A prompt accepted behind the update hold never started: replay it verbatim, no attempt used, no note.
    held = marker.get("reason") == UPDATE_HOLD_QUEUE_REASON
    if held:
        attempt, text = marker["attempts"], marker["prompt"]
    elif paused:
        from tui_gateway.update_pause import later_turns_since, pause_note
        attempt, text = marker["attempts"], pause_note(
            marker["prompt"], marker.get("killed_jobs"), carried=bool(marker.get("carried_turn_paused")),
            paused_at=marker.get("paused_at"), pauses=int(marker.get("pauses") or 0),
            later_turns=later_turns_since(session, session_key, marker.get("paused_at"), marker.get("prompt")))
    elif receipt:
        attempt, text = marker["attempts"], jobs_stopped_note(marker.get("killed_jobs"))
    else:
        attempt, text = marker["attempts"] + 1, _auto_continue_note(marker["prompt"])
    if not (paused or receipt) and marker.get("killed_jobs"):
        from tui_gateway.update_pause import killed_jobs_report
        text += "\n\n" + killed_jobs_report(marker["killed_jobs"], "Background jobs stopped when the turn was "
                                            "interrupted:")
    pauses = int(marker.get("pauses") or 0)

    def kickoff() -> None:
        rid = f"__auto_continue__{int(time.time() * 1000)}"
        try:
            with contextlib.suppress(Exception):  # [daveey] before the build, so a deferred build gets it right
                _adopt_stored_continuation_source(sid, session, session_key)
            _start_agent_build(sid, session)
            err = _wait_agent(session, rid, timeout=120.0)
            if not err:  # a build already in flight (or the paused turn's agent) may predate the adoption
                with contextlib.suppress(Exception):
                    _adopt_stored_continuation_source(sid, session, session_key)
        except Exception:
            logger.warning("auto-continue agent build failed for %s", sid, exc_info=True)
            err = {"error": {"message": "agent build failed"}}
        if err:  # leave the marker: the next resume retries (bounded by attempts)
            session["_auto_continue_scheduled"] = False
            return
        with session["history_lock"]:
            if session.get("running") or session.get("_turn_cancel_requested") or session.get("_finalized"):
                session["_auto_continue_scheduled"] = False  # a real user prompt beat us; it clears the marker
                return
            session["running"] = True
            session["_notif_turn_claim"] = claim = object()  # the exception release below frees only this claim
            session["last_active"] = time.time()
        # Ownership admission BEFORE message.start: a sibling backend sharing this HERMES_HOME may have written the
        # marker and still be mid-turn. Leave the marker so a later resume retries.
        # Running the continuation anyway would be the double-writer this fence exists to prevent. See
        # #94778.
        # A paused continuation takes over only an idle detached sibling: one mid-turn (a notice, another
        # reader's continuation) keeps the slot; this backs off 30 s below and the next resume, boot sweep or
        # refused-turn timer retries (nothing re-polls).
        slot_error = (_ensure_active_session_slot(sid, session, takeover_idle_only=True) if paused
                      else _ensure_active_session_slot(sid, session))
        if slot_error is not None:
            try:
                handed = paused and _paused_turn_to_ledger_owner(sid, session, session_key, marker, slot_error)
            except Exception:  # health: allow BLE001 -- fall back to the backoff below; never strand the claim
                logger.warning("core-update pause: hand-off of %s to its owner failed", session_key, exc_info=True)
                handed = False
            if handed:
                with session["history_lock"]:
                    session["running"] = False
                    session["_auto_continue_scheduled"] = False
                return
            logger.info("auto-continue for %s refused: session has another live owner", session_key)
            with session["history_lock"]:
                session["running"] = False
                session["_auto_continue_retry_after"] = time.monotonic() + 30.0
                session["_auto_continue_scheduled"] = False
            return
        if paused and read_turn_marker(home, session_key) != marker:
            # Two readers can build from the same dead writer's marker. The slot
            # serializes them; the winner replaced/cleared the pause before releasing
            # it, so a delayed loser must not dispatch its stale snapshot.
            _release_active_session_slot(session)
            with session["history_lock"]:
                session["running"] = False
                session["_auto_continue_scheduled"] = False
            return
        with session["history_lock"]:
            # Marker inputs read back by _run_prompt_submit: attempt count (crash breaker) and the ORIGINAL prompt (no
            # nested notes). Set here, not at schedule time, so a bail above leaves nothing for a racing user turn.
            session["_auto_continue_attempt"], session["_auto_continue_prompt"] = attempt, marker["prompt"]
            session["_auto_continue_pauses"] = pauses
            session["_auto_continue_killed_jobs"] = marker.get("killed_jobs", [])
            # [daveey] A continued at-most-once turn keeps the contract: off again at its first tool.
            session["_marker_until_tool"] = bool(marker.get("until_tool"))
            # Read back by _auto_continue_refused: only this submit's own ownership fence may set it.
            session.pop("_turn_owner_refusal", None)
        try:
            from gateway.warning_notifications import render_notification
            diagnostic = marker.get("notification_category") == "diagnostic"
            with _session_profile_runtime_scope(session):
                def announce():
                    _emit("status.update", sid, {"kind": "process", "text": "Sending your queued message…" if held
                                                 else "Resuming paused turn…" if paused
                                                 else "Reporting stopped background jobs…" if receipt
                                                 else "Resuming interrupted turn…"})
                    _emit("message.start", sid)
                render_notification(announce, platform="tui", diagnostic=diagnostic)
                accepted = _run_prompt_submit(rid, sid, session, text,
                    display_kind=None if held else "auto_continue",
                    **({"display_metadata": {"notification_category": "diagnostic"}} if diagnostic else {}))
        except Exception as exc:
            _notif_log_failure("auto-continue dispatch failed", exc)
            _notif_release_claim(session, claim)  # a user turn admitted after a refusal keeps its ``running``
            _drop_kickoff_marker_inputs(session, marker)
            return
        if accepted is False:
            _notif_release_claim(session, claim)  # still ours only if admission did not release it
            _auto_continue_refused(sid, session, session_key, paused, "submit refused at admission", marker)
    if _start_session_work(kickoff, name=f"auto-continue-{sid}", home=session.get("profile_home")) is None:
        session["_auto_continue_scheduled"] = False
        return None
    logger.info("auto-continue scheduled for session %s (attempt %d%s, interrupted %.0fs ago, writer pid %s: %s, "
                "reader pid %s)", session_key, attempt, ", core-update pause" if paused else "", age,
                marker.get("writer_pid"), writer_state, os.getpid())
    return {"attempt": attempt, "interrupted_at": marker["started_at"]}


def _paused_turn_to_ledger_owner(sid: str, session: dict, session_key: str, marker: dict, refusal) -> bool:
    """[daveey] A paused continuation refused because the gateway's durable ledger owns the chat
    (t_01a1d4920ce3cbf4). This legacy backend can never run a turn while that ledger holds work, and the readers
    defer to this live writer, so waiting only ends in a stale replay: on David-MBP the pause was refused every
    2-3 min for 87 min while the gateway ran 9 turns. One owner, one FIFO (owner_forward): forwardable work gets
    the continuation queued behind it in the owner's FIFO, which runs it exactly once; work no running gateway will
    finish (``unknown``, no owner) retires the pause with the reason logged. Either way the marker goes.
    False for any other refusal (a sibling backend mid-turn): the caller's backoff applies, as before.
    A forward the owner refused before queuing anything for a passing reason (``_owner_forward_transient``: it
    did not admit it, draining, queue full, a dropped dial) also returns False and keeps the pause: the 30 s
    backoff only gates the next try, which the next resume, boot sweep or refused-turn retry makes (nothing
    polls), up to OWNER_FORWARD_TRIES forwards per pause; then, and on a definitive refusal (no owner running,
    permission, profile, not found), the pause is retired with the reason logged. An unconfirmed forward
    (including a receipt naming an admission in no confirmed state) is never retried: it may already be queued
    there, and a resend would run it twice."""
    status = getattr(refusal, "ledger_status", None)
    if status is None:
        return False
    from tui_gateway import owner_forward
    from tui_gateway.update_pause import later_turns_since, pause_note, retire_paused_turn
    if read_turn_marker(_session_home(session), session_key) != marker:
        return True  # another path continued or replaced this pause meanwhile: nothing left to hand over
    if status not in owner_forward.FORWARDABLE_LEDGER_STATES:
        retire_paused_turn(session, session_key, marker, f"the gateway's ledger holds {status} work for the chat, "
                           "which only Discard or a gateway resume resolves; nothing will run the continuation here")
        return True
    # It runs after the owner's queued turns, so it is never worded as the chat's previous turn.
    text = pause_note(marker["prompt"], marker.get("killed_jobs"), carried=bool(marker.get("carried_turn_paused")),
                      paused_at=marker.get("paused_at"), queued=True, pauses=int(marker.get("pauses") or 0),
                      later_turns=later_turns_since(session, session_key, marker.get("paused_at"),
                                                    marker.get("prompt")))
    try:
        receipt = owner_forward.forward_prompt(sid, session, text)
    except owner_forward.OwnerUnconfirmed:
        retire_paused_turn(session, session_key, marker, "went to the gateway that owns the chat, which did not "
                           "confirm it in time (it may be queued there)")
        return True
    except Exception as exc:  # health: allow BLE001 -- transient: bounded backoff; anything else: logged retirement
        why = str(exc) if isinstance(exc, owner_forward.OwnerUnavailable) else type(exc).__name__
        if _owner_forward_transient(exc):
            pause_id = (session_key, marker.get("started_at"), marker.get("paused_at"), marker.get("pauses"))
            with session["history_lock"]:
                last = session.get("_owner_forward_failures")
                count = last[1] + 1 if isinstance(last, tuple) and last[0] == pause_id else 1
                session["_owner_forward_failures"] = (pause_id, count)
            if count < OWNER_FORWARD_TRIES:
                logger.warning("core-update pause: the gateway that owns %s did not take the continuation (%s); "
                               "kept for a later try after the 30s backoff (attempt %d/%d)", session_key, why, count,
                               OWNER_FORWARD_TRIES)
                return False
            why = f"{why} ({count} attempts)"
        retire_paused_turn(session, session_key, marker, f"the gateway owns the chat ({status} work in its ledger) "
                           f"and did not take the continuation: {why}")
        return True
    retire_paused_turn(session, session_key, marker, "queued in the FIFO of the gateway that owns the chat "
                       f"(admission {receipt.get('admission_id')}); it runs there after the current turn", handed=True)
    return True


# [daveey] Forwards of one pause's continuation the owner turned away for a passing reason before it is retired.
OWNER_FORWARD_TRIES = 3
# Owner refusals (``data.reason``, raised before its durable write) that a later forward may clear.
_TRANSIENT_OWNER_REFUSALS = frozenset({"runtime_draining", "stale_epoch", "admission_conflict", "queue_full"})


def _owner_forward_transient(exc: BaseException) -> bool:
    """[daveey] Whether a failed forward is worth another try after the backoff: nothing was queued there (never
    ``OwnerUnconfirmed``), and the owner may take it later (not ``OwnerNotRunning``; not a refusal that names the
    request itself, such as ``permission_denied``, ``profile_mismatch``, ``not_found`` or ``runtime_retired``)."""
    from hermes_cli.gateway_client import GatewayRPCError
    from tui_gateway import owner_forward
    if isinstance(exc, (owner_forward.OwnerNotRunning, owner_forward.OwnerUnconfirmed)):
        return False
    if isinstance(exc, GatewayRPCError):
        reason = exc.data.get("reason", str(exc)) if isinstance(exc.data, dict) else str(exc)
        return reason in _TRANSIENT_OWNER_REFUSALS
    return isinstance(exc, (owner_forward.OwnerUnavailable, OSError))


_KICKOFF_MARKER_INPUTS = ("_auto_continue_attempt", "_auto_continue_prompt", "_auto_continue_pauses",
                          "_auto_continue_killed_jobs")
# Re-arms of the pause watcher for one pause whose continuation keeps being refused: each re-arm is a fresh
# watcher with a fresh budget, so a refusal that persists would otherwise loop every ~10-15 s.
REFUSED_REARM_MAX = 6


def _drop_kickoff_marker_inputs(session: dict, marker: dict) -> None:
    """[daveey] The kickoff's marker inputs are read back only by a started turn (``_record_turn_marker``). A
    submit that never started leaves them for whatever turn records next: a user turn would write its crash
    marker with the paused prompt, its attempt count and the at-most-once flag. ``_marker_until_tool`` is
    dropped only when the kickoff set it True (False is the default, and the key is shared with user turns)."""
    with session["history_lock"]:
        for key in _KICKOFF_MARKER_INPUTS:
            session.pop(key, None)
        if marker.get("until_tool") and session.get("_marker_until_tool") is True:
            session.pop("_marker_until_tool", None)


def _auto_continue_refused(sid: str, session: dict, session_key: str, paused: bool, cause: str,
                           marker: dict) -> None:
    """[daveey] The kickoff's submit was refused before its turn started (admission re-held during the agent
    build): "scheduled" is not "started", so the continuation keeps an owner. The claim and the kickoff's
    marker inputs are released. A core-update pause re-arms the in-place watcher (it waits out the hold and
    schedules again after a short backoff, no attempt used), at most REFUSED_REARM_MAX times per pause and
    never for an ownership refusal (another backend owns the chat: no watcher waits that out). Anything else
    only backs off 30 s: nothing re-polls, so the next resume, boot sweep or refused-turn timer retries, as
    after a slot refusal. The marker is untouched: it is still the turn's record. The WARNING is the
    detection signal: no continuation is dropped silently."""
    _drop_kickoff_marker_inputs(session, marker)
    owner_refusal = session.get("_turn_owner_refusal")  # set by _admit_prompt_turn's ownership fence
    pause_id = (session_key, marker.get("started_at"), marker.get("pauses"))
    with session["history_lock"]:
        session["_auto_continue_scheduled"] = False
        last = session.get("_auto_continue_refusals")
        count = last[1] + 1 if isinstance(last, tuple) and last[0] == pause_id else 1
        session["_auto_continue_refusals"] = (pause_id, count)
        rearm = (paused and not (session.get("_closing") or session.get("_finalized"))
                 and not owner_refusal and count <= REFUSED_REARM_MAX)
        # Paused: short, so a hold that lifted is not waited on, yet a refusal that persists cannot spin.
        session["_auto_continue_retry_after"] = time.monotonic() + (10.0 if rearm else 30.0)
    why = (f"owner refusal {owner_refusal}" if owner_refusal and paused
           else f"re-arm cap {REFUSED_REARM_MAX} reached" if paused and count > REFUSED_REARM_MAX else "")
    logger.warning("auto-continue: continuation of %s never started (%s); %s", session_key, cause,
                   f"rescheduling through its pause watcher (re-arm {count}/{REFUSED_REARM_MAX})" if rearm
                   else "not rescheduled here (" + (why + "; " if why else "")
                   + "30s backoff): the next resume, boot sweep or refused-turn timer retries")
    if rearm:
        from tui_gateway import update_pause
        update_pause.start_in_place_watcher(sid, session, session_key)


def _ac_inflight_original(session: dict) -> str:
    turn = session.get("inflight_turn")
    return str(turn.get("user") or "").strip() if isinstance(turn, dict) else ""


def _enqueue_prompt(session: dict, text: Any, transport: Any, image_paths: list[str] | None = None,
                    turn_author: dict | None = None) -> dict | None:
    """Queue a message for the next turn. Text-only arrivals share a slot and merge losslessly (like the
    consecutive-user merge in ``repair_message_sequence``); image-bearing and authored ones stay separate
    envelopes so attachment chronology and the sender survive. ``transport`` is pinned so the drained turn
    streams to its sender. Returns the envelope dict the text landed in (the merged head on a merge)
    so the caller can attach durable state to it; None when the text was dropped as a duplicate."""
    image_paths = list(image_paths or [])
    # Scrub live-turn self-duplicates first so the text merge below can't glue "{original}\n\n{later}" and re-fire the
    # original after a correction settles.
    # See #84417.
    _drop_queued_duplicates_of_inflight_user(session)
    text_only = not image_paths and isinstance(text, str)
    # A text-only self-copy of the live prompt would restart it on drain; an authored copy is another sender's message.
    if text_only and not turn_author and text.strip() == _ac_inflight_original(session) != "":
        return None
    queued = {"text": text, "transport": transport, **({"image_paths": image_paths} if image_paths else {}),
              **({"turn_author": turn_author} if turn_author else {})}
    existing = session.get("queued_prompt")
    if (existing and text_only and not turn_author and isinstance(existing.get("text"), str)
            and not existing.get("image_paths") and not existing.get("turn_author")
            and not session.get("queued_prompts")):
        prev = existing["text"]
        existing["text"] = f"{prev}\n\n{text}" if prev and text else (prev or text)
        return existing
    if existing:
        session.setdefault("queued_prompts", []).append(queued)
    else:
        session["queued_prompt"] = queued
    return queued


def _sanitize_queued_entry_vs_inflight_user(entry: Any, original: str) -> dict | None:
    """Drop (``None``) a text-only self-duplicate of the live user text, or rewrite a merged slot
    ``"{original}\\n\\n{later}"`` to ``later`` so the correction survives without re-firing the original. Image-bearing
    and authored envelopes are left alone: chronology and the sender's own words are kept.

    Returns ``None`` to drop the envelope, or a (possibly rewritten) dict to keep. A merged slot
    ``"{original}\\n\\n{later}"`` (from ``_enqueue_prompt``'s consecutive text merge) is rewritten to just
    ``later`` so a later correction is not lost and the original is not re-fired (#84417).
    """
    if not isinstance(entry, dict):
        return None
    text = entry.get("text")
    if not original or entry.get("image_paths") or entry.get("turn_author") or not isinstance(text, str):
        return entry
    # A lossless text-merge may have glued the live original onto a later follow-up: keep the remainder.
    rest = next((text[len(original + sep):] for sep in ("\n\n", "\n") if text.startswith(original + sep)), text).strip()
    return None if not rest or rest == original else (entry if rest == text.strip() else {**entry, "text": rest})


def _drop_queued_duplicates_of_inflight_user(session: dict) -> None:
    """Remove server-queue copies of the live turn's original user text: a mid-turn ``prompt.submit`` of the same text
    queued while redirect was unavailable must not drain and restart the original.

    A mid-turn ``prompt.submit`` of the same text can land in ``queued_prompt`` when redirect is not yet
    available (model not active, build window, tool boundary). If the user then corrects the turn with a
    different prompt via redirect, that stale self-duplicate must not ``_drain_queued_prompt`` after the
    redirected turn completes — otherwise the original prompt restarts as a fresh agent turn (#84417).
    """
    if not (original := _ac_inflight_original(session)):
        return
    head = session.get("queued_prompt")
    cleaned = (_sanitize_queued_entry_vs_inflight_user(e, original)
               for e in ([head] if head else []) + list(session.get("queued_prompts") or []))
    _ac_set_queue(session, [c for c in cleaned if c is not None])


def _ac_set_queue(session: dict, entries: list) -> None:
    """Write ``entries`` back as queued_prompt (head) + queued_prompts (rest)."""
    session["queued_prompt"] = entries[0] if entries else None
    if len(entries) > 1:
        session["queued_prompts"] = entries[1:]
    else:
        session.pop("queued_prompts", None)


def _interrupt_busy_session(sid: str, session: dict, agent: Any) -> None:
    """Interrupt a busy turn on a worker thread, never under ``history_lock`` (some providers can't apply ``interrupt()``
    until a blocking call returns; inline it stalled ``session.resume``). At most one interrupt worker per session so
    repeated steering can't leak threads."""
    use_agent = agent is not None and hasattr(agent, "interrupt")
    if not use_agent and not _session_uses_compute_host(session):
        return
    with session["history_lock"]:
        if session.get("_busy_interrupt_pending"):
            return
        session["_busy_interrupt_pending"] = True

    def interrupt() -> None:
        try:
            with contextlib.suppress(Exception):
                agent.interrupt() if use_agent else _get_compute_host_supervisor().interrupt(sid)
        finally:
            with session["history_lock"]:
                session["_busy_interrupt_pending"] = False
    threading.Thread(target=interrupt, daemon=True, name=f"busy-interrupt-{sid}").start()


def _ac_try_correction(rid, session: dict, agent: Any, method: str, plain_text: str, status: str) -> dict | None:
    """Apply ``agent.<method>(plain_text)`` (steer/redirect); on acceptance record the correction, scrub stale
    self-duplicates so the live turn's original text is not re-fired after settle, and return the ``status`` reply.
    None → caller falls through to the queue path."""
    try:
        if not getattr(agent, method)(plain_text):
            return None
    except Exception:
        return None
    with session["history_lock"]:
        _record_inflight_correction(session, plain_text)
        _drop_queued_duplicates_of_inflight_user(session)
        session["last_active"] = time.time()
    return _ok(rid, {"status": status})


def _session_compression_in_flight(session: dict) -> bool:
    """True when a compression lock is held for this session's durable id.

    Context compression is interrupt-protected (#23975), but a correction that reaches the
    provider mid-compression still aborts it with ``explicit_interrupt`` — the user's
    follow-up kills the very turn that would answer it (#61042). The channel-side busy
    path demotes interrupt→queue for the same reason (gateway/run_busy.py
    ``_session_has_compression_in_flight``, #56391); this is the local-RPC twin. Both
    blocking reads run off the event loop so a large state.db never freezes the dispatcher.
    """
    if session.get("_manual_compress_active"):  # held before/after the DB lock row exists (#133504)
        return True
    agent = session.get("agent")
    sid = str(getattr(agent, "session_id", "") or "") or str(session.get("session_key") or "")
    if not sid:
        return False
    try:
        with _session_db(session) as db:
            get_holder = getattr(db, "get_compression_lock_holder", None)
            if not callable(get_holder):
                return False
            holder = get_holder(sid)
    except Exception:
        logger.debug("compression in-flight check failed for session %s", sid, exc_info=True)
        return False
    # Production returns Optional[str]. Reject non-strings so a MagicMock auto-attr cannot
    # look like a held lock and needlessly demote every submit (see #96953).
    return isinstance(holder, str) and bool(holder)


def _persist_queued_user_row(session: dict, envelope: dict, display_kind: str | None) -> None:
    """Make a queued prompt durable the moment it is accepted. Writes the user row through the same
    #111868 machinery as an idle submit (so a cold ``session.resume`` sees it and a restart cannot lose
    it) and attaches the durable dict to the QUEUE ENVELOPE — never ``session["_submit_user_row"]``, the
    shared slot a possibly-still-staged in-flight turn owns. A text-only merge into an existing envelope
    rewrites the already-written row (and the staged dict) to the merged text in place, so cold readers
    see the merged text and the drained turn's ``_adopt_submit_user_row`` content match keeps working.
    A failed write stages nothing on the envelope; the drained turn then writes its own row as before.
    Caller holds ``history_lock`` (the durable row, the envelope text and the queue must move together —
    a drain cannot claim between the merge and the write)."""
    # The queue path bypasses prompt.submit's lazy row creation: the first message of a draft session can
    # be a queued one, and the message insert needs its sessions row.
    # Isolated compute-host turns are EXCLUDED exactly as prompt.submit excludes them (an isolated
    # dispatch returns before ``_persist_session_row_for_submit``): the worker process owns that turn's
    # persistence, and a local accept-time row would double it on drain.
    if _session_uses_compute_host(session):
        return
    _ensure_session_db_row(session)
    staged = envelope.get("_submit_user_row")
    if isinstance(staged, dict) and isinstance(staged.get("_row_id"), int):
        # Merge sync: the envelope text grew; the already-written row must not lag it.
        if staged.get("content") != envelope.get("text"):
            with _session_db(session) as db:
                if db is None:
                    return
                try:
                    # The staged dict records the session its row was written under; a rotated-away
                    # ``session_key`` misses that row's session_id and the merge update no-ops. An
                    # in-place compaction of the live turn may also have re-sequenced the row to a
                    # new id (the original was deactivated and cloned): follow it or the update
                    # no-ops against the dead original (#123675).
                    key = _submit_row_owner_key(staged, session)
                    live_id = db.resolve_active_row_id(key, staged["_row_id"])
                    updated = live_id is not None and bool(
                        db.set_user_message_content(key, live_id, envelope["text"]))
                except Exception:
                    logger.debug("queued-prompt row merge update failed", exc_info=True)
                    return
            if not updated:
                # No live row carries the prompt any more (compaction re-sequenced it away):
                # drop the staged row so the drained turn writes its own, rather than letting
                # the envelope claim a durable row that no longer exists.
                envelope.pop("_submit_user_row", None)
                return
            staged["_row_id"], staged["content"] = live_id, envelope["text"]
        return
    # ``display_metadata`` marker: ``reopen_session`` retires still-marked rows after a restart
    # discarded the in-memory queue (#125577); the drain's replacement row is unmarked.
    from hermes_state_common import QUEUED_PROMPT_METADATA_KEY
    staged = _write_submit_user_row(
        session, envelope.get("text"), display_kind,
        accept_metadata={QUEUED_PROMPT_METADATA_KEY: True})
    if staged is not None:
        envelope["_submit_user_row"] = staged
        if display_kind:
            envelope["_queued_display_kind"] = display_kind


def _adopt_queued_user_row_in_place(session: dict, early: dict) -> dict | None:
    """The accept-time row, unmarked, as the dispatching turn's durable dict — or None (the caller re-places
    it at the end) when it is no longer the session's last active row or the store is unavailable."""
    from hermes_state_common import QUEUED_PROMPT_METADATA_KEY
    from hermes_state_message_content import adopt_queued_user_row_in_place
    with _session_db(session) as db:
        if db is None:
            return None
        try:
            key = _submit_row_owner_key(early, session)
            # The row was written under the session live at ACCEPT; the turn flushes under the
            # session live NOW. A mid-turn compression in between rotated the agent to a
            # continuation: adopting the row in the closed parent strands the drained turn's user
            # row there (#123545 split). Re-append into the flush session instead.
            if key != _submit_row_target_key(session):
                return None
            live_id = db.resolve_active_row_id(key, early["_row_id"])
            if not adopt_queued_user_row_in_place(db, key, live_id):
                return None
        except Exception:
            logger.debug("queued-prompt in-place row adoption failed; re-placing it", exc_info=True)
            return None
    adopted = {**early, "_row_id": live_id}
    meta = {k: v for k, v in (early.get("display_metadata") or {}).items() if k != QUEUED_PROMPT_METADATA_KEY}
    if meta:
        adopted["display_metadata"] = meta
    else:
        adopted.pop("display_metadata", None)
    return adopted


def _replace_queued_user_row_for_turn(session: dict, queued: dict, is_dispatching: bool = False) -> dict | None:
    """Re-place a queued prompt's accept-time row at the transcript END before dispatching its turn.

    The accept-time write lands BEFORE the in-flight turn's assistant rows (raw ``[uA, uB, aA]``), and
    ``repair_alternation`` would glue the two user turns into one. So on drain: write an identical row
    with a fresh timestamp at the end via the normal ``_persist_submit_user_row`` — it slots
    ``session["_submit_user_row"]``, so the turn's ``_adopt_submit_user_row`` adopts it and the flush
    writes no second row (the exact existing contract) — then deactivate the early row (durable
    history, never deleted). Steady-state raw order: ``[uA, aA, uB, aB]``. Caller holds
    ``history_lock`` so a concurrent submit cannot interleave its own row between the two writes.
    """
    early = queued.get("_submit_user_row")
    if not (isinstance(early, dict) and isinstance(early.get("_row_id"), int)):
        return None  # no accept-time row (write failed / pre-feature envelope): the turn persists as before
    # Nothing landed after the accept row (an idle submit queued behind the core-update hold, or a busy one
    # whose turn wrote no rows since): the turn's rows follow it, so the DISPATCHING envelope adopts that
    # row in place. Re-appending here left two user rows for one submit (t_01a1ceb141b10c7c).
    if is_dispatching and (adopted := _adopt_queued_user_row_in_place(session, early)) is not None:
        queued["_submit_user_row"] = adopted
        return adopted
    # Append the replacement FIRST: if that write fails nothing is deactivated, the accept-time row
    # stays active (the message stays visible) and the turn's crash persist persists it as before.
    # The DISPATCHING envelope's replacement carries no marker (its turn adopts the row immediately);
    # still-queued envelopes keep the never-drained marker (#125577) so a restart between drains
    # retires the row rather than gluing the never-run prompt into the previous turn.
    if is_dispatching:
        _persist_submit_user_row(session, queued.get("text"), queued.get("_queued_display_kind"))
    else:
        from hermes_state_common import QUEUED_PROMPT_METADATA_KEY
        _persist_submit_user_row(session, queued.get("text"), queued.get("_queued_display_kind"),
                                 accept_metadata={QUEUED_PROMPT_METADATA_KEY: True})
    fresh = session.get("_submit_user_row")
    if not (isinstance(fresh, dict) and isinstance(fresh.get("_row_id"), int)):
        return None  # re-append wrote nothing: keep the accept-time row active
    queued["_submit_user_row"] = fresh  # the envelope follows the live row (a retry drains cleanly)
    with _session_db(session) as db:
        if db is None:
            return
        try:
            # The accept-time dict records the session its row was written under; a rotated-away
            # ``session_key`` would miss it and leave that row ACTIVE beside its replacement in the
            # continuation — the [uA, uB, aA] shape this function exists to prevent. An in-place
            # compaction of the live turn may also have re-sequenced the row to a new id: deactivate
            # the row that is live NOW, or the clone stays active beside its replacement and the
            # queued prompt is active twice after the drain (#123675).
            key = _submit_row_owner_key(early, session)
            live_id = db.resolve_active_row_id(key, early["_row_id"])
            if live_id is not None:
                db.deactivate_message(key, live_id)
        except Exception:
            # Both rows briefly active merges in projection but never loses the message; deleting or
            # losing text would be worse.
            logger.debug("queued-prompt row re-placement deactivate failed", exc_info=True)
    return fresh


def _handle_busy_submit(rid, sid: str, session: dict, text: Any, transport: Any, queued: bool = False,
                        turn_author: dict | None = None, display_kind: str | None = None) -> dict | None:
    """Apply ``display.busy_input_mode`` to a mid-turn prompt instead of rejecting it (rejection made clients busy-retry
    and drop sends): ``interrupt`` (default) → redirect, falling back to hard interrupt + queue; ``queue`` → queue only;
    ``steer`` → inject after the current atomic action. ``queued=True`` (client queue drain) forces queue mode: a "run
    after" message must NEVER become a live correction."""
    mode = "queue" if queued else _load_busy_input_mode()
    agent = session.get("agent")
    # Compression in flight demotes steer/interrupt to queue: a correction delivered
    # mid-compression aborts the compression instead of waiting for it (#61042). The
    # follow-up drains when compression finishes — the Discord-gateway contract.
    if mode in ("steer", "interrupt") and _session_compression_in_flight(session):
        mode = "queue"
    with session["history_lock"]:
        if not session.get("running"):
            return None  # turn ended since prompt.submit's busy check; caller retries on the idle session
        # Typed while the turn ran, in any busy mode: the running turn no longer counts as unattended.
        session["_turn_user_input"] = True
        image_paths = list(session.get("attached_images", []))
        if image_paths:
            session["attached_images"] = []  # claim now so a later paste isn't consumed when the turn yields
    plain_text = _coerce_message_text(text).strip() if not image_paths and _is_text_only_busy_payload(text) else ""
    # Text-only corrections steer/redirect in place when supported; media payloads and older agents fall through to
    # the proven interrupt + queue path.
    if plain_text and agent is not None:
        supported = {
            "steer": hasattr(agent, "steer"),
            "interrupt": getattr(agent, "_supports_active_turn_redirect", False) is True and hasattr(agent, "redirect")}
        method, status = {"steer": ("steer", "steered"), "interrupt": ("redirect", "redirected")}.get(mode, (None, None))
        if (method and supported[mode]
                and (resp := _ac_try_correction(rid, session, agent, method, plain_text, status)) is not None):
            return resp
    # Queue before asking the live turn to stop. Never call a provider/compute-host method under history_lock: an
    # interrupt can wait behind the op it cancels.
    with session["history_lock"]:
        if not session.get("running"):
            if image_paths:
                session["attached_images"] = image_paths + list(session.get("attached_images", []))
            return None
        envelope = _enqueue_prompt(session, text, transport, image_paths=image_paths, turn_author=turn_author)
        # Durable AT ACCEPT (not when the turn runs): a cold resume sees the queued message and a
        # backend restart cannot lose it. Lives on the envelope, never the shared session slot.
        if envelope is not None:
            _persist_queued_user_row(session, envelope, display_kind)
        session["last_active"] = time.time()
    # Attachments need their own model invocation: queue without cancelling so the user gets both results in order.
    # ``steer`` must NEVER escalate to a hard interrupt: it would kill the live turn AND drop ``AIAgent._pending_steer``
    # (earlier accepted steers); steer fall-throughs stay FIFO-queued.
    # A burst of user messages while the agent is busy can land as a mix of accepted steers (stashed in
    # ``AIAgent._pending_steer``) and fall-through queue envelopes (payload not steerable, ``steer()``
    # rejected/raised). A hard interrupt here kills the live turn AND ``AIAgent.interrupt()`` drops the
    # pending steer buffer — silently destroying the earlier messages of the burst. See #86134.
    if mode == "interrupt" and not image_paths:
        _interrupt_busy_session(sid, session, agent)
    return _ok(rid, {"status": "queued"})


def _queue_submit_behind_update_hold(rid, sid: str, session: dict, text: Any, transport: Any,
                                     turn_author: dict | None = None, display_kind: str | None = None) -> dict | None:
    """An idle ``prompt.submit`` refused ONLY by the core-update admission hold is queued, not bounced.

    The update drainer holds admission for a bounded drain window while the turns in flight finish
    (hermes-sync ``drain_window``, t_01a181e61e88e5d4). Desktop has no 5035 handling, so a refusal there
    showed the user an error for a message they had just sent. Instead the message joins the durable
    queue exactly like a mid-turn send (persisted at accept, so a backend restart for the update cannot
    lose it), and ``retry_queued_prompt`` re-enters admission until the hold lifts. Returns the
    ``{"status": "queued"}`` reply, or None when the refusal was not the hold (a retiring backend keeps its
    5035: the client is replacing this process) or the session is no longer idle (caller retries)."""
    from hermes_cli.backend_retirement import last_refusal, retirement, update_hold_started_at
    # Only a refusal by the update hold itself is labelled: a briefly busy or unreadable fence queues as
    # before (the retry admits it within a tick) but is not "waiting for an update". Read before any other
    # reservation in this context can overwrite the reason.
    held_by_update = last_refusal() == "held"
    if not retirement.acquire():
        return None
    retirement.release()
    hold_started = update_hold_started_at(session.get("profile_home") or None) if held_by_update else None
    from tui_gateway.admission_retry import retry_queued_prompt
    with session["history_lock"]:
        if session.get("running") or session.get("_closing") or session.get("_finalized"):
            return None
        image_paths = list(session.get("attached_images", []))
        if image_paths:
            session["attached_images"] = []
        envelope = _enqueue_prompt(session, text, transport, image_paths=image_paths, turn_author=turn_author)
        if envelope is not None and held_by_update:
            # On the envelope (a merge lands in the head, which then carries it): the live-session snapshot
            # and an idle Stop (``_interrupt_session_turn``) read it from there.
            envelope[QUEUED_REASON_KEY] = UPDATE_HOLD_QUEUED_REASON
            if hold_started is not None or not envelope.get(QUEUED_STARTED_AT_KEY):
                envelope[QUEUED_STARTED_AT_KEY] = hold_started
        if envelope is not None:
            _persist_queued_user_row(session, envelope, display_kind)
        session["last_active"] = time.time()
        # Durable replay: the in-memory queue dies with this process when the update restarts the backend
        # (and the accept row is retired on the next resume), so journal the merged text as a turn marker
        # that the post-restart ``session.resume`` replays through auto-continue.
        if envelope is not None and (queued_now := session.get("queued_prompt")):
            from tui_gateway.turn_marker import record_turn_start
            if not record_turn_start(_session_home(session), str(session.get("session_key") or ""),
                                     str(queued_now.get("text") or text), reason=UPDATE_HOLD_QUEUE_REASON):
                # [daveey] Not journaled: a sibling backend's live turn holds this session's marker (it must keep
                # the drain from updating over that turn), or the write failed. The message stays queued in memory;
                # a restart for the update before the hold lifts leaves it "not sent" with Retry.
                logger.warning("update-hold queue for session %s: not journaled for replay (marker held or "
                               "unwritable); a restart before the hold lifts drops it to Retry", sid)
        if not retry_queued_prompt(rid, sid, session) and session.get("queued_prompt"):
            # Retirement raced in between: the queue is the client's to replay; keep the 5035.
            logger.info("update-hold queue for session %s: retry unavailable, kept queued", sid)
        reply: dict = {"status": "queued"}
        if held_by_update:
            head = envelope if envelope is not None else session.get("queued_prompt")
            started = head.get(QUEUED_STARTED_AT_KEY) if isinstance(head, dict) else hold_started
            reply.update(queued_reason=UPDATE_HOLD_QUEUED_REASON, queued_started_at=started)
    logger.info("prompt.submit for session %s queued behind the core-update admission hold (refusal=%s)",
                sid, "held" if held_by_update else "other")
    return _ok(rid, reply)


def _update_held_queue(session: dict) -> bool:
    """Caller holds history_lock. True when any queued envelope waits on the core-update admission hold."""
    head = session.get("queued_prompt")
    return any(isinstance(e, dict) and e.get(QUEUED_REASON_KEY) == UPDATE_HOLD_QUEUED_REASON
               for e in ([head] if head else []) + list(session.get("queued_prompts") or []))


def _drain_queued_prompt(rid, sid: str, session: dict) -> bool:
    """Fire a queued next-turn prompt if one is waiting and the session is idle. True when dispatched: the caller
    skips lower-priority follow-ups this cycle (the user's message wins)."""
    with _session_turn_admission(session) as admitted:
        if not admitted:
            from tui_gateway.admission_retry import retry_queued_prompt
            return retry_queued_prompt(rid, sid, session)
        if session.get("_closing") or not (queued := session.get("queued_prompt")) or session.get("running"):
            return False
        queue_generation = int(session.get("_queued_prompt_generation", 0))
        _ac_set_queue(session, session.get("queued_prompts") or [])
        session["running"] = True
        session["_notif_turn_claim"] = object()  # [daveey] _dispatch_claimed_queue reads it back: its release frees only this claim
        queued_transport = queued.get("transport")
        # The queuer's transport is pinned so the drained turn reaches the client that sent it — but
        # ATTACHED, not rebound: a mid-turn prompt from a second client used to silence the first for the
        # whole drained turn. A peer that disconnected while its prompt sat in the queue is skipped: the
        # prompt still runs, only the dead pin is dropped.
        if queued_transport is not None and not _transport_is_dead(queued_transport):
            _attach_session_transport(session, queued_transport)
    from tui_gateway.turn_admission import stage
    with stage(session):
        return _dispatch_claimed_queue(rid, sid, session, queued, queue_generation)


def _dispatch_claimed_queue(rid, sid, session, queued, queue_generation):
    claim = session.get("_notif_turn_claim")  # set by _drain_queued_prompt's claim just before
    use_compute_host = _session_uses_compute_host(session)
    with session["history_lock"]:
        if int(session.get("_queued_prompt_generation", 0)) != queue_generation:
            # Generation bump cancelled the claim (Stop, compress re-anchor, …): don't dispatch, but restore the
            # envelope (claimed head first, then whatever advanced into the slot) so a legitimate follow-up isn't dropped.
            # See #84417.
            advanced = session.get("queued_prompt")
            _ac_set_queue(session, [queued, *([advanced] if advanced else []), *(session.get("queued_prompts") or [])])
            session["running"] = False
            _clear_inflight_turn(session)
            return True
    kwargs: dict = {"queued_prompt_generation": queue_generation}
    if queued.get("image_paths"):
        kwargs["image_paths"] = queued["image_paths"]
    # Re-place the accept-time rows (if any) at the transcript END before the turn's rows follow
    # them, and slot the dispatching envelope's fresh row for adoption. EVERY queued envelope is
    # re-placed in acceptance order: a later-accepted prompt's row must never sit behind while an
    # earlier one heals past it, or a crash between drains would permanently render the later
    # prompt before the earlier one. Under history_lock so a concurrent submit can't interleave
    # its own row write between the re-append and the deactivation.
    with session["history_lock"]:
        dispatch_row = _replace_queued_user_row_for_turn(session, queued, is_dispatching=True)
        still_queued = (([session["queued_prompt"]] if session.get("queued_prompt") else [])
                        + list(session.get("queued_prompts") or []))
        for envelope in still_queued:
            _replace_queued_user_row_for_turn(session, envelope)
        # The single adoption slot must hold the DISPATCHING envelope's row (the loop above leaves
        # the last processed one there) or the turn would adopt the wrong prompt's row.
        if dispatch_row is not None:
            session["_submit_user_row"] = dispatch_row
        else:
            session.pop("_submit_user_row", None)
    # The compute-host frame has no author field, so only the inline runner receives it.
    author_kwargs = {"turn_author": queued["turn_author"]} if queued.get("turn_author") else {}
    dispatch_failed = False
    try:
        if not use_compute_host:
            _run_prompt_submit(rid, sid, session, queued["text"], **kwargs, **author_kwargs)
        elif (resp := _submit_prompt_to_compute_host(rid, sid, session, queued["text"], **kwargs)).get("error"):
            with session["history_lock"]:
                session["running"] = False
                _clear_inflight_turn(session)
            _emit("error", sid, {"message": str((resp.get("error") or {}).get("message") or "queued prompt failed")})
            dispatch_failed = True
    except Exception as exc:
        logger.exception("Queued prompt dispatch failed")
        _notif_log_failure("queued prompt dispatch failed", exc)
        _notif_release_claim(session, claim)
        dispatch_failed = True
    if dispatch_failed:
        with session["history_lock"]:
            drain_next = bool(session.get("queued_prompt")) and not session.get("_turn_cancel_requested")
        if drain_next:
            _drain_queued_prompt(rid, sid, session)
    return True


def _inflight_snapshot(session: dict) -> dict | None:
    turn = session.get("inflight_turn")
    if not isinstance(turn, dict):
        return None
    user, assistant = str(turn.get("user") or "").strip(), str(turn.get("assistant") or "")
    streaming, error = bool(turn.get("streaming")), str(turn.get("error") or "").strip()
    if not (user or assistant or streaming or error):
        return None
    snapshot = {"assistant": assistant, "streaming": streaming, "user": user}
    if isinstance(display_kind := turn.get("display_kind"), str) and display_kind:
        snapshot["display_kind"] = display_kind
    if isinstance(display_metadata := turn.get("display_metadata"), dict):
        snapshot["display_metadata"] = dict(display_metadata)
    raw_offsets = turn.get("correction_offsets") or []
    correction_pairs = [(str(c), raw_offsets[i] if i < len(raw_offsets) else None)
                        for i, c in enumerate(turn.get("corrections") or []) if str(c).strip()]
    if correction_pairs:
        # Mid-turn redirects alongside (not over) the original prompt so resume can rebuild every user bubble; offsets
        # only when every correction has one so clients can trust the pairing.
        snapshot["corrections"] = [c for c, _ in correction_pairs]
        if all(isinstance(offset, int) and offset >= 0 for _, offset in correction_pairs):
            snapshot["correction_offsets"] = [int(offset) for _, offset in correction_pairs]  # type: ignore[arg-type]
    if error:
        # Retained failed turn (_fail_inflight_turn): a resuming client must rebuild the failed bubble, not render the
        # partial text as a healthy reply.
        snapshot.update(error=error, status=str(turn.get("status") or "error"), recoverable=bool(turn.get("recoverable")))
        if isinstance(surface := turn.get("error_surface"), dict) and surface:
            snapshot["error_surface"] = surface
    return snapshot


def _emit_terminal_turn_error(
    sid: str, session: dict, error: Any, error_surface: Optional[dict] = None, *, retire_marker: bool = True) -> None:
    """Close a failed turn with the same ``status: "error"`` ``message.complete`` frame as the returned-error path,
    retaining the turn so a client that missed the frame recovers it from ``session.resume``'s ``inflight``.
    ``error_surface`` ({layer, code, retryable}) is classified from an exception if absent."""
    agent = session.get("agent")
    if error_surface is None and isinstance(error, BaseException):
        with contextlib.suppress(Exception):
            from agent.error_surface import build_error_surface_from_exception
            error_surface = build_error_surface_from_exception(
                error, provider=str(getattr(agent, "provider", "") or ""), model=str(getattr(agent, "model", "") or ""),
                api_key=getattr(agent, "api_key", None))
    with session["history_lock"]:
        _fail_inflight_turn(session, error, error_surface=error_surface)
        turn = session.get("inflight_turn") or {}
        message, partial = str(turn.get("error") or "turn failed"), str(turn.get("assistant") or "")
        cols = int(session.get("cols", 80))
    text = partial or turn_error_text(message, error_surface)
    rendered = ""
    with contextlib.suppress(Exception):
        rendered = render_message(text, cols)
    payload = {"text": text, "usage": _get_usage(agent) if agent is not None else {}, "status": "error",
               "error": message, "recoverable": True, **({"error_surface": error_surface} if error_surface else {}),
               **({"partial": True} if partial else {}), **({"rendered": rendered} if rendered else {})}
    if isinstance(error, BaseException):
        from tui_gateway.credential_failure import exception_failure_fields
        with _session_profile_runtime_scope(session):
            payload.update(exception_failure_fields(
                error, str(getattr(agent, "provider", "") or ""),
                pool=getattr(agent, "_credential_pool", None)))
    if retire_marker:
        _retire_turn_marker(session)
    _emit("message.complete", sid, payload)


def _restore_agent_history_after_turn_error(session: dict, agent) -> bool:
    """Keep a failed turn's working transcript: ``AIAgent`` persists its messages independently, so after a raise the
    next prompt must see them, not the pre-turn snapshot."""
    agent_messages = getattr(agent, "_session_messages", None)
    if not isinstance(agent_messages, list):
        return False
    with session["history_lock"]:
        session["history"] = list(agent_messages)
        session["history_version"] = int(session.get("history_version", 0)) + 1
    return True


def _queued_prompt_snapshot(session: dict) -> dict | None:
    """The accepted next-turn prompt without its transport handle, for the live-session projection (Desktop may
    reconnect while it is still queued)."""
    queued = session.get("queued_prompt")
    user = _inflight_text(queued.get("text")) if isinstance(queued, dict) else ""
    if not user or not isinstance(queued, dict):
        return None
    snapshot: dict = {"user": user}
    if queued.get(QUEUED_REASON_KEY) == UPDATE_HOLD_QUEUED_REASON:
        # A reconnecting client re-renders "waiting for an update" (and since when) instead of a bare queue.
        snapshot.update(queued_reason=UPDATE_HOLD_QUEUED_REASON, queued_started_at=queued.get(QUEUED_STARTED_AT_KEY))
    return snapshot


def register(server) -> None:
    """Publish this module's helpers + handlers onto ``server``, rebound to its globals."""
    bind_module(globals(), server, skip=("_",))
