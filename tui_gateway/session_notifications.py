"""Per-session notification poller: kanban/loop/delegation events routed to the owning session,
desktop UI wiring (HUD surface note: session_notifications_surface). Bodies are rebound onto server.py's
globals at install time (method_ctx.bind_module), so they reference server.py globals bare."""

from __future__ import annotations

import contextlib
import threading

from .method_ctx import bind_module
from . import session_notifications_surface as _surface
from . import session_notifications_readopt as _readopt
from .kanban_notice_text import (chat_wait_suffix as _kb_chat_wait, failure_suffix as _kb_failure,
    left_open_suffix as _kb_left_open, repin_suffix as _kb_repin_notice, timed_out_suffix as _kb_timed_out,
    parent_unclaimed_suffix as _kb_parent_unclaimed, quota_episode_suffix as _kb_quota_episode)
from .completion_obligation import settle_started as _settle_started, settle_suppressed as _settle_suppressed
from .kanban_leg_custody import LINEAGE_LIMIT


def _notif_locked_sessions(fn, default):
    """Run ``fn(_sessions)`` under ``_sessions_lock``; ``default`` on failure (poller must never crash)."""
    try:
        with _sessions_lock:
            return fn(_sessions)
    except Exception:
        return default


def _notif_current_keys(sid: str, session: dict) -> set:
    return {str(session.get("session_key") or ""), _session_lookup_key(session, fallback=sid)}


def _notif_session_matches(s: dict, keys) -> bool:
    return str(s.get("session_key") or "") in keys or _session_lookup_key(s, fallback="") in keys


def _notif_live_session_matches(keys, exclude: dict | None = None) -> bool:
    """Any non-finalized live session (other than ``exclude``) matches ``keys``; False if the registry can't be read
    (fail open rather than drop the event)."""
    return _notif_locked_sessions(
        lambda ss: any(s is not exclude and not s.get("_finalized") and _notif_session_matches(s, keys)
                       for s in ss.values()),
        False)


def _notif_resolve_event_key(evt_key: str, session: dict | None = None) -> str:
    """Resolve a compression-rotated session key to its continuation tip (or itself). Looked up in
    ``session``'s own store: a named-profile session's lineage lives in ``profiles/<x>/state.db``,
    where the launch handle cannot see it."""
    try:
        with _session_db(session or {}) as db:
            return (db.resolve_resume_session_id(evt_key) if db is not None else evt_key) or evt_key
    except Exception:
        return evt_key


def _notif_live_session_matching(keys, exclude: dict | None = None) -> dict | None:
    """The first non-finalized live session (other than ``exclude``) matching ``keys``; None if none or the registry
    can't be read."""
    return _notif_locked_sessions(
        lambda ss: next((s for s in ss.values() if s is not exclude and not s.get("_finalized")
                         and _notif_session_matches(s, keys)), None),
        None)


def _notification_event_owner_elsewhere(sid: str, session: dict, evt: dict) -> dict | None:
    """The *different* live session that owns ``evt``, or None. Background completions carry the ``session_key`` of the
    session that started the work; async delegation completions also carry ``origin_ui_session_id`` (the live TUI tab)."""
    evt_ui_sid = str(evt.get("origin_ui_session_id") or "")
    if evt_ui_sid:
        if evt_ui_sid == str(sid or "") and not session.get("_finalized"):
            return None
        owner = _notif_locked_sessions(
            lambda ss: ss[evt_ui_sid] if evt_ui_sid in ss and not ss[evt_ui_sid].get("_finalized") else None, None)
        if owner is not None:
            return owner
        # Exact UI tab gone: fall through to durable session_key routing so a resumed continuation with the same
        # key/lineage can still claim it.
    evt_key = str(evt.get("session_key") or "")
    if not evt_key:
        return None
    current_keys = _notif_current_keys(sid, session)
    # Compression can rotate AIAgent.session_id while the detached child is still running: map the event's original
    # key to its continuation tip so it reaches the live session instead of becoming an orphan any poller may consume.
    # A live continuation wins over the compressed parent, else a stale parent tab could consume the event first.
    resolved_key = _notif_resolve_event_key(evt_key, session)
    if resolved_key != evt_key:
        if resolved_key in current_keys:
            return None
        owner = _notif_live_session_matching({resolved_key})
        if owner is not None:
            return owner
    if evt_key in current_keys:
        return None
    if resolved_key == evt_key:
        owner = _notif_other_profile_session_owner(sid, session, evt)
        if owner is not None:
            return owner
    return _notif_live_session_matching({evt_key, resolved_key}, exclude=session)


def _notification_event_belongs_elsewhere(sid: str, session: dict, evt: dict) -> bool:
    """True if ``evt`` is owned by a *different* live session (``_notification_event_owner_elsewhere``)."""
    return _notification_event_owner_elsewhere(sid, session, evt) is not None


def _notif_other_profile_session_owner(sid: str, session: dict, evt: dict) -> dict | None:
    """The live session on ANOTHER profile store that provably owns ``evt`` (its compression lineage resolves
    there), or None. Every poller drains one process-wide queue, but lineage is looked up in the dequeuer's own
    store; without this, profile B dequeuing an event keyed on profile A's compressed parent found no owner
    anywhere and dropped it for good. Snapshot under the lock, resolve outside it."""
    own_home = str(session.get("profile_home") or "")
    candidates = _notif_locked_sessions(
        lambda ss: [(other_sid, other) for other_sid, other in ss.items()
                    if other is not session and not other.get("_finalized")
                    and str(other.get("profile_home") or "") != own_home],
        [])
    return next((other for other_sid, other in candidates
                 if _session_owns_notification_event(other_sid, other, evt)), None)


def _session_owns_notification_event(sid: str, session: dict, evt: dict) -> bool:
    """True iff *this* session PROVABLY owns ``evt`` (UI origin is this live session, or ``session_key`` raw/compression-
    resolved matches) — the fail-closed gate for addressed notifications, without the orphan-adoption fallback."""
    if session.get("_finalized"):
        return False
    # A completion replayed at process start has no in-process owner. A matching
    # persisted key alone can belong to a detached runtime whose old lease survived;
    # requiring a current client prevents it from spending a full model turn unseen.
    # Shared peer membership excludes stdio, even when it is the real TUI RPC channel
    # (possibly wrapped for a dashboard sidecar), rather than a headless fallback sink.
    if (evt.get("type") == "async_delegation" and evt.get("startup_replay")
            and not (_session_has_live_transport(session)
                     or (_stdio_is_rpc_channel and session.get("transport") is _stdio_transport))):
        return False
    if str(evt.get("origin_ui_session_id") or "") == str(sid or ""):
        return True
    evt_key = str(evt.get("session_key") or "")
    current_keys = _notif_current_keys(sid, session)
    return bool(evt_key) and (evt_key in current_keys or _notif_resolve_event_key(evt_key, session) in current_keys)


def _notification_event_requires_owner(evt: dict) -> bool:
    """Whether ``evt`` must be positively claimed before TUI delivery."""
    return evt.get("type") == "async_delegation" or bool(evt.get("origin_ui_session_id") or evt.get("session_key"))


# Extra dedup fields per event type. Completions are terminal (one-shot per process session); watch events are not —
# one process can match patterns many times, so their content is part of the key.
_DEDUP_EXTRA_FIELDS = {
    "watch_match": ("command", "pattern", "output", "suppressed", "message_id"),
    "heartbeat": ("seq",),
    "watch_disabled": ("command", "message", "suppressed"),
    "watch_overflow_": ("command", "message", "suppressed"),  # prefix match
}


def _notification_event_dedup_key(evt: dict) -> tuple:
    """UI-emission identity for a process notification event."""
    evt_type = evt.get("type", "completion")
    if evt_type in {"watch_match", "watch_disabled"} and evt.get("event_id"):
        return (evt.get("session_id", ""), evt_type, evt["event_id"])
    if evt_type == "async_delegation":
        # No process session_id: else every completion keys as ("", "async_delegation") and the second is suppressed forever.
        # An early per-task failure notice must not collapse with the batch's final result (nor with a sibling's notice).
        if evt.get("task_failure_notice"):
            task_idx = ((evt.get("results") or [{}])[0] or {}).get("task_index", "")
            return (evt.get("delegation_id", ""), evt_type, "task_failure", task_idx)
        return (evt.get("delegation_id", ""), evt_type)
    extra = _DEDUP_EXTRA_FIELDS.get("watch_overflow_" if evt_type.startswith("watch_overflow_") else evt_type, ())
    return (evt.get("session_id", ""), evt_type, *(evt.get(f, 0 if f == "suppressed" else "") for f in extra))


# Mirror gateway/kanban_watchers.py TERMINAL_KINDS: claim silent kinds (archived/unblocked) too so the cursor advances
# past them and they can't wedge a later completed/blocked event behind an unclaimed row.
_KANBAN_NOTIFY_KINDS = ("completed", "blocked", "gave_up", "crashed", "timed_out", "status", "archived", "unblocked",
                        "quota_episode_notice", "worker_repin_notice", "dependency_parent_unclaimed",
                        "chat_wait_unclaimed", "spend_budget_decision_ready", "request_withdrawn", "failure_finalized", "resolved_check_broken",
                        "resolved_check_lost", "park_progress_notice", "decision_requested", "parent_held", "wake_probe_unproven_notice")
# kanban, /loop + /heartbeat and the bot mailbox share one idle-poll cadence; probing the lease registry on
# every 0.5s queue timeout cost ~a core at 11 sessions (#108005).
_KANBAN_POLL_SECONDS = _LOOP_POLL_SECONDS = 5.0


# [daveey] Completion events (pid → event) a claimed notice turn will carry; its start (start_synthesized_turn)
# moves them to ``_completion_delivered`` + ``COMPLETION_UNSECURED``; until then, poller in-flight custody.
NOTIF_OFFERED_COMPLETIONS = "_notif_offered_completions"
# Delivered to a started turn whose crash marker is not written yet: the exit journal still reports these.
# ``_record_turn_marker`` drops them once the marker (whose prompt carries the notice) is on disk.
COMPLETION_UNSECURED = "_completion_unsecured"


def admit_synthesized_turn(session: dict) -> bool:
    """Caller holds ``history_lock``. Early refusal only: a synthesized (automatic) turn is refused once a backend
    exit fenced notices. Nothing is marked delivered here; the commit point is the turn's start."""
    return not (session.get("_synthesized_turn") and session.get("_exit_notices_pending"))


@contextlib.contextmanager
def start_synthesized_turn(session: dict):
    """[daveey] The single commit point for the completions a notice turn carries. Enter it BEFORE the server's
    ``_sessions_lock`` (it takes ``history_lock``; lock order is ``history_lock`` -> ``_sessions_lock``, since
    session.resume holds history_lock while it takes _sessions_lock). It yields ``commit(start)``; call that under
    ``_sessions_lock``, under which alone ``fence_exit_notices`` raises the exit fence, so the fence check and the
    worker start are one step for the exit journal (which fences first): fenced → refused (still in custody, so
    journaled); started → delivered (owed receipts settled), unsecured until the turn's crash marker is on disk.
    ``commit`` returns ``start()``'s result (None = not started). The delivered mark is taken on entry, before the
    start: a marked completion is also unsecured and the journal reports those, so a fence landing between mark and
    commit still journals it. A commit that did not start (fenced, refused, raised or never called) rolls the mark
    back on exit, after the caller released ``_sessions_lock``."""
    with session["history_lock"]:
        synthesized = bool(session.get("_synthesized_turn"))
        fenced = synthesized and bool(session.get("_exit_notices_pending"))
        offered = dict(session.pop(NOTIF_OFFERED_COMPLETIONS, None) or {}) if synthesized and not fenced else {}
        if offered:  # before the start: the started turn may secure them at once (_record_turn_marker)
            session.setdefault("_completion_delivered", set()).update(offered)
            session.setdefault(COMPLETION_UNSECURED, {}).update(offered)
    started = None

    def commit(start):
        nonlocal started
        # The fence is one-way and raised only under _sessions_lock (held here), so this read is current.
        if synthesized and session.get("_exit_notices_pending"):
            return None
        started = start()
        return started

    try:
        yield commit
    finally:
        if offered and started is not None:  # delivered into the session: settle its owed receipt
            _settle_started(offered)
        elif offered:
            with session["history_lock"]:
                session.get("_completion_delivered", set()).difference_update(offered)
                unsecured = session.get(COMPLETION_UNSECURED) or {}
                for pid in offered:
                    unsecured.pop(pid, None)


def _notif_release_turn(session: dict) -> None:
    from tui_gateway.turn_admission import settle
    with session["history_lock"]:
        if settle(session):
            session["running"] = False
        session.pop("_notif_turn_claim", None)
    from tui_gateway.detached_turn_cleanup import settled
    settled(session)


def _notif_release_claim(session: dict, claim) -> None:
    """Release ``running`` only while it is still ``claim``'s: admission that refused (or a turn that ended) already
    released it, and a user prompt may own ``running`` since — clearing it would run two turns on one agent.
    [daveey] Same settle as ``_notif_release_turn`` (the turn-admission generation and the detached grace)."""
    with session["history_lock"]:
        if claim is None or session.get("_notif_turn_claim") is not claim:
            return
    _notif_release_turn(session)


def _notif_claim_turn(session: dict) -> bool:
    """Claim the idle session (running=True) under history_lock; False if a turn is live.
    After the user's Stop no automatic turn starts: the cancel latch holds notifications
    (requeued by the callers) until the next user prompt clears it. While another backend owns
    the chat (owner-refusal backoff) no automatic turn starts either."""
    if _notif_owner_refused(session):
        return False
    from tui_gateway.update_pause import continuation_due
    if continuation_due(session):
        return False  # [daveey] a core-update-paused turn continues first; callers requeue (update_pause)
    with _session_turn_admission(session) as admitted:
        if (not admitted or session.get("running") or session.get("_turn_cancel_requested")
                or session.get("_exit_notices_pending")):
            return False
        session["running"] = True
        session["_notif_turn_claim"] = object()  # this claim's identity; every release of it pops the token
        return True


def _notif_log_failure(what: str, exc: BaseException) -> None:
    print(f"[tui_gateway] {what}: {type(exc).__name__}: {exc}", file=sys.stderr)


# [daveey] A synthesized turn (process completion, subagent result, /loop, /heartbeat, bot DM, kanban notice)
# that the admission gate refuses because another live process holds the chat: SESSION_NOT_OWNED is
# deterministic until that owner lets go, so the poller waits (doubling backoff) instead of re-running the turn
# on its next pass. Before, a refused completion went straight back on the queue and the poller re-ran it every
# ~0.8 s, painting a "Your message was not answered" card each time (139 in 110 s on 2026-10-28). The first
# refusal of a foreign-owner streak shows its card; later probes in the streak are silent. Gateway
# ledger contention never shows a takeover card and retries every 30 s instead of doubling.
_OWNER_REFUSAL_BACKOFF_S = 30.0
_OWNER_REFUSAL_BACKOFF_MAX_S = 300.0
_OWNER_REFUSAL_CODES = ("SESSION_NOT_OWNED", "SESSION_COORDINATION_UNAVAILABLE")


def _notif_owner_refused(session: dict) -> bool:
    """True while an owner-refusal backoff holds this session's automatic turns."""
    return time.monotonic() < float(session.get("_notif_owner_refused_until") or 0)


def _notif_note_owner_refusal(session: dict, reason: str, *, ledger_status: str | None = None) -> None:
    from tui_gateway.owner_forward import FORWARDABLE_LEDGER_STATES
    queue_busy = ledger_status in FORWARDABLE_LEDGER_STATES
    delay = (_OWNER_REFUSAL_BACKOFF_S if queue_busy else
             float(session.get("_notif_owner_refusal_delay") or _OWNER_REFUSAL_BACKOFF_S))
    session["_notif_owner_refusal_delay"] = (
        _OWNER_REFUSAL_BACKOFF_S if queue_busy else min(delay * 2, _OWNER_REFUSAL_BACKOFF_MAX_S))
    session["_notif_owner_refused_until"] = time.monotonic() + delay
    logger.info("automatic turn for session %s refused (%s): another owner holds the chat; next try in %.0fs",
                session.get("session_key") or "", reason, delay)


def _notif_owner_preflight(sid: str, session: dict) -> bool:
    """False = refused silently. Only inside a refusal streak (its card already shown): probe ownership
    before anything is emitted, so a later retry while the chat is still held elsewhere stays invisible."""
    if not session.get("_notif_owner_refusal_delay"):
        return True
    refusal = _ensure_active_session_slot(sid, session)
    if refusal is None:
        return True
    _notif_note_owner_refusal(session, str(getattr(refusal, "reason", None) or "refused"),
                             ledger_status=getattr(refusal, "ledger_status", None))
    return False


def _notif_run_submit(rid, sid, session, text, **kwargs):
    """Run a turn this caller just claimed via _notif_claim_turn.

    A plain stage() adopts the freshly claimed generation instead of inheriting a stale
    one from an enclosing worker context (which would refuse the new turn and leak it)."""
    from tui_gateway.turn_admission import stage
    if not _notif_owner_preflight(sid, session):
        _notif_release_turn(session)
        return False
    session.pop("_turn_owner_refusal", None)
    session.pop("_turn_owner_refusal_ledger_status", None)
    session["_synthesized_turn"] = True  # [daveey] keeps a core-update-paused marker (prompt_turn)
    try:
        with stage(session):
            accepted = _run_prompt_submit(rid, sid, session, text, **kwargs)
    finally:
        session.pop("_synthesized_turn", None)
    reason = session.pop("_turn_owner_refusal", None)
    ledger_status = session.pop("_turn_owner_refusal_ledger_status", None)
    if not accepted and reason in _OWNER_REFUSAL_CODES:
        _notif_note_owner_refusal(session, reason, ledger_status=ledger_status)
    return accepted


def _notif_unowned_event(sid: str, evt: dict, deferred) -> None:
    """Dispose of an addressed event this session cannot prove it owns (never adopt an orphan): park it for its
    owner's return (``notification_park``). One the park refuses: a delegation is dropped at WARNING with its offer
    cleared (``return_completion_offer``; the row stays pending) or, during a shutdown drain, goes onto ``deferred``
    to be requeued; anything else drops."""
    evt_type, is_delegation = evt.get("type", "completion"), evt.get("type") == "async_delegation"
    origin, key = str(evt.get("origin_ui_session_id") or ""), str(evt.get("session_key") or "")
    from tui_gateway import notification_park
    if notification_park.park(evt):
        logger.debug("Parked unowned %s notification (origin=%r key=%r) until its owner returns",
                     evt_type, origin, key)
    elif deferred is not None:
        if is_delegation:
            deferred.append(evt)
        else:
            notification_park.log_drop(evt, f"unowned during shutdown drain (origin={origin!r})")
    else:
        # A durable replay stays pending: hand it back so the orphan sweep re-offers it once its owner
        # is live (#97202), and keep that retry out of WARNING.
        if is_delegation:
            (logger.debug if evt.get("restored") else logger.warning)(
                "Dropping unowned %s notification (origin=%r key=%r) instead of delivering to session %s",
                evt_type, origin, key, sid)
            from tools.async_delegation import return_completion_offer
            return_completion_offer(evt)
        else:
            notification_park.log_drop(evt, f"unowned and not parkable (origin={origin!r}, poller={sid})")


# A retried notice the canonical ledger refuses (SESSION_NOT_OWNED persists while a gateway owns the
# chat) waits before its requeue, doubling per session up to the kanban/loop poll period so the
# poller thread never delays those polls by more than one period.
_NOTIF_REFUSAL_BACKOFF_S = 0.25
_NOTIF_REFUSAL_BACKOFF_MAX_S = _KANBAN_POLL_SECONDS


def _notif_refusal_backoff(session: dict) -> None:
    """Wait before requeueing a refused notice: this session's next delay doubles, to a cap."""
    delay = session.get("_notif_refusal_delay") or _NOTIF_REFUSAL_BACKOFF_S
    session["_notif_refusal_delay"] = min(delay * 2, _NOTIF_REFUSAL_BACKOFF_MAX_S)
    time.sleep(delay)


def _notif_submit(rid: str, sid: str, session: dict, text: str, what: str, **kwargs) -> bool:
    """Only the execution gate can acknowledge a claimed notification turn (message.start is emitted by
    _run_prompt_submit once admitted). Returns the gate's verdict (False = refused, no turn runs): callers
    that already took the notice off a durable store put it back. Every caller claimed ``running`` just
    before, so the token read here is its own; nothing else can claim while ``running`` stays True."""
    claim = session.get("_notif_turn_claim")
    from tui_gateway.turn_admission import note_refusal
    note_refusal(spared_live_turn=False)  # a stale verdict of this thread's earlier refusal never carries over
    # [daveey] The first ownership refusal of a streak shows its one notice; later probes in the streak are
    # silent (_notif_owner_preflight / the owner-refusal backoff).
    kwargs.setdefault("report_ownership_refusal", not session.get("_notif_owner_refusal_delay"))
    try:
        accepted = _notif_run_submit(rid, sid, session, text, **kwargs)
        if accepted:
            session.pop("_notif_refusal_delay", None)
        else:
            # A refusing admission already released ``running``, and a user prompt may own it since. A caller
            # that requeues the notice backs off first (_notif_refusal_backoff); the rest return at once.
            # [daveey] A refusal over a live turn on this record leaves its ``running`` and claim alone.
            from tui_gateway.turn_admission import refusal_spared_live_turn
            if not refusal_spared_live_turn():
                _notif_release_claim(session, claim)
        return accepted
    except Exception as exc:
        _notif_log_failure(what, exc)
        _notif_release_claim(session, claim)
        raise


def _notif_loop_status(sid: str, text: str) -> None:
    _emit("status.update", sid, {"kind": "loop", "text": text})


def _notif_slash_loop_tick(rid: str, sid: str, session: dict, mgr, wakeup: str) -> None:
    """Slash-command /loop wakeup: route through the slash pipeline, not the model. No model reply to evaluate, so the
    tick completes immediately — unless the command resolves to a prompt (skill command etc.), which runs as a normal
    turn whose post-turn hook completes the tick."""
    _notif_release_turn(session)
    try:
        parts = wakeup.lstrip()[1:].split(None, 1)
        resp = _methods["command.dispatch"](
            rid, {"name": parts[0] if parts else "", "arg": parts[1] if len(parts) > 1 else "", "session_id": sid})
        payload = (resp or {}).get("result") or {}
        if out := str(payload.get("output") or "").strip():
            _notif_loop_status(sid, out)
        if payload.get("type") == "send" and payload.get("message"):
            if not _notif_claim_turn(session):
                mgr.abandon_tick()
                _publish_session_control_snapshot(sid, session)  # back to the countdown, not "running now"
                return
            # Releases the claim on failure: the swallow below would otherwise leave the session busy for good.
            # Typed loop_tick: the expanded command is the loop's, never read as a person's reply. [daveey]
            from hermes_cli.loops import loop_tick_display
            _notif_submit(rid, sid, session, payload["message"], "loop wakeup send failed", **loop_tick_display(wakeup))
            return
    except Exception:
        pass
    decision = mgr.complete_tick("")
    if decision.get("message"):
        _notif_loop_status(sid, decision["message"])
    _publish_session_control_snapshot(sid, session)  # next_due_at moved: clients re-anchor the countdown


def _notif_gateway_owns_heartbeat(session: dict, session_key: str) -> bool:
    """Whether the gateway's heartbeat poller owns this session's due tick.

    Desktop/TUI can attach to a messaging conversation, but its session-owner poller has no adapter
    route for the reply. Ownership is the gateway's LIVE routing index, not the row's immutable
    ``source``: ``gateway/run_heartbeat_restore.py`` only registers watches for a non-suspended,
    origin-bearing key whose current ``session_id`` is this one, so a row archived by /reset,
    auto-reset or compression rotation belongs to nobody there and must keep firing here. The index
    lives in the gateway's home store (the launch handle for a multiplexed gateway, the profile's
    own store for a per-profile gateway), so both are consulted; no entry is fail-open.
    """
    try:
        with _session_db(session) as db:
            for store in {id(d): d for d in (db, _get_db()) if d is not None}.values():
                if (entry := store.gateway_routing_entry_for_session(session_key)) is not None:
                    return bool(entry.get("origin")) and not entry.get("suspended")
        return False
    except Exception:
        return False


def _maybe_fire_tui_heartbeat_tick(sid: str, session: dict) -> None:
    """Fire a due /heartbeat prompt for an idle TUI/Desktop/dashboard session (#102056, #103044).

    ``/heartbeat`` runs in the slash worker, whose CLI watchdog queues the due prompt into a
    ``_pending_input`` no turn loop ever drains — armed-but-dead. State is durable in SessionDB, so
    the session-owner process drives firing exactly like ``_maybe_fire_tui_loop_tick``: claim the
    idle session first (a racing user prompt wins), then re-enter through ``_run_prompt_submit`` as a
    plain user turn. A dispatch that never starts a turn rewinds the persisted fire so the tick stays
    due instead of being silently consumed.
    """
    try:
        from hermes_cli.heartbeat import HeartbeatManager
    except Exception:
        return
    if not (sid_key := session.get("session_key") or ""):
        return
    from tui_gateway.notification_idle import schedule_manager
    mgr = schedule_manager(session, "heartbeat", HeartbeatManager)
    if not mgr.is_active() or not mgr.state.is_due() or _notif_gateway_owns_heartbeat(session, sid_key):
        return  # not due, or the gateway poller owns the routed conversation — stays due there
    if not _notif_claim_turn(session):
        return  # busy — the tick coalesces to the next idle poll
    claim = session.get("_notif_turn_claim")
    if not (prompt := mgr.due_prompt()):
        _notif_release_turn(session)
        return
    started = False
    try:
        _emit("status.update", sid, {"kind": "heartbeat", "text": f"♥ heartbeat #{mgr.state.fire_count} firing…"})
        started = bool(_notif_run_submit(f"__heartbeat__{int(time.time() * 1000)}", sid, session, prompt))
    except Exception as exc:
        _notif_log_failure("heartbeat dispatch failed", exc)
    if not started:
        # A refusing admission already released ``running`` (a user prompt may own it since): release only our claim.
        _notif_release_claim(session, claim)
        with contextlib.suppress(Exception):
            mgr.abandon_fire()


def _session_owns_live_wakeup_schedule(session: dict) -> bool:
    """True while THIS process's notification poller is the only driver of an active /loop or /heartbeat for
    ``session`` — the reapers must then keep the detached session (and with it the poller) alive, or the schedule
    freezes until a client reattaches. Gateway-routed schedules are fired by the gateway, so they never pin the
    session here. Bounded: a loop pauses at its tick budget and a stopped/paused schedule releases the session.
    Fail-open to False: an unreadable store must not make a session unreapable."""
    if not (sid_key := session.get("session_key") or ""):
        return False
    try:
        from hermes_cli.heartbeat import HeartbeatManager
        from hermes_cli.loops import LoopManager
        with _session_profile_runtime_scope(session, hydrate_secrets=False):
            loop = LoopManager(session_id=sid_key)
            if loop.is_active() and not _loop_route_is_gateway_chat(loop.state):
                return True
            heartbeat = HeartbeatManager(session_id=sid_key)
            return heartbeat.is_active() and not _notif_gateway_owns_heartbeat(session, sid_key)
    except Exception:
        logger.debug("wakeup-schedule check failed for %s", sid_key, exc_info=True)
        return False


def _session_owns_pending_process_notification(session: dict) -> bool:
    """True while a background process this session started will still report back into it (``notify_on_complete``
    or ``watch_patterns``): running, or its notice not yet routed (see ``tools.process_registry_awaiting``). The notice
    reaches the model only through THIS session's notification poller, and closing the session kills the process
    (``agent_close``), so the reapers must keep it alive. Ownership is the set close() kills by — the agent's raw
    spawning task ids, which keep pre-compression ids after the session key rotates. Bounded by delivery: the session
    is released once routing admits the notice's turn (``running`` then protects it), requeues or drops it."""
    if session.get("_turn_cancel_requested"):
        # After Stop no notification turn can start until the user's next prompt (``_notif_claim_turn``), so a
        # pending notice is no reason to keep the session: the user's prompt resumes it like any other.
        return False
    if session.get("_notif_held"):
        # [daveey] A notice whose turn admission refused (update hold, a paused turn's continuation due) waits in
        # this session's held list: off the queue, out of the in-flight hold, so awaits_notification cannot see
        # it. Reaping now parks it unowned and drops it 10 min later (hub chat 20261111_005201_05663e).
        return True
    from tools.process_registry import process_registry
    from tools.process_registry_awaiting import awaits_notification
    return awaits_notification(process_registry, _session_process_owners(session))


def _session_process_owners(session: dict) -> set:
    """The raw spawning ids ``close()`` kills by (pre-compression ids included), plus the current key."""
    agent = session.get("agent")
    return {*getattr(agent, "_process_owner_task_ids", ()), str(session.get("session_key") or "")} - {""}


def _maybe_fire_tui_loop_tick(sid: str, session: dict) -> None:
    """Fire a due /loop wakeup for an idle TUI/Desktop/dashboard session (per-session poller, coarse cadence). Claims
    the session (running=True) before dispatching so a racing user prompt wins; the post-turn hook completes the tick."""
    try:
        from hermes_cli.loops import LoopManager, goal_blocks_loop_tick
    except Exception:
        return
    if not (sid_key := session.get("session_key") or ""):
        return
    from tui_gateway.notification_idle import schedule_manager
    mgr = schedule_manager(session, "loop", LoopManager)
    if not mgr.is_due() or goal_blocks_loop_tick(sid_key) or _loop_route_is_gateway_chat(mgr.state):
        return  # not due, or the gateway's wakeup scanner owns the routed chat — stays due there
    if not _notif_claim_turn(session):
        return  # busy — stays due, next poll retries
    claim = session.get("_notif_turn_claim")
    if not (wakeup := mgr.fire_tick()):
        _notif_release_turn(session)
        return
    rid = f"__loop__{int(time.time() * 1000)}"
    _publish_session_control_snapshot(sid, session)  # awaiting_response: clients swap the countdown for "running"
    try:
        _notif_loop_status(sid, f"↻ /loop wakeup #{mgr.state.ticks_fired if mgr.state else '?'} firing…")
        if wakeup.lstrip().startswith("/"):
            _notif_slash_loop_tick(rid, sid, session, mgr, wakeup)
        else:
            if not _notif_owner_preflight(sid, session):  # still held elsewhere: no dangling message.start
                _notif_release_turn(session)
                with contextlib.suppress(Exception):
                    mgr.abandon_tick()
                return
            _emit("message.start", sid)
            if not _notif_run_submit(rid, sid, session, wakeup):
                # Refused at admission (another surface owns the chat): no turn will settle this tick. Admission
                # already released ``running``; only the loop state rolls back here.
                _notif_abandon_loop_tick(sid, session, mgr)
    except Exception as exc:
        _notif_log_failure("loop wakeup dispatch failed", exc)
        _notif_release_claim(session, claim)  # still ours unless admission released it before raising
        _notif_abandon_loop_tick(sid, session, mgr)


def _notif_abandon_loop_tick(sid: str, session: dict, mgr) -> None:
    """Roll the fired tick back to the countdown. Never touches ``running``: it may be another turn's by now."""
    with contextlib.suppress(Exception):
        mgr.abandon_tick()
    _publish_session_control_snapshot(sid, session)  # back to the countdown, not "running now"


def _kb_first_line(value: Any, limit: int) -> str:
    lines = str(value).strip().splitlines()
    return f"\n{lines[0][:limit]}" if lines else ""


def _kb_completed(task, payload: dict, title: str) -> str:
    from hermes_cli.kanban_final_send import public_summary
    from hermes_cli.kanban_artifact_receipt import format_receipt
    handoff = (_kb_first_line(payload["summary"], 200) if payload.get("summary")
               else _kb_first_line(public_summary(getattr(task, "result", None)), 160))
    return f" done — {title}{handoff}{_kb_left_open(payload)}{format_receipt(payload)}"


# kind -> (glyph, suffix after "Kanban <id>"); silent kinds (archived/unblocked) are absent → None.
def _kb_spend_budget(task, payload, title):
    from hermes_cli.kanban_spend_decision import notice
    return ": " + notice({**(payload or {}), "task_id": getattr(task, "id", None)})


def _kb_decision_requested(payload, *, parent=False):
    from hermes_cli.kanban_decision_ask import notice_text
    from hermes_cli.kanban_parent_held import text
    return ": " + (text(payload) if parent else notice_text(payload))


def _kb_park_progress(payload):
    from hermes_cli.kanban_done_tail import notice_text
    return ": " + notice_text(payload)


def _unproven_text(payload):
    from hermes_cli.kanban_wake_unproven import notice_text
    return notice_text(payload)


_KANBAN_EVENT_FORMATTERS = {
    "park_progress_notice": ("ℹ", lambda t, p, title: _kb_park_progress(p)),
    "decision_requested": ("❓", lambda t, p, title: _kb_decision_requested(p)),
    "parent_held": ("⏸", lambda t, p, title: _kb_decision_requested(p, parent=True)),
    "spend_budget_decision_ready": ("⏸", _kb_spend_budget),
    "request_withdrawn": ("ℹ", lambda t, p, title: " request withdrawn: the card was archived. "
                         "No answer is needed for this request; this is not approval and not proof of completion."),
    "resolved_check_broken": ("⏸", lambda t, p, title: " blocked"),  # body rendered by block_notice in the formatter
    "resolved_check_lost": ("⏸", lambda t, p, title: " blocked"),
    "completed": ("✔", _kb_completed),
    "blocked": ("⏸", lambda t, p, title: " blocked" + (f": {str(p.get('reason'))[:160]}" if p.get("reason") else "")),
    "failure_finalized": ("✖", lambda t, p, title: _kb_failure("failure_finalized", p, title)),
    "gave_up": ("✖", lambda t, p, title: _kb_failure("gave_up", p, title)),
    "crashed": ("✖", lambda t, p, title: _kb_failure("crashed", p, title)),
    "timed_out": ("⏱", _kb_timed_out),
    "status": ("🔄", lambda t, p, title: f" → {p.get('status') or ''}"),
    "quota_episode_notice": ("⏳", lambda t, p, title: _kb_quota_episode(p)),
    "worker_repin_notice": ("⚠", lambda t, p, title: _kb_repin_notice(p)),
    "wake_probe_unproven_notice": ("⚠", lambda t, p, title: ": " + _unproven_text(p)),
    "dependency_parent_unclaimed": ("⏳", lambda t, p, title: _kb_parent_unclaimed(p)),
    "chat_wait_unclaimed": ("⏳", lambda t, p, title: _kb_chat_wait(p)),
}


def _format_kanban_event_text(sub: dict, task, ev, board_slug: str) -> Optional[str]:
    """Single-line notification text for one kanban event; wording mirrors gateway/kanban_watchers.py (reads the same
    as on Telegram). None for silent kinds."""
    from hermes_cli.kanban_failure_notice import provisional_failure
    if provisional_failure(ev):
        return None
    if (entry := _KANBAN_EVENT_FORMATTERS.get(getattr(ev, "kind", ""))) is None:
        return None
    glyph, fmt = entry
    task_id = sub.get("task_id", "")
    title = (getattr(task, "title", None) or task_id)[:120]
    who = getattr(task, "assignee", None) or ""
    prefix = f"{glyph} " + (f"[{board_slug}] " if board_slug else "") + (f"@{who} " if who else "")
    payload = getattr(ev, "payload", None) or {}
    if getattr(ev, "kind", "") in ("blocked", "resolved_check_broken", "resolved_check_lost"):
        from hermes_cli.kanban_block_notice import ask_update_notice, block_notice, is_metadata_only
        if ev.kind == "blocked" and is_metadata_only(payload):  # an ask amendment, not a new block [daveey]
            body = ask_update_notice(task, task_id, board_slug, payload, chat_id=sub.get("chat_id"))
            return f"{prefix}Kanban {task_id} {body}" if body is not None else None
        return f"{prefix}Kanban {task_id} " + block_notice(task, task_id, board_slug, reason=str(payload.get("reason") or ""),
                                                         actor=str(payload.get("actor") or ""),
                                                         drop_event=ev.kind if ev.kind != "blocked" else "")
    if getattr(ev, "kind", "") == "completed":
        # A worker refused a send into this chat put its message in the summary as a FINAL SEND line. [daveey]
        from hermes_cli.kanban_final_send import final_send_for_chat
        from hermes_cli.kanban_artifact_receipt import format_receipt
        if final := final_send_for_chat(payload, str(sub.get("chat_id") or "")):
            return f"{prefix}Kanban {task_id} done — {title}\n{final}{_kb_left_open(payload)}{format_receipt(payload)}"
    return f"{prefix}Kanban {task_id}{fmt(task, payload, title)}"


def _kb_board_key(_kb, board_meta) -> tuple[str, str]:
    """(slug, resolved DB identity) — multiple slugs can point at one DB when HERMES_KANBAN_DB pins it."""
    slug = (board_meta or {}).get("slug") or _kb.DEFAULT_BOARD
    db_path = (board_meta or {}).get("db_path")
    try:
        return slug, str(Path(db_path).expanduser().resolve() if db_path else _kb.kanban_db_path(slug).resolve())
    except Exception:
        return slug, f"slug:{slug}"


def _kb_board_file_sig(db_path, chat_id: str | None = None) -> tuple | None:
    """What a poll of this board for ``chat_id`` could claim, read through a guarded retained reader once per
    commit for every chat (``notification_idle.board_signature``): bookkeeping commits keep it, so they no
    longer re-poll every board for every chat (t_01a1cee98b798343). Stat alone misses WAL publication."""
    from tui_gateway.notification_idle import board_signature
    try:
        return board_signature(db_path, chat_id)
    except Exception:
        logger.debug("Kanban revision unavailable", exc_info=True)
        return None  # unreadable never means unchanged


def _kb_poll_board(_kb, slug: str, session_key: str, claims: list | None = None, seen: dict | None = None,
                   *, stray: tuple = (), unreclaimed: list | None = None) -> list:
    """Claim + format this session's unseen events on one board. One poller per live session: the board is not opened
    writable unless it has a subscription owned by this exact session (a failed read-only probe — locked/corrupt DB —
    falls through so delivery is preserved).

    *seen* (the session's ``{db path: signature}``) skips a board where nothing this session could claim has
    changed since its last completed poll (``_kb_board_file_sig``): no new deliverable event and no change to its
    own subscription rows. 21 open chats each probing every board every 5 s, and opening it writable to re-claim
    nothing, kept the Desktop backend busy. The signature is taken before the poll, so a commit during it is
    polled again next tick."""
    from hermes_cli import kanban_db_connect as _kbc
    from hermes_cli import kanban_db_notify as _kbn
    from hermes_cli.kanban_placement import is_placeholder_session
    if is_placeholder_session(session_key):
        return []
    # Reclaim before the quiet-board shortcut: a failed reclaim must retry even without a board commit.
    reclaimed = not stray or _kb_reclaim_stray_subs(_kbn, slug, session_key, stray)
    if not reclaimed and unreclaimed is not None:
        unreclaimed.append(slug)
    db_path = sig = None
    if seen is not None:
        with contextlib.suppress(Exception):
            db_path = str(_kb.kanban_db_path(board=slug))
            sig = _kb_board_file_sig(db_path, session_key)
        if reclaimed and sig is not None and seen.get(db_path) == (session_key, sig):
            from hermes_constants import get_hermes_home
            from tui_gateway.kanban_consumer_health import polled
            polled(get_hermes_home(), session_key, db_path)
            return []
    texts = _kb_poll_board_now(_kb, _kbc, _kbn, slug, session_key, claims)
    from tui_gateway.notification_idle import board_quiet
    if reclaimed and sig is not None and board_quiet(db_path, session_key):
        seen[db_path] = (session_key, sig)
    elif seen is not None:
        seen.pop(db_path, None)
    return texts


def _kb_stray_delegate_keys(session: dict, session_key: str) -> tuple:
    """This session's marked delegate subagent ids, looked up once per live session. Before the reserved child
    kept its ``_delegate_from`` marker, ``kanban_create`` resolved the parent's key to the unmarked child and
    subscribed the card under the child's id, so its completions went to the subagent (#150198)."""
    if (cached := session.get("_kanban_stray_keys")) is not None:
        return cached
    import sqlite3
    try:
        with _session_db(session) as db:
            if db is None:
                return ()  # store unavailable: not cached, so a later poll retries
            from hermes_state_delegate_children import delegate_descendant_ids
            keys = tuple(delegate_descendant_ids(db, session_key))
    except sqlite3.Error as exc:  # busy/locked store: not cached, so a later poll retries
        _notif_log_failure("kanban stray-subscription lookup failed", exc)
        return ()
    session["_kanban_stray_keys"] = keys
    return keys


def _kb_reclaim_stray_subs(_kbn, slug: str, session_key: str, stray: tuple) -> bool:
    """Re-key ``tui`` subscriptions bound to this session's own subagents back onto it. True when the board is
    clean afterwards (nothing to move, or moved); False on a DB error so the caller retries next poll."""
    from hermes_cli import kanban_db_connect as _kbc
    try:
        if not any(_kbn.count_notify_subs(board=slug, platform="tui", chat_id=k) for k in stray):
            return True
        with contextlib.closing(_kbc.connect(board=slug)) as conn:
            _kbn.retarget_notify_subs(conn, platform="tui", from_chat_ids=stray, to_chat_id=session_key)
        return True
    except Exception as exc:  # corruption, permissions and archival races also retry without dropping other boards
        logger.debug("Kanban stray-subscription reclaim failed on %s", slug, exc_info=True)
        _notif_log_failure(f"kanban stray-subscription reclaim failed on {slug}", exc)
        return False


def _kb_poll_board_now(_kb, _kbc, _kbn, slug: str, session_key: str, claims: list | None) -> list:
    from tui_gateway.notification_idle import board_quiet
    with contextlib.suppress(Exception):
        # A global event hint wakes quiet historical subscribers too. Prove
        # absence before acquiring a writable/migrating handle, not just after.
        # In-flight claims and unknown reads still enter clock/liveness recovery.
        if board_quiet(_kb.kanban_db_path(board=slug), session_key):
            return []
    try:
        conn = _kbc.connect(board=slug)
    except Exception:
        return []
    texts: list = []
    from tui_gateway.kanban_leg_custody import chat_lineage, gateway_claims
    chats = chat_lineage(session_key, _session_lineage)  # its local rows may sit on the lineage root
    with contextlib.closing(conn):
        try:
            # A global deliverable-event hint also wakes chats with only historical rows. Keep quiet and
            # silent-only rows inside SQLite; in-flight rows stay eligible for clock/liveness recovery.
            subs = _kbn.list_notify_subs(conn, platform="tui", chat_id=session_key, pending_only=True,
                                         pending_kinds={"tui": _KANBAN_NOTIFY_KINDS})
        except Exception:
            return []
        if subs:
            # A claim whose holder process died (serve replaced mid-turn) goes back to the cursor. [daveey]
            with contextlib.suppress(Exception):
                _kbn.recover_orphaned_notify_claims(conn, platform="tui", chat_id=session_key)
        for sub in subs:
            if (sub.get("platform") or "").lower() != "tui" or sub.get("chat_id") != session_key:
                continue
            if gateway_claims(session_key, sub := {**sub, "sibling_chats": chats}):
                continue  # the live gateway owns the chat and claims this row within one tick (kanban_leg_custody)
            sub_ident = dict(task_id=sub["task_id"], platform=sub["platform"], chat_id=sub["chat_id"],
                             thread_id=sub.get("thread_id") or "")
            from gateway.kanban_wake_sibling import tui_defer
            if tui_defer(conn, sub):
                continue  # the chat's waking local row still owes this event's turn; it settles this row [daveey]
            # durable: the claim stays IN FLIGHT (owned by this process) until the notice is in the transcript
            # (_kb_ack_durable acks it); a process that dies first hands it back. [daveey]
            _old, _new, events = _kbn.claim_unseen_events_for_sub(conn, kinds=_KANBAN_NOTIFY_KINDS,
                                                                   deliverable_kinds=_KANBAN_EVENT_FORMATTERS,
                                                                   durable=claims is not None, **sub_ident)
            if not events:
                continue
            from gateway.kanban_wake_sibling import wake_owes_after
            if wake_owes_after(conn, sub, _old):
                # A covered event landed between tui_defer and the claim: hand it back to the wake. [daveey]
                with contextlib.suppress(Exception):
                    _kbn.rewind_notify_cursor(conn, claimed_cursor=_new, old_cursor=_old, **sub_ident)
                continue
            claim = None
            if claims is not None:  # so a refused turn can hand the range back (_kb_rewind_refused) [daveey]
                claim = {"board": slug, **sub_ident, "old": _old, "new": _new, "texts": [],
                         "notifier_profile": sub.get("notifier_profile"), "claimed_mono": time.monotonic()}
                claims.append(claim)
            task = _kb.get_task(conn, sub["task_id"])
            from gateway.kanban_wake_sibling import tui_formatter
            from gateway.kanban_watchers_notifier import coalesce_superseded_blocks, diagnostic_event
            from gateway.warning_notifications import DiagnosticText
            sub_texts = []
            fmt = tui_formatter(conn, sub, _format_kanban_event_text)  # a waking local row's turn carries some [daveey]
            for ev in coalesce_superseded_blocks(events):  # a block superseded by a drop in this batch renders once
                text = fmt(sub, task, ev, slug)
                if text:
                    text = DiagnosticText(text) if diagnostic_event(ev) else _KanbanText(text)
                    from hermes_cli.kanban_failure_notice import event_key
                    text._kb_keys = (event_key(slug, ev),)  # type: ignore[attr-defined]
                    text._kb_event = ev  # type: ignore[attr-defined]  # _kb_settle_held re-checks custody [daveey]
                    texts.append(text)
                    sub_texts.append(text)
                    if claim is not None:
                        claim["texts"].append(text)
            if not sub_texts:
                acked = False
                with contextlib.suppress(Exception):  # nothing to deliver: the claim is complete as is
                    acked = _kbn.ack_notify_claim(conn, upto=_new, from_cursor=_old, **sub_ident)
                if claim is not None and claims is not None:
                    if acked:
                        claims.remove(claim)  # silent kinds only (archived/unblocked): nothing left to hand back
                    else:
                        # An earlier claim of this process is still pending, so the ack was skipped: keep it as
                        # delivered, and the next ack run covers it. Dropping it leaked this range in flight with
                        # nothing in it to deliver, blocking every other consumer (t_01a199de3003e911). [daveey]
                        claim["durable"] = True
            # Unsubscribe only on archive: ``done`` is reversible in review/controller flows, so keeping the sub lets a
            # later reopen notify the same session. The claimed cursor prevents replay. A sub with notices still in
            # flight is removed by their ack instead, so the claim it records survives until delivery. [daveey]
            if task and getattr(task, "status", "") == "archived" and (not sub_texts or claim is None):
                with contextlib.suppress(Exception):
                    _kbn.ack_notify_claim(conn, upto=_new, from_cursor=_old, **sub_ident)
    from hermes_constants import get_hermes_home
    from tui_gateway.kanban_consumer_health import polled
    polled(get_hermes_home(), session_key, _kb.kanban_db_path(board=slug))
    return texts


def _collect_kanban_notifications(session: dict, claims: list | None = None, *, remote_only=False) -> list:
    """Claim unseen terminal kanban events for this session's ``platform="tui"`` subscriptions (``kanban_create``
    auto-subscribes with ``chat_id=HERMES_SESSION_KEY``; no "tui" messaging adapter exists, so this poller is the
    delivery path). Same atomic cursor-claim as the gateway notifier: exactly-once even if a gateway polls the same DB.
    See #59890. An owned chat's remote notices: kanban_remote_owner while it holds the chat's work, else this poller
    once ownership ends; the sweep skips live chats (a claim here was handed straight back, review 5890277360). [daveey]
    """
    session_key = str(session.get("session_key") or "")
    if not session_key or session.get("_finalized"):
        return []
    if remote_only:
        return [] if _kb_gateway_owns(session) else _collect_remote_kanban_notifications(session_key, claims, session=session)
    try:
        from hermes_cli import kanban_db as _kb
    except Exception:
        return []
    try:
        boards = ([{'slug': slug} for slug in session['_notif_local_boards']]
                  if '_notif_local_boards' in session else _kb.list_boards(include_archived=False))
    except Exception:
        try:
            boards = [_kb.read_board_metadata(_kb.DEFAULT_BOARD)]
        except Exception:
            return []
    # dict keyed by resolved DB identity: first slug per DB wins (a pinned HERMES_KANBAN_DB aliases slugs).
    unique = {}
    for slug, resolved in (_kb_board_key(_kb, board_meta) for board_meta in boards):
        unique.setdefault(resolved, slug)
    seen = session.setdefault("_kb_board_seen", {})
    stray, unreclaimed = _kb_stray_delegate_keys(session, session_key), []
    owned = _kb_gateway_owns(session)  # the gateway's collector admits the rows it claims in one tick: skipped below
    texts = [t for slug in unique.values() for t in _kb_poll_board(
        _kb, slug, session_key, claims, None if '_notif_local_boards' in session else seen,
        stray=stray, unreclaimed=unreclaimed)]
    if stray and not unreclaimed:
        session["_kanban_stray_keys"] = ()  # every board is clean; stop probing
    return texts if owned else texts + _collect_remote_kanban_notifications(session_key, claims, session=session)


_LINEAGE_TTL_S = 30.0
_lineage_memo: dict[tuple, tuple] = {}  # (state.db, session key) -> (monotonic at, lineage)


def _session_lineage(session_key: str, limit: int = LINEAGE_LIMIT) -> tuple:
    """Earlier ids of this chat (compaction parents): a remote card stays linked to the id that created it.

    Every live chat's poller asked for it every 5 s, opening state.db each time (t_01a1cee98b798343). A chat's
    parents are set when it is created and only change on import or deletion repair, so a found chain is reused
    for ``_LINEAGE_TTL_S``; a chat not yet in the store is looked up again on the next poll."""
    from hermes_constants import get_hermes_home
    path = Path(get_hermes_home()) / "state.db"
    memo_key, now = (str(path), session_key), time.monotonic()
    if (hit := _lineage_memo.get(memo_key)) is not None and now - hit[0] < _LINEAGE_TTL_S:
        return hit[1]
    out: list = []
    found = False
    with contextlib.suppress(Exception):
        import sqlite3
        if not path.is_file():
            return ()
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.5)
        try:
            sid = session_key
            for _ in range(limit):
                row = conn.execute("SELECT parent_session_id FROM sessions WHERE id = ?", (sid,)).fetchone()
                found = found or (row is not None and sid == session_key)
                if not row or not row[0] or row[0] in out:
                    break
                sid = str(row[0])
                out.append(sid)
        finally:
            conn.close()
    if found:
        if len(_lineage_memo) >= 256:
            _lineage_memo.clear()
        _lineage_memo[memo_key] = (now, tuple(out))
    return tuple(out)


def _forwarded_locally(remote_task_id: str) -> bool:
    """True when a deferred-placement placeholder on this board forwarded to ``remote_task_id``: it reports the
    result through its own tui subscription, so a remote notice would repeat it. Called only for a fresh,
    not-yet-claimed remote state (rare), so the unindexed event scan stays off the hot poll path."""
    try:
        from hermes_cli import kanban_db_connect as _kbc
        from hermes_cli.kanban_placement_forward import FORWARDED
        with _kbc.connect_closing() as conn:
            return conn.execute("SELECT 1 FROM task_events WHERE kind = ? AND payload LIKE ? LIMIT 1",
                                (FORWARDED, f'%"{remote_task_id}"%')).fetchone() is not None
    except Exception:
        return False


def _remote_transcript_has(session_key: str):
    """``(notice_key, task_id) -> bool`` over this chat's lineage, for orphan and legacy reconciliation."""
    def check(notice_key: str, task_id: str) -> bool:
        try:
            with _session_db({"session_key": session_key}) as db:
                from tui_gateway.kanban_remote_notices import notice_in_transcript
                return notice_in_transcript(db, session_key, notice_key, task_id)
        except Exception:
            logger.debug("remote kanban transcript check failed", exc_info=True)
            return False
    return check


def _collect_remote_kanban_notifications(session_key: str, claims: list | None, *, session=None) -> list:
    """Done/blocked notices for cards on other gateways linked to this chat (``kanban_remote_notices``). [daveey]"""
    try:
        from hermes_cli.profiles import get_active_profile_name
        from tui_gateway import kanban_remote_notices as _krn
        if not _krn.hub_db_path().is_file():
            return []
        def on_defer():
            if session is not None:
                session["_kanban_remote_retry_at"] = time.monotonic() + 1.0
        found = _krn.collect(session_key, get_active_profile_name() or "default",
                             lineage=_session_lineage(session_key), skip=_forwarded_locally,
                             transcript_has=_remote_transcript_has(session_key), on_defer=on_defer,
                             shared_window=True, **({'keys': session['_notif_remote_batch']}
                                 if session is not None and '_notif_remote_batch' in session else {}))
        if (getattr(found, 'incomplete', False) and session is not None
                and '_notif_remote_batch' in session):
            session['_kanban_remote_retry_at'] = found.next_deadline or time.monotonic() + 1.0
    except Exception as exc:
        if session is not None:
            if '_notif_remote_batch' in session:
                session['_notif_f_retry_at'] = time.monotonic() + 1.0
            session.pop("_kanban_remote_retry_at", None)
        _notif_log_failure("remote kanban notification poll failed", exc)
        return []
    texts = []
    for item in found:
        text = _KanbanText(item["text"])
        text._kb_remote = item
        if item.get("event_key"):
            text._kb_keys = tuple(item.get("event_keys") or [item["event_key"]])  # type: ignore[attr-defined]
        text._kb_blocked = bool(item.get("blocked_on_human"))  # type: ignore[attr-defined]  # notice guarantee
        texts.append(text)
        if claims is not None:
            claims.append({"remote": item, "texts": [text]})
    return texts


def _notif_poll_kanban(sid: str, session: dict, *, remote_only=False) -> None:
    from tui_gateway.notification_remote_retry import attempt
    with attempt(session, time.monotonic):
        _notif_poll_kanban_attempt(sid, session, remote_only=remote_only)


class _KanbanText(str):
    """A plain kanban notice with identity, so a claim can tell which of its texts were delivered. [daveey]"""


# A refused kanban turn (another backend owns the chat: SESSION_NOT_OWNED) hands its claimed cursor range back
# and this session stops claiming for a while, so the owner's poller gets the events instead of this one
# re-claiming them every 5 s. [daveey]
_KANBAN_REFUSAL_BACKOFF_S = 15.0
_KANBAN_REFUSAL_BACKOFF_MAX_S = 300.0


def _kb_rewind_refused(session: dict, batch: list, *, not_owned: bool = False) -> None:
    """CAS-rewind wholly refused claims newest first; retain partially delivered claims to avoid replay.

    ``not_owned`` also releases each unbuffered subscription's whole in-flight marker, including earlier
    unacked ranges: a cursor rewind alone leaves the owner blocked behind this process. [daveey]"""
    from hermes_cli import kanban_db_connect as _kbc
    from hermes_cli import kanban_db_notify as _kbn
    claims = session.get("_kanban_claims") or []
    refused = {id(t) for t in batch}
    keep_back: set = set()
    remaining = []
    for claim in reversed(claims):
        texts = claim.get("texts") or []
        if claim.get("durable"):
            # Already in the transcript (written before the turn): nothing to hand back. Unless the chat is owned
            # elsewhere (the whole marker is released below), an unacked one stays listed for the next ack run.
            if not claim.get("acked") and (claim.get("remote") or not not_owned):
                remaining.append(claim)
            continue  # [daveey]
        if texts and all(id(t) in refused for t in texts):
            if claim.get("remote"):
                from tui_gateway import kanban_remote_notices as _krn
                if _krn.release(claim["remote"]):
                    continue
                keep_back.update(id(t) for t in texts if id(t) in refused)
                remaining.append(claim)
                continue
            try:
                conn = _kbc.connect(board=claim["board"])
                with contextlib.closing(conn):
                    ok = _kbn.rewind_notify_cursor(
                        conn, task_id=claim["task_id"], platform=claim["platform"], chat_id=claim["chat_id"],
                        thread_id=claim.get("thread_id") or "", claimed_cursor=claim["new"], old_cursor=claim["old"])
            except Exception as exc:
                _notif_log_failure("kanban notification rewind failed", exc)
                ok = False
            if ok:
                continue
        keep_back.update(id(t) for t in texts if id(t) in refused)
        remaining.append(claim)
    session["_kanban_claims"] = list(reversed(remaining))
    if not_owned:
        held = {(c["board"], c["task_id"], c["platform"], c["chat_id"], c.get("thread_id") or "")
                for c in remaining if not c.get("remote")}
        subs = {(c["board"], c["task_id"], c["platform"], c["chat_id"], c.get("thread_id") or "")
                for c in claims if not c.get("remote")} - held
        for board, task_id, platform, chat_id, thread_id in sorted(subs):
            try:
                with contextlib.closing(_kbc.connect(board=board)) as conn:
                    _kbn.release_notify_claim(conn, task_id=task_id, platform=platform, chat_id=chat_id,
                                              thread_id=thread_id, why="does not own the chat")
            except Exception:
                logger.warning("kanban notice claim release failed for %s/%s", board, task_id, exc_info=True)
    rewound = refused - keep_back
    with session["history_lock"]:
        session["_kanban_pending"] = [t for t in batch if id(t) not in rewound] + [
            t for t in (session.get("_kanban_pending") or []) if id(t) not in refused]
        delay = session.get("_kanban_refusal_delay") or _KANBAN_REFUSAL_BACKOFF_S
        session["_kanban_refusal_delay"] = min(delay * 2, _KANBAN_REFUSAL_BACKOFF_MAX_S)
        session["_kanban_refused_until"] = time.monotonic() + delay
        from tui_gateway import kanban_consumer_health
        kanban_consumer_health.invalidate(session)
    logger.info("kanban notification turn refused for session %s: rewound %d of %d notice(s); retry in %.0fs",
                session.get("session_key") or "", len(rewound), len(refused), delay)


def _kb_mark_delivered(session: dict, batch: list) -> None:
    delivered = {id(t) for t in batch}
    claims = []
    for claim in session.get("_kanban_claims") or []:
        claim["texts"] = [t for t in claim.get("texts") or [] if id(t) not in delivered]
        # A durable claim _kb_ack_durable could not ack yet (an earlier claim of the same sub was still pending)
        # stays listed so the next ack run covers it; dropping it leaked its range in flight. [daveey]
        if claim["texts"] or (claim.get("durable") and not claim.get("acked")):
            claims.append(claim)
    session["_kanban_claims"] = claims
    session.pop("_kanban_refusal_delay", None)
    session.pop("_kanban_refused_until", None)


def _kb_text_keys(texts) -> list:
    return [k for t in texts for k in (getattr(t, "_kb_keys", None) or ())]


def _kb_ack_durable(session: dict, durable_ids: set) -> None:
    """Mark claims whose every text is now in the transcript (``durable_ids``) and ack them on the board: per
    subscription, the contiguous run of durable claims from the earliest outstanding one (an earlier claim still
    pending keeps the in-flight marker, so a crash still redelivers it). Never raises. [daveey]"""
    from hermes_cli import kanban_db_connect as _kbc
    from hermes_cli import kanban_db_notify as _kbn
    by_sub: dict = {}
    for claim in session.get("_kanban_claims") or []:
        if claim.get("remote"):
            texts = claim.get("texts") or []
            if texts and all(id(t) in durable_ids for t in texts):
                claim["durable"] = True
            if claim.get("durable") and not claim.get("acked"):
                try:
                    from tui_gateway import kanban_remote_notices as _krn
                    if _krn.ack(claim["remote"]):
                        claim["acked"] = True
                except Exception:
                    logger.warning(
                        "remote kanban notice ack failed (a restart redelivers; the row dedups)", exc_info=True)
            continue
        texts = claim.get("texts") or []
        if texts and all(id(t) in durable_ids for t in texts):
            claim["durable"] = True
        key = (claim["board"], claim["task_id"], claim["platform"], claim["chat_id"], claim.get("thread_id") or "")
        by_sub.setdefault(key, []).append(claim)
    for (board, task_id, platform, chat_id, thread_id), group in by_sub.items():
        group.sort(key=lambda c: int(c["old"]))
        run = []
        for claim in group:
            if not claim.get("durable") or claim.get("acked"):
                break
            run.append(claim)
        if not run:
            continue
        try:
            with contextlib.closing(_kbc.connect(board=board)) as conn:
                acked = _kbn.ack_notify_claim(conn, task_id=task_id, platform=platform, chat_id=chat_id,
                    thread_id=thread_id, upto=int(run[-1]["new"]), from_cursor=int(run[0]["old"]))
                if not acked and _kbn.notify_claim_in_flight(conn, task_id=task_id, platform=platform,
                                                           chat_id=chat_id, thread_id=thread_id):
                    continue
            for claim in run:
                claim["acked"] = True
        except Exception as exc:
            _notif_log_failure("kanban notice ack failed (a restart redelivers; the row dedups)", exc)


def _kb_persist_batch(session: dict, batch: list, diagnostic: bool) -> tuple:
    """Write this batch's kanban notices into the chat's transcript BEFORE its turn runs, so the claim can be
    acknowledged: at-least-once delivery (t_01a1835174955959). Returns ``(verdict, batch, kwargs)``:

    - ``"written"``: one ``kanban_notice`` row holds the (remaining) batch and is staged for the turn to adopt
      (no second row); ``kwargs`` carry its display kind/metadata for the submit.
    - ``"delivered"``: every notice is already in the transcript (a redelivery after a crash): no turn.
    - ``"busy"``: a turn lease / compaction holds the transcript now: retry on a later poll.
    - ``"unkeyed"``: nothing to persist here (remote-only notices, no session store/row): the turn runs as
      before and its own user row is the record.

    Notices whose event keys are already in the transcript are dropped from ``batch`` (dedup by event id)."""
    keys = _kb_text_keys(batch)
    if not keys:
        return "unkeyed", batch, {}
    from hermes_state_errors import CompressionSessionClosedError, KanbanNoticeOverlapError, SessionTurnLeaseLostError
    try:
        with _session_db(session) as db:
            if db is None or not hasattr(db, "record_kanban_notice"):
                return "unkeyed", batch, {}
            target = _submit_row_target_key(session)
            if not target or db.get_session(target) is None:
                return "unkeyed", batch, {}
            done = db.kanban_notice_keys_delivered(target, keys)
            from tui_gateway.kanban_claim_custody import batch_text, fresh_texts
            from tui_gateway.kanban_remote_late import missing_texts
            fresh = fresh_texts(missing_texts(batch, done), done)
            if not fresh:
                return "delivered", [], {}
            text = batch_text(fresh)  # [daveey] stale block asks re-read at delivery
            fresh_keys = _kb_text_keys(fresh)
            import hashlib
            metadata = {"notice_id": "live:" + hashlib.sha1(",".join(sorted(fresh_keys)).encode()).hexdigest()[:20],
                        "display_text": text, "kanban_event_keys": fresh_keys}
            if diagnostic:
                metadata["notification_category"] = "diagnostic"
            if any(getattr(t, "_kb_blocked", False) for t in fresh):
                metadata["blocked_on_human"] = True  # [daveey] a remote card: the guarantee can't look it up here
            from agent.context_compressor import _DB_PERSISTED_MARKER
            from agent.message_metadata import stamp_message_timestamp, stamp_message_uid
            staged = stamp_message_timestamp({"role": "user", "content": text, "display_kind": "kanban_notice",
                                              "display_metadata": metadata})
            stamp_message_uid(staged)
            row_id, inserted = db.record_kanban_notice(target, text, metadata, message=staged)
            if not inserted:
                return "busy", batch, {}  # raced another consumer for some of these keys: re-filter next poll
            from hermes_state_outcome import refresh_outcomes_quietly
            refresh_outcomes_quietly([target], "notice", db=db)  # [daveey] until its turn ends, the notice is the tail
    except (SessionTurnLeaseLostError, CompressionSessionClosedError, KanbanNoticeOverlapError):
        return "busy", batch, {}
    except Exception as exc:
        _notif_log_failure("kanban notice row write failed; the turn records it instead", exc)
        return "unkeyed", batch, {}
    staged["_row_id"] = row_id
    staged[_DB_PERSISTED_MARKER] = True
    staged[_SUBMIT_ROW_SESSION_KEY] = target
    session["_submit_user_row"] = staged
    return "written", fresh, {"display_kind": "kanban_notice", "display_metadata": metadata}


def _kb_release_claims(session: dict) -> None:
    from tui_gateway.kanban_claim_custody import release_claims
    release_claims(session, _notif_log_failure, logger)


def _kb_owner_refusal(sid: str, session: dict):
    """Probe, read-only, whether ANOTHER process owns this chat, BEFORE a kanban batch is written to its
    transcript: a non-owner must not write the notice row and ack the claim, or the owner never hears the event
    and no turn reacts to the row. Two owners count: a foreign durable turn lease (state.db) and, while this
    session holds no registry slot, a live registry entry for the chat from another pid. Claims nothing (the
    turn's admission still claims the slot); an unprovable answer is None and the admission decides. [daveey]"""
    import os

    from hermes_cli.active_sessions import SESSION_NOT_OWNED
    try:
        refusal = _durable_owner_refusal(session)
        if getattr(refusal, "reason", None) != SESSION_NOT_OWNED:
            refusal = None
        key = str(session.get("session_key") or "")
        if refusal is None and key and session.get("active_session_lease") is None:
            from hermes_cli.active_sessions import (ActiveSessionRefusal, active_session_registry_snapshot,
                                                    session_already_owned_message)
            for entry in active_session_registry_snapshot(session.get("profile_home")):
                if str(entry.get("session_id") or "") == key and int(entry.get("pid") or -1) != os.getpid():
                    refusal = ActiveSessionRefusal(session_already_owned_message(key, entry), SESSION_NOT_OWNED)
                    break
    except Exception as exc:
        _notif_log_failure("kanban notice ownership probe failed; the turn's admission decides", exc)
        return None
    if refusal is not None:
        _notif_note_owner_refusal(session, SESSION_NOT_OWNED)
    return refusal


def _notif_collect_kanban(session: dict, *, remote_only=False) -> tuple:
    claims: list = []
    texts = []
    if time.monotonic() >= float(session.get("_kanban_refused_until") or 0):
        try:
            texts = (_collect_kanban_notifications(session, claims, remote_only=True) if remote_only
                     else _collect_kanban_notifications(session, claims))
        except Exception as exc:
            from tui_gateway.notification_remote_retry import cancel
            cancel(session)
            if '_notif_remote_batch' in session:
                session['_notif_f_retry_at'] = time.monotonic() + 1.0
            logger.debug("Kanban collection failed", exc_info=True)
            _notif_log_failure("kanban notification poll failed", exc)
    return texts, claims


def _kb_gateway_owns(session: dict) -> bool:  # the gateway's collector admits this chat's tui rows [daveey]
    from tui_gateway.kanban_leg_custody import legacy_gateway_owns
    return legacy_gateway_owns(session, _session_lineage, _session_db)


def _kb_settle_held(session: dict) -> None:  # a buffered claim the chat's wake delivered meanwhile [daveey]
    from tui_gateway.kanban_leg_custody import legacy_settle_held
    legacy_settle_held(session, _session_lineage, _notif_log_failure)


def _notif_poll_kanban_scoped(sid: str, session: dict, prepared=None) -> None:
    """One kanban poll: emit new texts, buffer them, and run the buffered batch as a turn if idle. Events are
    cursor-claimed, so they wait in the buffer instead of dropping the agent turn; a turn the admission gate
    refuses hands its claims back (_kb_rewind_refused)."""
    if session.get("_finalized") or session.get("_closing") or session.get("_lease_taken_over"):
        # Closing or taken over: hand buffered claims back now, so the chat's next consumer (often another live
        # backend) is not blocked behind this process's in-flight marker until it exits. [daveey]
        if session.get("_kanban_claims") or session.get("_kanban_pending"):
            _kb_release_claims(session)
        return
    from tui_gateway.kanban_claim_custody import batch_text, release_expired
    if release_expired(session, time.monotonic()):
        return
    texts, claims = prepared if prepared is not None else _notif_collect_kanban(session)
    for text in texts:
        from gateway.warning_notifications import DiagnosticText, render_notification
        render_notification(lambda: _emit("status.update", sid, {"kind": "process", "text": text}),
                            platform="tui", diagnostic=isinstance(text, DiagnosticText))
    if texts and prepared is None:
        session.setdefault("_kanban_pending", []).extend(texts)
    if claims and prepared is None:  # prepared collections already transferred custody
        session.setdefault("_kanban_claims", []).extend(claims)
    _kb_ack_durable(session, set())  # claims-only retry; never re-admit the committed action
    session["_kanban_claims"] = [c for c in session.get("_kanban_claims", []) if not c.get("acked")]
    _kb_settle_held(session)
    if session.get("_kanban_pending") and _kb_gateway_owns(session):  # hand back now, not after a refused turn
        _kb_rewind_refused(session, list(session.get("_kanban_pending") or []), not_owned=True)
        return
    if not session.get("_kanban_pending") or not _notif_claim_turn(session):
        return
    with session["history_lock"]:
        pending = session.get("_kanban_pending") or []
        from gateway.warning_notifications import DiagnosticText, warning_notifications_enabled
        split = not warning_notifications_enabled("tui")
        diagnostic = split and isinstance(pending[0], DiagnosticText)
        batch = [text for text in pending if not split or isinstance(text, DiagnosticText) == diagnostic]
        session["_kanban_pending"] = [text for text in pending if split and isinstance(text, DiagnosticText) != diagnostic]
    # Only the chat's owner writes the row and acks; a non-owner hands the claims back for the owner's poller.
    if _kb_owner_refusal(sid, session) is not None:
        _notif_release_turn(session)
        _kb_rewind_refused(session, batch, not_owned=True)
        return
    # At-least-once (t_01a1835174955959): row written and claim acked BEFORE the turn; a dying process hands it back.
    verdict, fresh, submit_kwargs = _kb_persist_batch(session, batch, diagnostic)
    if verdict == "busy":
        with session["history_lock"]:
            session["_kanban_pending"] = batch + (session.get("_kanban_pending") or [])
        _notif_release_turn(session)
        return
    if verdict in ("written", "delivered"):
        _kb_ack_durable(session, {id(t) for t in batch})
    if verdict == "delivered":
        _kb_mark_delivered(session, batch)
        _notif_release_turn(session)
        return
    if verdict == "unkeyed":  # [daveey] still a kanban_notice row (typed: never mistaken for a person's reply)
        meta = {"display_text": batch_text(fresh)}
        if diagnostic:
            meta["notification_category"] = "diagnostic"
        submit_kwargs = {"display_kind": "kanban_notice", "display_metadata": meta}
    try:
        text = (submit_kwargs.get("display_metadata") or {}).get("display_text") or batch_text(fresh)  # = the row
        accepted = _notif_submit(f"__notif__{int(time.time() * 1000)}", sid, session, text,
                                 "kanban notification dispatch failed", **submit_kwargs)
    except Exception:
        accepted = False
    if accepted is False:
        _kb_rewind_refused(session, batch, not_owned=_notif_owner_refused(session))
    else:
        if verdict == "unkeyed":  # the accepted turn's own user row is the record
            _kb_ack_durable(session, {id(t) for t in batch})
        _kb_mark_delivered(session, batch)


def _notif_defer_event(evt, claim, put=None):
    from tools.async_delegation import defer_completion_delivery
    from tools.process_registry import process_registry
    if claim:
        defer_completion_delivery(evt['delegation_id'], claim)
    (put or process_registry.completion_queue.put)(evt)


def _background_notifications_off(session: dict) -> bool:
    """Whether the owning profile set ``display.background_process_notifications: off``. Same
    gate the messaging gateway applies to its process-event injection; only ``off`` matters
    here (the other modes shape gateway chat receipts, not agent wakes)."""
    with _session_profile_runtime_scope(session):
        raw = (_load_cfg().get("display") or {}).get("background_process_notifications")
    return raw is False or str(raw or "").strip().lower() == "off"


def _notif_dispatch_event(sid: str, session: dict, evt: dict, text: str) -> None:
    """Run the claimed (running=True) agent turn for one notification event."""
    from tools.async_delegation import claim_event_delivery, complete_event_delivery, release_event_delivery
    turn_claim = session.get("_notif_turn_claim")
    try:
        claim = claim_event_delivery(evt, "tui-poller")
    except Exception as exc:  # shared ledger busy/unreadable: the durable row stays pending and replays
        _notif_log_failure("notification delivery claim failed", exc)
        claim = None
    if claim is None:
        # Another consumer holds the durable row — a gateway sharing this home claims before it verifies
        # the target. No turn will run, and nothing else clears ``running``: a busy session is exempt
        # from the reaper, keeps its lease, and never reaches its bot mailbox again.
        _notif_release_claim(session, turn_claim)
        return
    evt_type = evt.get("type")
    kwargs: dict = {}
    if evt_type == "async_delegation":
        kwargs = {"display_kind": "async_delegation_complete", "display_metadata": _async_delegation_display_metadata(evt)}
    elif evt_type == "heartbeat":
        # Model-facing scaffolding: the process row on the status stack already says it is running,
        # so the wake never paints as a user bubble (Desktop, TUI and the transcript preview all
        # honour ``hidden``). Only what the agent says about the new output is visible.
        from tools.process_registry_notifications import HEARTBEAT_DISPLAY_KIND
        kwargs = {"display_kind": HEARTBEAT_DISPLAY_KIND}
    from agent.notification_presentation import diagnostic_process_event
    if diagnostic_process_event(evt):
        kwargs.setdefault("display_metadata", {})["notification_category"] = "diagnostic"
    try:
        accepted = _notif_submit(f"__notif__{int(time.time() * 1000)}", sid, session, text,
                                 "notification poller dispatch failed", **kwargs)
    except Exception:
        release_event_delivery(evt, claim)
        return
    if not accepted:
        # Refused (e.g. another backend owns the chat): the event waits instead of being marked delivered
        # while no turn ran (a subagent result used to be dropped this way). [daveey] It goes to this
        # session's own poller (_notif_hold_for) when one runs, not the shared queue every poller re-reads.
        from tools.process_registry import process_registry
        q = process_registry.completion_queue
        _notif_defer_event(evt, claim, lambda e: _notif_hold_for(session, e, q) or q.put(e))
        _notif_refusal_backoff(session)  # requeued: queue.get(timeout=...) would never wait
        return
    complete_event_delivery(evt, claim)


def _notif_hold_for(owner: dict, evt: dict, queue) -> bool:
    """Hand ``evt`` to ``owner``'s poller (its ``_notif_held`` list) instead of the shared completion queue; False if
    ``owner`` has no running poller (none started, or its shutdown drain already took the list). Every live
    session's poller drains the one process-wide queue, so an event put back there while its owner was mid-turn
    was dequeued, lineage-resolved against state.db and re-queued by every other poller, ten times a second
    each, for the whole turn (a 22-chat Desktop backend at 300% CPU, 2026-10-27). [daveey]

    The list is off every queue, so a delegation result in it is marked held (``async_delegation.mark_copy_held``)
    while the list owns it; else the orphan sweep reads a result waiting out a turn longer than a claim lease as
    lost and queues a duplicate. The caller still holds its own mark (in-flight or park) here, so the copy is never
    unmarked; whoever takes it back out (``_notif_take_held``, the shutdown drain) unmarks after routing."""
    from tools.async_delegation import completion_copy_id, mark_copy_held
    with _notif_held_lock:
        held = owner.get("_notif_held")
        if held is None:
            return False
        mark_copy_held(completion_copy_id(evt))
        held.append(evt)
    from tui_gateway.notification_wait import wake
    if owner.get("_notif_poller_ident") != _notif_poller_identity():
        wake(queue)  # a self re-hold must still wait (admission/backoff may refuse while not running)
    return True


def _notif_poller_identity():
    """The running poller: its f ON executor lane (all lanes share one OS thread), else this thread's ident."""
    from tui_gateway.notification_loaded_executor import current_identity
    return current_identity()


def _notif_take_held(session: dict) -> list:
    """Remove and return every event held for ``session`` (oldest first), in in-flight custody (taken in the same
    critical section). Each result's held-copy mark passes to the caller too: once routing returns the caller MUST
    ``process_registry_awaiting.release`` the list, then pass it to ``_notif_unhold`` (a re-hold takes its own mark)."""
    from tools.process_registry_awaiting import hold
    held = session.get("_notif_held")
    if not held:
        return []
    with _notif_held_lock:
        taken = list(held)
        held.clear()
        hold(taken)
    return taken


def _notif_unhold(events) -> None:
    """End the held-copy mark ``_notif_hold_for`` took for each of ``events`` (no-op for non-results)."""
    if events:
        from tools.async_delegation import completion_copy_id, unmark_copy_held
        for evt in events:
            unmark_copy_held(completion_copy_id(evt))


def _notif_handle_event(sid, session, evt, emitted, registry, fmt, deferred, completions=None, *, owned=False) -> bool:
    """Route one dequeued event: foreign (another live session owns it) → handed to its owner's poller
    (``_notif_hold_for``; the shared queue only if it has none), or onto ``deferred`` during the shutdown drain;
    unowned (addressed but unprovable — never adopt an orphan) → a re-adopted orphan's ``park_if_unowned`` exit is
    parked on the registry for its chat's poller, anything else parks for a bounded window so the owner's resume can
    still claim it (``_notif_unowned_event``); ours (or ownerless legacy, kept process-global) → status.update once,
    then an agent turn if idle, else held for this session's idle boundary (ownerless legacy: re-queued). False = the
    drain must stop (session busy). ``owned`` skips the ownership gates for events a caller already drained through
    ``_session_owns_notification_event`` (the post-turn safety net), so lineage resolves once."""
    queue = registry.completion_queue
    evt_type, is_delegation = evt.get("type", "completion"), evt.get("type") == "async_delegation"
    from tools.environments.sandbox_scope import event_visible
    if not event_visible(evt):  # another hosted agent's event: it stays queued for that agent
        if deferred is not None:
            deferred.append(evt)
        else:
            queue.put(evt)
            time.sleep(0.1)
        return True
    owner = None if owned else _notification_event_owner_elsewhere(sid, session, evt)
    if owner is not None:
        if deferred is not None:
            deferred.append(evt)
        elif not _notif_hold_for(owner, evt, queue):
            queue.put(evt)  # owner has no poller to hold it: the shared queue is its only route
            time.sleep(0.1)
        return True
    if not owned and _notification_event_requires_owner(evt) and not _session_owns_notification_event(sid, session, evt):
        if evt.get("park_if_unowned") and hasattr(registry, "park_unowned_event"):
            # A re-adopted orphan's exit: its chat may simply not be open in this backend yet. Park it
            # for that chat's own poller (take_parked_events) instead of dropping it. [daveey]
            registry.park_unowned_event(evt)
            return True
        _notif_unowned_event(sid, evt, deferred)
        return True
    if evt_type == "completion" and registry.is_completion_consumed(evt.get("session_id", "")):
        return True
    text = fmt(evt)
    if not text:
        return _settle_suppressed(evt, "no notice text")
    # Emit once per dedup key: a re-queued completion would otherwise re-emit every 0.5s while the session is busy,
    # while distinct watch_match events from one process must stay visible.
    dedup_key = _notification_event_dedup_key(evt)
    if dedup_key not in emitted:
        from tools.process_registry_notifications import async_delegation_display_text, process_completion_display_text
        display_text = (async_delegation_display_text(evt) if is_delegation
                        else process_completion_display_text([evt]) if evt_type == "completion" else text)
        from agent.notification_presentation import diagnostic_process_event
        from gateway.warning_notifications import render_notification
        render_notification(lambda: _emit("status.update", sid, {"kind": "process", "text": display_text}),
                            platform="tui", diagnostic=diagnostic_process_event(evt))
        emitted.add(dedup_key)
    if evt_type != "async_delegation" and _background_notifications_off(session):
        # The user opted out of process-driven agent wakes: the status row above is the whole
        # delivery. Subagent results are not process notifications and still land.
        return _settle_suppressed(evt, "background notifications off")
    if evt_type == "completion" and completions is not None:
        completions.append((evt, text))
        return True
    if not _notif_claim_turn(session):
        if deferred is not None:
            queue.put(evt)
            return False
        if _notification_event_requires_owner(evt) and _notif_hold_for(session, evt, queue):
            return True  # ours: wait in this session's held list until it is idle, off the shared queue
        queue.put(evt)
        time.sleep(0.25)  # back off: the re-queued event keeps the queue non-empty, else this loop spins at 100% CPU
        return True
    _notif_dispatch_event(sid, session, evt, text)
    return True


def _notif_undeliver(session, claimed) -> None:
    """[daveey] Completions going back to custody were not delivered, whatever admission marked."""
    with session["history_lock"]:
        session.get("_completion_delivered", set()).difference_update(e.get("session_id") for e, _t, _c in claimed)


def _notif_retry_completions(claimed, registry, deferred):
    """A delegation claim release cannot restore a dequeued native completion."""
    from tools.async_delegation import release_event_delivery
    for event, _text, claim in claimed:
        release_event_delivery(event, claim)
        if event.get("type", "completion") == "completion":
            (deferred.append if deferred is not None else registry.completion_queue.put)(event)
    if deferred is None and claimed:
        time.sleep(0.25)


def _notif_dispatch_completions(sid, session, notifications, registry, deferred):
    from tools.process_registry_notifications import PROCESS_COMPLETE_DISPLAY_KIND, ProcessNotificationBatch
    from tools.async_delegation import claim_event_delivery, complete_event_delivery

    if not notifications:
        return
    if not _notif_claim_turn(session):
        requeued = False
        for event, _text in notifications:
            if deferred is not None:
                deferred.append(event)
            elif not (_notification_event_requires_owner(event)
                      and _notif_hold_for(session, event, registry.completion_queue)):
                registry.completion_queue.put(event)
                requeued = True
        if requeued:
            time.sleep(0.25)
        return
    from tui_gateway.process_consumer_health import begin_delivery
    delivery_token = begin_delivery(session)
    claimed: list = []
    try:
        for event, event_text in notifications:
            if (claim := claim_event_delivery(event, "tui-completion-batch")) is not None:
                claimed.append((event, event_text, claim))
        batch = ProcessNotificationBatch(tuple((event, event_text) for event, event_text, _claim in claimed))
        text = batch.render(registry)
    except Exception as exc:
        _notif_log_failure("completion batch preparation failed", exc)
        _notif_release_turn(session)
        _notif_retry_completions(claimed, registry, deferred)
        return
    if text is None:
        _notif_release_turn(session)
    try:
        if text is not None:
            # [daveey] The turn's start marks these delivered atomically with the exit fence check
            # (start_synthesized_turn; exit_job_receipts: a fenced start is refused instead).
            from tui_gateway.session_notifications import NOTIF_OFFERED_COMPLETIONS
            session[NOTIF_OFFERED_COMPLETIONS] = {e.get("session_id"): e for e, _t, _c in claimed
                                                  if e.get("type", "completion") == "completion"
                                                  and e.get("session_id")}
            try:
                accepted = _notif_submit(f"__notif__{int(time.time() * 1000)}", sid, session, text,
                                         "completion batch dispatch failed", display_kind=PROCESS_COMPLETE_DISPLAY_KIND,
                                         display_metadata={"display_text": batch.display_text(registry)})
            finally:
                session.pop(NOTIF_OFFERED_COMPLETIONS, None)
            from tui_gateway.process_consumer_health import delivered
            delivered(sid, session, registry, [event for event, _, _ in claimed], accepted, delivery_token)
            if not accepted:
                from tui_gateway.session_notifications import _notif_undeliver
                _notif_undeliver(session, claimed)
                for event, _text, claim in claimed:
                    _notif_defer_event(event, claim,
                        deferred.append if deferred is not None else registry.completion_queue.put)
                if deferred is None:  # requeued onto the live queue (a shutdown drain hands them back)
                    _notif_refusal_backoff(session)
                return
    except Exception:
        from tui_gateway.session_notifications import _notif_undeliver
        _notif_undeliver(session, claimed)
        _notif_retry_completions(claimed, registry, deferred)
        return
    for event, _text, claim in claimed:
        complete_event_delivery(event, claim)
        if event.get("type", "completion") == "completion":
            with session["history_lock"]:
                session.setdefault("_completion_delivered", set()).add(event.get("session_id"))


def _notif_handle_ready(sid, session, events, emitted, registry, fmt, deferred, *, owned=False):
    """One ready snapshot: ownership and UI emission per event, one turn per completion run. Events this pass
    does not route (busy session, or an event whose routing raised) go back to ``deferred`` / the queue."""
    completions = []
    hand_back = deferred.append if deferred is not None else registry.completion_queue.put
    for index, event in enumerate(events):
        if event.get("type", "completion") != "completion":
            _notif_dispatch_completions(sid, session, completions, registry, deferred)
            completions = []
        try:
            routed = _notif_handle_event(sid, session, event, emitted, registry, fmt, deferred, completions,
                                         owned=owned)
        except Exception:
            # The snapshot is the only in-memory copy of everything after this event: hand it back, and the
            # failed delegation's offer to the ledger, before the caller logs the failure.
            for remaining in events[index + 1:]:
                hand_back(remaining)
            _notif_dispatch_completions(sid, session, completions, registry, deferred)
            from tools.async_delegation import return_completion_offer
            return_completion_offer(event)
            raise
        if not routed:
            for remaining in events[index + 1:]:
                hand_back(remaining)
            break
    _notif_dispatch_completions(sid, session, completions, registry, deferred)


_PARKED_POLL_SECONDS = 2.0


def _notif_take_parked(sid, session, registry, handle) -> int:
    """Deliver the parked events (re-adopted orphans' exits whose chat was not open) this session owns. [daveey]"""
    take = getattr(registry, "take_parked_events", None)
    if take is None:
        return 0
    if not getattr(registry, "has_parked_events", lambda: True)():
        return 0
    try:
        events = take(lambda evt: _session_owns_notification_event(sid, session, evt))
        if events:
            from tools.process_registry_awaiting import release
            try:
                handle(events, None)
            finally:
                release(events)
        return len(events)
    except Exception as exc:
        _notif_log_failure("parked notification dispatch failed", exc)
        return 0


def _readopt_wake_profiles() -> list:
    """Launch store first, then each named profile with a state.db (a checkpoint entry records no profile)."""
    from hermes_constants import get_hermes_home
    out: list = [None]
    with contextlib.suppress(Exception):
        root = Path(get_hermes_home()) / "profiles"
        out += sorted(p.name for p in root.iterdir() if (p / "state.db").is_file())
    return out


def _wake_session_turn(session_key: str, text: str, *, display_kind: str | None = None,
                       display_metadata: dict | None = None, label: str = "wake", build_timeout: float = 120.0,
                       profiles: list | None = None, replay_delivery=None) -> str:
    """Run ONE agent turn with ``text`` in chat ``session_key`` from this backend: the chat live here is used as is,
    otherwise it is resumed in-process first (``session.resume``, launch store then each named profile, the same
    path the re-adopted-exit waker uses). For plugins that must close a loop without the user typing (e.g. a chat
    whose kanban cards all finished while no backend held it). [daveey]

    Returns ``"started"`` (the turn was admitted and runs on its own thread), ``"busy"`` (a turn is live, the user
    stopped the last one, or this backend is retiring), ``"refused"`` (another backend owns the chat, or resume was
    refused), ``"missing"`` (no store has it) or ``"error"``. Blocks <= ``build_timeout`` for the build; never raises."""
    import itertools as _it

    # [daveey] Moved to automation_replay; no wake on stale code (t_01a1d65520e78e7d).
    from tui_gateway.automation_replay import replay_failed_wake as _replay_failed_wake, retiring_backend_refuses
    key = str(session_key or "")
    if not key or not text:
        return "error"
    if retiring_backend_refuses(label, key):
        return "busy"
    try:
        from tui_gateway.wake_profiles import live_for_profiles, compression_live
        live = live_for_profiles(key, profiles)
        if live is None:
            # [daveey] A paused turn is its restarting backend's: here its viewers are not attached (update_pause).
            from tui_gateway.update_pause import left_to_successor
            # [daveey] ``profiles``: a handed-off replay resumes in its exact profile, never a first-match scan.
            wake_profiles = profiles if profiles is not None else _readopt_wake_profiles()
            if left_to_successor(_hermes_home, wake_profiles, key, label):
                return "busy"
            handler = _methods.get("session.resume")
            if handler is None:
                return "error"
            resumed = False
            for n, profile in zip(_it.count(1), wake_profiles):
                params = {"session_id": key, "omit_messages": True, **({"profile": profile} if profile else {})}
                env = handler(f"{label}-resume-{n}", params) or {}
                err = env.get("error") if isinstance(env, dict) else None
                if not err:
                    resumed = True
                    break
                if not (isinstance(err, dict) and err.get("code") == 4007):
                    logger.info("%s: resume of %s refused (%s)", label, key, err)
                    return "refused"
            if not resumed:
                return "missing"
            live = live_for_profiles(key, profiles)
            if live is None:
                with contextlib.suppress(Exception):
                    live = compression_live(key, profiles)
            if live is None:
                logger.warning("%s: %s resumed but no live session holds it", label, key)
                return "error"
        sid, session = live
        rid = f"__{label}__{int(time.time() * 1000)}"
        # [daveey] A build that already failed on this record is retried, as prompt.submit retries it: otherwise
        # every later wake (and the automation replay) re-read the same stale agent_error without building.
        with contextlib.suppress(Exception):
            _restart_completed_failed_agent_build(sid, session, session.get("agent_ready"))
        _start_agent_build(sid, session)  # idempotent; a resumed chat builds lazily
        if _wait_agent(session, rid, timeout=build_timeout):
            logger.warning("%s: agent build failed for %s", label, key)
            _replay_failed_wake(key, text, session, display_kind, display_metadata, label)
            return "error"
        # The shared admission gate (retirement + history_lock), like every notice turn: no double admission
        # against a user prompt or another waker, and nothing starts after the user's Stop.
        if session.get("_finalized") or not _notif_claim_turn(session):
            return "busy"
        session["last_active"] = time.time()
        if _ensure_active_session_slot(sid, session) is not None:
            _notif_release_turn(session)
            from tui_gateway.gateway_background_holder import wake_past_retiring_owner
            return wake_past_retiring_owner(sid, session, label, key, foreign_owner=_foreign_turn_owner)
        kwargs = {k: v for k, v in (("display_kind", display_kind), ("display_metadata", display_metadata)) if v}
        if replay_delivery is not None:
            accepted = replay_delivery.submit(rid, sid, session, text, display_kind, display_metadata, label)
        else:
            accepted = _notif_submit(rid, sid, session, text, f"{label} dispatch failed", **kwargs)
        if accepted is False:
            return "refused"
        logger.info("%s: started a turn in chat %s (live sid %s)", label, key, sid)
        return "started"
    except Exception:
        logger.warning("%s: waking %s failed", label, key, exc_info=True)
        return "error"


def _notification_poller_loop(stop_event: threading.Event, sid: str, session: dict) -> None:
    from tools.process_registry import process_registry
    _notification_poller_drive(_notification_poller_lane_steps(stop_event, sid, session),
                               process_registry.completion_queue)


def _notification_poller_lane_steps(stop_event: threading.Event, sid: str, session: dict):
    """The whole poller lifetime as a generator (see session_notifications_loop): the legacy thread and the
    f ON shared executor both run it, the latter in the lane's own context and thread identity."""
    from tools.process_registry import process_registry
    from tui_gateway import process_consumer_health
    with _session_profile_runtime_scope(session):
        process_consumer_health.register(sid, session, stop_event, process_registry)
        try:
            yield from _notification_poller_scoped_steps(stop_event, sid, session)
        finally:
            process_consumer_health.unregister()


def _notification_poller_scoped_loop(stop_event: threading.Event, sid: str, session: dict) -> None:
    from tools.process_registry import process_registry
    _notification_poller_drive(_notification_poller_scoped_steps(stop_event, sid, session),
                               process_registry.completion_queue)


def _notification_poller_scoped_steps(stop_event: threading.Event, sid: str, session: dict):
    """Daemon thread (started by _init_session()) that drains the process-global completion_queue for this session
    (ownership routing: _notif_handle_event), claims the events parked while their owner was not live
    (``notification_park``), and polls ``kanban_notify_subs`` every ``_KANBAN_POLL_SECONDS`` for this session's TUI
    subscriptions, delivering terminal task events (status.update + agent turn): the delivery path for
    platform="tui" rows that tools/kanban_tools.py documents (issue #59890). Each sub-poll is a
    ``notifications.<poll>`` pass (loop_accounting).
    """
    from tools.process_registry import process_registry
    from tools.process_registry_notifications import format_process_notification
    process_registry.restore_completions()  # first consumer in a TUI process (#123265)
    emitted = session.setdefault("_notification_emitted", set())
    with _notif_held_lock:
        session["_notif_poller_ident"] = _notif_poller_identity()
        session["_notif_held"] = session.get("_notif_held") or []
    handle = lambda events, deferred: _notif_handle_ready(
        sid, session, events, emitted, process_registry, format_process_notification, deferred)
    try:
        yield from _notification_poller_run_steps(stop_event, sid, session, process_registry, handle)
    finally:
        from tui_gateway.notification_remote_retry import cancel
        cancel(session)
        _notification_poller_cleanup(sid, session, process_registry, handle)


def _notification_poller_cleanup(sid, session, process_registry, handle):
    from tools.process_registry_awaiting import release as release_in_flight, take_ready
    queue = process_registry.completion_queue
    # Kanban claims still buffered go back to the board: the process may live on (close, evict). [daveey]
    try:
        _kb_release_claims(session)
    except Exception as exc:
        logger.debug("poller claim release failed", exc_info=True)
        _notif_log_failure("kanban notice release on poller stop failed", exc)
    # Drain remaining events after the stop signal so nothing is lost on shutdown: events held for this session are
    # routed with the queued ones; unowned ones park for their owner's return; foreign ones (and delegations the park
    # refused) go back to the shared queue afterwards, even when routing one of them raised (#140718).
    deferred: list = []
    from tools.process_registry_awaiting import hold as hold_in_flight
    with _notif_held_lock:  # from here on a handoff to this session falls back to the shared queue
        held = list(session.pop("_notif_held", None) or ())
        hold_in_flight(held)  # [daveey] in custody until handed back (backend-exit journal reads it)
    ready = []
    try:
        ready = take_ready(queue, 0)
        handle(held + list(ready), deferred)
    finally:
        for evt in deferred:
            queue.put(evt)
        release_in_flight(ready)  # after the requeue above, so custody is continuous
        release_in_flight(held)
        _notif_unhold(held)


def _async_delegation_display_metadata(evt: dict) -> dict:
    """Build display-only metadata before the completion event is formatted."""
    from tools.process_registry_notifications import async_delegation_display_text
    raw_results = evt.get("results")
    results: list[dict] = [r for r in raw_results if isinstance(r, dict)] if isinstance(raw_results, list) else []
    task_count = len(results) or 1
    completed_count = sum(1 for r in results if r.get("status") in {"completed", "success"})
    failed_count = sum(1 for r in results if r.get("status") in {"failed", "error"})
    duration = evt.get("total_duration_seconds") or evt.get("duration_seconds")
    return {"display_text": async_delegation_display_text(evt),
            "delegation_id": str(evt.get("delegation_id") or ""), "task_count": task_count,
            "completed_count": completed_count or task_count - failed_count, "failed_count": failed_count,
            **({"duration_seconds": duration} if isinstance(duration, (int, float)) else {})}


_desktop_ui_wired = False


def _wire_desktop_sinks() -> None:
    """Idempotently wire process-registry and desktop-tool sinks to renderer events: `agent.terminal.output` and
    `terminal.close` (drops a tab without killing the process) route to the window owning the process; desktop-only
    tools pass the turn's ``HERMES_UI_SESSION_ID`` as ``sid``. `_emit` is thread-safe."""
    global _desktop_ui_wired
    from tools.process_registry import process_registry

    def _owner_sid(session) -> str:
        # session may be None (process already finished/pruned) — the tab can still linger and be closed.
        session_key = str(getattr(session, "session_key", "") or "") if session is not None else ""
        if not session_key:
            return ""
        with _sessions_lock:
            return next((sid for sid, s in _sessions.items() if str(s.get("session_key") or "") == session_key), "")
    if getattr(process_registry, "on_output", None) is None:
        process_registry.on_output = lambda session, chunk: _emit(
            "agent.terminal.output", _owner_sid(session), {"process_id": session.id, "chunk": chunk})
    if getattr(process_registry, "on_close", None) is None:
        process_registry.on_close = lambda session, pid: _emit("terminal.close", _owner_sid(session), {"process_id": pid})
    if not _desktop_ui_wired:
        with contextlib.suppress(Exception):
            from tools import desktop_ui
            desktop_ui.set_emitter(lambda sid, event, payload: _emit(event, sid, payload))
            _desktop_ui_wired = True


# (stop_event, thread) for every poller started in this process, pruned of dead threads on each spawn. Test teardowns
# reap leaked pollers through it: an unjoined poller steals events off the process-global queue in a LATER test.
_notification_pollers: list = []

# Guards every session's ``_notif_held`` list (events handed to that session's poller, _notif_hold_for).
_notif_held_lock = threading.Lock()


def _start_notification_poller(sid: str, session: dict) -> threading.Event:
    """Start the background notification poller for a TUI session (its name is greppable).

    f ON (``display.loaded_notification_f``, opt-in) admits the session as a lane of the shared cooperative
    executor (``notification_loaded_executor``): no per-chat thread. f OFF keeps the legacy per-chat thread."""
    _wire_desktop_sinks()
    from tools.process_registry import process_registry
    from tui_gateway.notification_wait import PollerStop
    from tui_gateway.kanban_consumer_health import register
    stop = PollerStop(process_registry.completion_queue)
    if _notification_loaded_f_on():
        from tui_gateway import notification_loaded_executor
        lane = notification_loaded_executor.Lane(sid, stop)
        _notification_pollers[:] = [(s, th) for (s, th) in _notification_pollers if th.is_alive()] + [(stop, lane)]
        register(session, stop, lane)
        notification_loaded_executor.submit(process_registry.completion_queue, lane,
                                            lambda: _notification_poller_lane_steps(stop, sid, session))
        return stop
    t = threading.Thread(target=_notification_poller_loop, args=(stop, sid, session), daemon=True, name=f"tui-notif-poller-{sid}")
    _notification_pollers[:] = [(s, th) for (s, th) in _notification_pollers if th.is_alive()] + [(stop, t)]
    register(session, stop, t)
    t.start()
    return stop


def _notification_loaded_f_on() -> bool:
    from tui_gateway import notification_loaded_f
    try:
        return notification_loaded_f.enabled()
    except Exception:  # health: allow BLE001 -- an unreadable config keeps the legacy per-chat poller
        logger.debug("loaded_notification_f config read failed; legacy poller", exc_info=True)
        return False


def register(server) -> None:
    """Publish this module's helpers + handlers onto ``server``, rebound to its globals."""
    bind_module(globals(), server, skip=("_",))
    _surface.register(server)
    _readopt.register(server)
    from . import session_notifications_kanban, session_notifications_loop
    session_notifications_kanban.register(server)
    session_notifications_loop.register(server)
