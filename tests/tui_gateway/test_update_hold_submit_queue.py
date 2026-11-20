"""An idle prompt.submit refused only by the core-update admission hold is queued, never bounced.

The update drainer holds admission for a bounded drain window while in-flight turns finish (hermes-sync
``drain_window``, t_01a181e61e88e5d4). Desktop has no handling for 5035, so a refusal there showed the
user an error for the message they had just sent. The message now joins the durable queue (persisted at
accept) and the admission retry runs it once the hold lifts. A retiring backend keeps its 5035.
"""
import json
import os
import sqlite3
import threading
import types

import pytest

from hermes_cli import admission_fence as af, backend_retirement
from tui_gateway import admission_retry, server

pytestmark = pytest.mark.platforms("posix")
# The server-bound queue dispatch, captured before the fixture replaces it with a recorder.
_REAL_DISPATCH_CLAIMED_QUEUE = server._dispatch_claimed_queue


@pytest.fixture
def held(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    fence = backend_retirement.RetirementFence()
    monkeypatch.setattr(backend_retirement, "retirement", fence)
    timers = []

    class Timer:
        def __init__(self, delay, target):
            self.target = target
            self.daemon = True

        def start(self):
            timers.append(self)

    monkeypatch.setattr(admission_retry.threading, "Timer", Timer)
    persisted = []
    monkeypatch.setattr(server, "_persist_queued_user_row",
                        lambda session, envelope, kind: persisted.append((envelope["text"], kind)))
    dispatched = []

    def dispatch(rid, sid, record, envelope, generation):
        dispatched.append(envelope["text"])
        record["running"] = False
        server._clear_inflight_turn(record)
        return True

    monkeypatch.setattr(server, "_dispatch_claimed_queue", dispatch)
    session = {
        "agent": types.SimpleNamespace(valid_tool_names=set()),
        "session_key": "session-key", "history": [], "history_lock": threading.RLock(),
        "history_version": 0, "running": False, "transport": None, "attached_images": [],
        "profile_home": str(tmp_path),
    }
    server._sessions["sid"] = session
    af.set_hold(tmp_path)
    yield types.SimpleNamespace(home=tmp_path, fence=fence, timers=timers, persisted=persisted,
                                dispatched=dispatched, session=session)
    server._sessions.pop("sid", None)
    af.clear_hold(tmp_path)


def _submit(**params):
    return server._methods["prompt.submit"]("r1", {"session_id": "sid", "text": "hello there", **params})


def test_idle_submit_under_the_update_hold_is_queued_and_runs_after_it(held):
    resp = _submit()
    assert "error" not in resp, resp
    assert resp["result"]["status"] == "queued"
    assert held.session["queued_prompt"]["text"] == "hello there"
    assert held.session["running"] is False
    assert held.persisted == [("hello there", None)], "durable at accept: an update restart cannot lose it"
    assert len(held.timers) == 1
    assert af.state(held.home)["reservations"] == 0, "queued, not admitted"
    # Still held: the retry re-arms without dispatching.
    held.timers.pop().target()
    assert not held.dispatched and len(held.timers) == 1
    af.clear_hold(held.home)
    held.timers.pop().target()
    assert held.dispatched == ["hello there"]
    assert not held.session.get("queued_prompt")


def test_second_send_under_the_hold_merges_into_the_same_queue(held):
    _submit()
    resp = _submit(text="and another")
    assert resp["result"]["status"] == "queued"
    assert held.session["queued_prompt"]["text"] == "hello there\n\nand another"
    assert len(held.timers) == 1, "one retry timer per session"


def test_retiring_backend_keeps_its_5035(held):
    token = held.fence.prepare()["token"]
    try:
        resp = _submit()
    finally:
        held.fence.cancel(token)
    assert resp["error"]["code"] == 5035
    assert not held.session.get("queued_prompt")
    assert not held.timers


def test_a_rewind_under_the_hold_is_refused_not_queued(held):
    resp = _submit(truncate_before_user_ordinal=0, confirm_truncate=True)
    assert resp["error"]["code"] == 5035
    assert not held.session.get("queued_prompt")


def test_without_a_hold_the_submit_is_admitted_normally(held, monkeypatch):
    af.clear_hold(held.home)
    ran = []
    monkeypatch.setattr(server, "_dispatch_admitted_prompt", lambda rid, *a: ran.append(a) or server._ok(rid, {"status": "streaming"}))
    resp = _submit()
    assert resp["result"]["status"] == "streaming"
    assert ran and not held.session.get("queued_prompt")


def test_hold_reply_validates_as_prompt_submit_result(held):
    """The reply is a wire reply: an extra field is rejected by the generated contract (t_01a186190e59f122)."""
    from tui_gateway.contracts.prompt_voice import PromptSubmitResult
    resp = _submit()
    assert set(resp["result"]) == {"status", "queued_reason", "queued_started_at"}
    parsed = PromptSubmitResult.model_validate(resp["result"])  # extra=forbid: every key is declared
    assert parsed.status == "queued" and parsed.queued_reason == "core_update"


def _age_marker(home, key, seconds):
    """Backdate the journaled marker, as if the prompt was queued ``seconds`` before the resume."""
    from tui_gateway import turn_marker
    path = turn_marker._marker_path(home)
    entries = json.loads(path.read_text())
    entries[key]["started_at"] -= seconds
    path.write_text(json.dumps(entries))


def _restart(held, monkeypatch, run_prompt_submit):
    """The update restarts the backend while the hold is up: the in-memory queue, its timer and the old
    process are gone; only the durable state (turn marker, state.db) survives. Returns the reborn session."""
    held.timers.clear()
    server._sessions.pop("sid", None)
    af.clear_hold(held.home)
    monkeypatch.setattr(server, "marker_writer_state", lambda entry: "dead")
    reborn = {
        "agent": types.SimpleNamespace(valid_tool_names=set()), "session_key": "session-key", "history": [],
        "history_lock": threading.RLock(), "history_version": 0, "running": False, "transport": None,
        "attached_images": [], "profile_home": str(held.home),
    }
    server._sessions["sid"] = reborn
    monkeypatch.setattr(server, "_start_session_work", lambda target, **kw: target() or True)
    monkeypatch.setattr(server, "_start_agent_build", lambda *a, **k: None)
    monkeypatch.setattr(server, "_wait_agent", lambda *a, **k: None)
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *a, **k: None)
    monkeypatch.setattr(server, "_adopt_stored_continuation_source", lambda *a, **k: None)
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(server, "_run_prompt_submit", run_prompt_submit)
    return reborn


def test_queued_hold_prompt_is_dispatched_after_the_update_restart(held, monkeypatch):
    from tui_gateway.turn_marker import read_turn_marker
    _submit()
    marker = read_turn_marker(held.home, "session-key")
    assert marker and marker["reason"] == "update_hold_queue" and marker["prompt"] == "hello there"
    ran = []
    reborn = _restart(held, monkeypatch, lambda rid, sid, session, text, **kw: ran.append((text, kw.get("display_kind"))))
    assert server._maybe_schedule_auto_continue("sid", reborn, "session-key") is not None
    assert ran == [("hello there", None)], "the user's own text, verbatim, not an auto-continue note"


def test_hold_prompt_older_than_the_turn_freshness_window_still_replays(held, monkeypatch):
    """The drain window holds admission up to 15 min and the apply takes 1-4 min more, so a prompt queued
    early in the window is past the 15 min turn freshness window by the post-restart resume."""
    _submit()
    _age_marker(held.home, "session-key", 20 * 60)
    ran = []
    reborn = _restart(held, monkeypatch, lambda rid, sid, session, text, **kw: ran.append(text))
    assert server._maybe_schedule_auto_continue("sid", reborn, "session-key") is not None
    assert ran == ["hello there"]


def test_hold_prompt_past_its_own_bound_is_dropped_and_an_ordinary_turn_keeps_15_min(held, monkeypatch):
    from tui_gateway.turn_marker import UPDATE_HOLD_QUEUE_MAX_AGE_S, read_turn_marker, record_turn_start
    _submit()
    _age_marker(held.home, "session-key", UPDATE_HOLD_QUEUE_MAX_AGE_S + 60)
    ran = []
    reborn = _restart(held, monkeypatch, lambda rid, sid, session, text, **kw: ran.append(text))
    assert server._maybe_schedule_auto_continue("sid", reborn, "session-key") is None
    assert ran == [] and read_turn_marker(held.home, "session-key") is None
    # Control: the wider bound is only for hold-queued prompts; a crashed turn at 20 min stays stale.
    record_turn_start(held.home, "session-key", "a crashed turn")
    _age_marker(held.home, "session-key", 20 * 60)
    reborn.pop("_auto_continue_scheduled", None)
    assert server._maybe_schedule_auto_continue("sid", reborn, "session-key") is None
    assert ran == []


def test_startup_sweep_retries_hold_prompts_past_the_turn_freshness_window(held):
    from tui_gateway import startup_resume
    _submit()
    _age_marker(held.home, "session-key", 20 * 60)
    cfg = {"desktop": {"auto_continue": {"resume_on_startup": True}}}
    rows = startup_resume.pending_resumes(held.home, cfg, log_skips=False)
    # The writer is this (live) test process, so the boot sweep holds it back until the old writer is gone,
    # but keeps retrying: the hold-queue reason is in the update-recovery retry set and within its age bound.
    assert rows == [] and startup_resume._paused_markers_remain(held.home, cfg)


def _desktop_lease_replay(db, admitted):
    """A ``_run_prompt_submit`` stand-in that takes the real Desktop turn-lease admission the replay goes
    through (agent.run_conversation -> admit_durable_turn_lease, platform desktop: wait_seconds=0)."""
    from agent.turn_facade_lease import admit_durable_turn_lease

    def run(rid, sid, session, text, **kw):
        agent = types.SimpleNamespace(
            _session_db=db, session_id="session-key", _persist_disabled=False, _interrupt_requested=False,
            _interrupt_message=None, _execution_thread_id=None, _session_turn_lease_refresh_interval=60.0,
            platform="desktop", statuses=[])
        agent._emit_status = agent._emit_warning = agent.statuses.append
        agent._touch_activity = lambda *a, **k: None
        agent._liveness_activity_lock = lambda: threading.Lock()
        admission = admit_durable_turn_lease(
            agent, session_id="session-key", relay_turn_id="session-key:t:replay",
            task_context={"session_id": "session-key", "task_id": "t", "platform": "desktop"},
            conversation_history=[])
        admitted.append((text, admission.early_result, list(agent.statuses)))
        if admission.lease is not None:
            admission.lease.release()
    return run


def test_replay_after_restart_takes_the_lease_over_a_stale_pre_restart_row_and_a_busy_state_db(
        held, monkeypatch):
    """Card fix 3 (live 17:44:32 PDT: "state.db write lock unavailable for 0s" then "session turn lease wait
    timed out", failure_reason=session_busy, no turn running). The replay must not die on the lease when the
    only row is the dead pre-restart backend's and state.db is briefly write-locked by another writer."""
    import hermes_state
    from hermes_state import SessionDB
    from hermes_state_pidns import holder_namespace_token
    monkeypatch.setattr("agent.turn_liveness.resolve_turn_liveness_settings", lambda cfg: (None, 1.0))
    db = SessionDB(held.home / "state.db")
    db.create_session("session-key", source="desktop")
    _submit()
    # The pre-restart backend's unexpired lease row (its pid is gone after the update restart).
    dead = f"pid=424242{holder_namespace_token()}:turn=before-update:platform=desktop"
    assert db.try_acquire_session_turn_lease("session-key", dead, ttl_seconds=300)
    monkeypatch.setattr(hermes_state, "psutil", types.SimpleNamespace(pid_exists=lambda pid: pid != 424242))
    # Another process holds the state.db write lock when the replay asks for the lease, past the first
    # attempt's write patience; it lets go before the retry. Without the busy-DB floor (BUSY_DB_WAIT_SECONDS)
    # a wait_seconds=0 Desktop admission never retries and reports "another active writer".
    blocker = sqlite3.connect(str(held.home / "state.db"), timeout=0, isolation_level=None,
                              check_same_thread=False)
    blocker.execute("BEGIN IMMEDIATE")
    attempts = []
    first_try = db.try_acquire_session_turn_lease

    def try_acquire(*a, **kw):
        attempts.append(1)
        if len(attempts) == 2:
            blocker.commit()
        return first_try(*a, **kw)

    monkeypatch.setattr(db, "try_acquire_session_turn_lease", try_acquire)
    admitted = []
    reborn = _restart(held, monkeypatch, _desktop_lease_replay(db, admitted))
    try:
        assert server._maybe_schedule_auto_continue("sid", reborn, "session-key") is not None
    finally:
        blocker.close()
    assert len(admitted) == 1 and len(attempts) >= 2, "the busy first attempt was retried"
    text, early, statuses = admitted[0]
    assert text == "hello there"
    assert early is None, f"replay refused: {early and early.get('final_response')}"
    assert not any("another active writer" in str(s) for s in statuses), statuses
    assert db.session_turn_lease_owner("session-key") is None, "the replay released its lease"


# Exactly once across the three ways a hold-queued prompt meets a restart or reconnect: the restart (above),
# the prompt accepted just before the restart, and a renderer reload while this backend still holds the queue.


def _run_real_drain(held, monkeypatch, turn_body):
    """Lift the hold and let the armed retry drain the queue through the REAL claim/dispatch path; only the
    turn body (``_run_prompt_submit``) is ``turn_body``, which starts the turn the way run_body does."""
    monkeypatch.setattr(server, "_dispatch_claimed_queue", _REAL_DISPATCH_CLAIMED_QUEUE)
    monkeypatch.setattr(server, "_replace_queued_user_row_for_turn", lambda *a, **k: None)
    monkeypatch.setattr(server, "_run_prompt_submit", turn_body)
    af.clear_hold(held.home)
    held.timers.pop().target()


def test_prompt_accepted_just_before_the_restart_is_continued_not_replayed(held, monkeypatch):
    """The hold lifted and the queued prompt's turn started (its marker is the turn's own), then the update's
    SIGTERM killed it mid-turn. The next backend must continue that turn, not send the user's text again."""
    from tui_gateway.turn_marker import read_turn_marker
    _submit()
    started = []

    def killed_mid_turn(rid, sid, session, text, **kw):
        started.append(text)
        server._record_turn_marker(session, text)  # run_body's first step; the SIGTERM lands before the end

    _run_real_drain(held, monkeypatch, killed_mid_turn)
    assert started == ["hello there"]
    marker = read_turn_marker(held.home, "session-key")
    assert marker and "reason" not in marker, "the started turn owns the marker; the hold-queue entry is gone"
    ran = []
    reborn = _restart(held, monkeypatch, lambda rid, sid, session, text, **kw: ran.append((text, kw.get("display_kind"))))
    assert server._maybe_schedule_auto_continue("sid", reborn, "session-key") is not None
    assert len(ran) == 1 and ran[0][1] == "auto_continue", ran
    assert ran[0][0] != "hello there", "an interrupted-turn continuation, not a second copy of the prompt"
    assert held.persisted == [("hello there", None)], "one accept row"


def test_prompt_answered_just_before_the_restart_is_not_sent_again(held, monkeypatch):
    from tui_gateway.turn_marker import read_turn_marker
    _submit()

    def answered(rid, sid, session, text, **kw):
        key = server._record_turn_marker(session, text)
        server._retire_turn_marker(session, key)  # the terminal frame went out

    _run_real_drain(held, monkeypatch, answered)
    assert read_turn_marker(held.home, "session-key") is None
    ran = []
    reborn = _restart(held, monkeypatch, lambda rid, sid, session, text, **kw: ran.append(text))
    assert server._maybe_schedule_auto_continue("sid", reborn, "session-key") is None
    assert ran == []


def _warm_reload(monkeypatch, session):
    """A renderer reload reconnects to THIS backend: session.resume reuses the live record (warm)."""
    from tui_gateway import update_pause
    monkeypatch.setattr(server, "_reattach_refusal", lambda *a, **k: None)
    monkeypatch.setattr(server, "_cancel_ws_orphan_reap", lambda *a, **k: None)
    monkeypatch.setattr(server, "_live_session_payload", lambda *a, **k: {"message_count": 0})
    monkeypatch.setattr(update_pause, "_detect_skew", lambda: None)
    ctx = types.SimpleNamespace(rid="r2", target="session-key", lazy=False, cols=80, omit_messages=False,
                                inline_images=False, defer_history=False)
    return server._resume_reuse_live_locked(ctx, "sid", session)


def test_renderer_reload_under_the_hold_leaves_the_prompt_to_the_live_queue(held, monkeypatch):
    """The marker's writer is this very process, so the writer-alive gate does not hold it back; the live
    queue still owns the prompt and drains it when the hold lifts. A replay here sent it twice."""
    from tui_gateway.turn_marker import read_turn_marker
    _submit()
    replayed = []
    monkeypatch.setattr(server, "_start_session_work", lambda target, **kw: target() or True)
    monkeypatch.setattr(server, "_start_agent_build", lambda *a, **k: None)
    monkeypatch.setattr(server, "_wait_agent", lambda *a, **k: None)
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *a, **k: None)
    monkeypatch.setattr(server, "_adopt_stored_continuation_source", lambda *a, **k: None)
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(server, "_run_prompt_submit", lambda rid, sid, session, text, **kw: replayed.append(text))
    resp = _warm_reload(monkeypatch, held.session)
    assert "auto_continue" not in resp["result"], resp
    assert replayed == []
    assert read_turn_marker(held.home, "session-key")["reason"] == "update_hold_queue", "kept for a restart"
    af.clear_hold(held.home)
    held.timers.pop().target()
    assert held.dispatched == ["hello there"] and replayed == []


def test_renderer_gone_long_enough_to_reap_the_session_replays_once_in_the_same_backend(held, monkeypatch):
    """The reload outlasted the orphan grace: the reaper's interrupt dropped the in-memory queue (marker kept,
    reason ws_orphan), so the next resume in the SAME process replays it; the old record's timer sends nothing."""
    from tui_gateway.turn_marker import read_turn_marker
    _submit()
    old = held.session
    old_timer = held.timers.pop()
    monkeypatch.setattr(server, "_clear_pending", lambda *a, **k: None)
    server._interrupt_session_turn("sid", old, orphan=True)
    assert not old.get("queued_prompt") and read_turn_marker(held.home, "session-key")["reason"] == "update_hold_queue"
    server._sessions.pop("sid", None)
    af.clear_hold(held.home)
    old_timer.target()
    assert held.dispatched == []
    ran = []
    reborn = {
        "agent": types.SimpleNamespace(valid_tool_names=set()), "session_key": "session-key", "history": [],
        "history_lock": threading.RLock(), "history_version": 0, "running": False, "transport": None,
        "attached_images": [], "profile_home": str(held.home)}
    server._sessions["sid"] = reborn
    for name in ("_start_agent_build", "_wait_agent", "_ensure_active_session_slot",
                 "_adopt_stored_continuation_source", "_emit"):
        monkeypatch.setattr(server, name, lambda *a, **k: None)
    monkeypatch.setattr(server, "_start_session_work", lambda target, **kw: target() or True)
    monkeypatch.setattr(server, "_run_prompt_submit", lambda rid, sid, session, text, **kw: ran.append(text))
    assert server._maybe_schedule_auto_continue("sid", reborn, "session-key") is not None
    assert ran == ["hello there"]
    assert server._maybe_schedule_auto_continue("sid", reborn, "session-key") is None, "scheduled once"


def _interrupt(rid="r3", **params):
    return server._methods["session.interrupt"](rid, {"session_id": "sid", **params})


@pytest.fixture
def stoppable(held, monkeypatch):
    monkeypatch.setattr(server, "_clear_pending", lambda *a, **k: None)
    monkeypatch.setattr(server, "_resume_wake_after_interrupt", lambda: None)
    return held


def test_delete_under_the_hold_discards_the_prompt_for_the_next_backend_too(stoppable, monkeypatch):
    """Explicit deletion (Desktop deletes the chat: origin ``delete``) keeps the old discard semantics."""
    from tui_gateway.turn_marker import read_turn_marker
    held = stoppable
    _submit()
    resp = _interrupt(origin="delete")
    assert resp["result"]["status"] == "interrupted", resp
    assert not held.session.get("queued_prompt")
    assert read_turn_marker(held.home, "session-key") is None, "a deleted message is not replayed after the update"
    ran = []
    reborn = _restart(held, monkeypatch, lambda rid, sid, session, text, **kw: ran.append(text))
    assert server._maybe_schedule_auto_continue("sid", reborn, "session-key") is None
    assert ran == []


# ── t_01a1aa2a13e2e65d: the reply/snapshot say WHY it waits; an idle Stop/Esc does not destroy it ──────────────

_HOLD_SET_AT = 1_794_000_000.0  # a fixed, recognisable hold start (2026-11-07)


def _backdate_hold(home, at=_HOLD_SET_AT):
    os.utime(home / "core-update" / "holds" / "hermes-sync", (at, at))


def test_reply_names_the_update_hold_and_when_the_hold_was_set(held):
    from tui_gateway.contracts.prompt_voice import PromptSubmitResult
    _backdate_hold(held.home)
    resp = _submit()
    assert resp["result"] == {"status": "queued", "queued_reason": "core_update", "queued_started_at": _HOLD_SET_AT}
    assert PromptSubmitResult.model_validate(resp["result"]).queued_started_at == _HOLD_SET_AT
    envelope = held.session["queued_prompt"]
    assert envelope["queued_reason"] == "core_update" and envelope["queued_started_at"] == _HOLD_SET_AT


def test_hold_time_comes_from_the_admission_hold_not_the_kanban_drain_hold(held):
    """The 5035 is refused by ``holds/hermes-sync`` (admission_fence.reserve); ``drain-hold`` is the Kanban
    claim hold with its own ``started_at``, which must not leak into the label."""
    _backdate_hold(held.home)
    (held.home / "core-update" / "drain-hold").write_text(json.dumps(
        {"pid": os.getpid(), "started_at": _HOLD_SET_AT - 3600, "reason": "update"}))
    assert _submit()["result"]["queued_started_at"] == _HOLD_SET_AT


def test_unreadable_hold_time_is_null_not_a_guess(held, monkeypatch):
    from hermes_cli import admission_fence
    monkeypatch.setattr(admission_fence, "hold_started_at", lambda root: None)
    resp = _submit()
    assert resp["result"] == {"status": "queued", "queued_reason": "core_update", "queued_started_at": None}


def test_a_merged_second_send_keeps_the_reason_and_hold_time(held):
    _backdate_hold(held.home)
    _submit()
    resp = _submit(text="and another")
    assert resp["result"]["queued_reason"] == "core_update"
    assert resp["result"]["queued_started_at"] == _HOLD_SET_AT
    assert held.session["queued_prompt"]["text"] == "hello there\n\nand another"


def test_busy_fence_refusal_queues_without_the_update_label(held, monkeypatch):
    """Negative control: a 5035 that is NOT the hold (a briefly busy admission.lock) keeps its old reply."""
    from hermes_cli import backend_retirement
    af.clear_hold(held.home)
    monkeypatch.setattr(backend_retirement, "_reserve", lambda root, wait, what: (None, "busy"))
    resp = _submit()
    assert resp["result"] == {"status": "queued"}
    assert "queued_reason" not in held.session["queued_prompt"]


def test_live_snapshot_carries_the_reason_for_a_reconnecting_client(held):
    from tui_gateway.contracts.sessions import QueuedPrompt
    _backdate_hold(held.home)
    _submit()
    queued = server._queued_prompt_snapshot(held.session)
    assert queued == {"user": "hello there", "queued_reason": "core_update", "queued_started_at": _HOLD_SET_AT}
    assert QueuedPrompt.model_validate(queued).queued_reason.value == "core_update"
    # Control: an ordinary queued prompt's snapshot is unchanged.
    held.session["queued_prompt"] = {"text": "plain", "transport": None}
    assert server._queued_prompt_snapshot(held.session) == {"user": "plain"}


@pytest.mark.parametrize("origin", [None, "esc", "stop_button", "slash", "tile"])
def test_idle_stop_keeps_the_update_held_prompt_and_it_still_runs(stoppable, origin):
    from tui_gateway.turn_marker import read_turn_marker
    held = stoppable
    _backdate_hold(held.home)
    _submit()
    generation = held.session.get("_queued_prompt_generation", 0)
    resp = _interrupt(**({"origin": origin} if origin else {}))
    assert resp["result"]["status"] == "interrupted", resp
    assert held.session["queued_prompt"]["text"] == "hello there", "the RPC kept the queue"
    assert held.session["queued_prompt"]["queued_reason"] == "core_update"
    assert held.session.get("_queued_prompt_generation", 0) == generation, "no cancel-generation bump"
    assert not held.session.get("_turn_cancel_requested"), "no cancel flag to leak into the drained turn"
    assert read_turn_marker(held.home, "session-key")["reason"] == "update_hold_queue", "replay marker kept"
    assert server._queued_prompt_snapshot(held.session)["queued_started_at"] == _HOLD_SET_AT
    af.clear_hold(held.home)
    held.timers.pop().target()
    assert held.dispatched == ["hello there"], "the armed retry still sends it once the hold lifts"


def test_idle_stop_keeps_it_for_the_next_backend_after_an_update_restart(stoppable, monkeypatch):
    held = stoppable
    _submit()
    _interrupt(origin="esc")
    ran = []
    reborn = _restart(held, monkeypatch, lambda rid, sid, session, text, **kw: ran.append(text))
    assert server._maybe_schedule_auto_continue("sid", reborn, "session-key") is not None
    assert ran == ["hello there"]


@pytest.mark.parametrize("origin", ["delete", "rewind", "send_now", "plugin:hermes-bots"])
@pytest.mark.parametrize("compute_host", [False, True])
def test_non_stop_gestures_clear_the_held_queue_as_before(stoppable, origin, compute_host, monkeypatch):
    from tui_gateway.turn_marker import read_turn_marker
    held = stoppable
    _submit()
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *a, **k: compute_host)
    _interrupt(origin=origin)
    assert not held.session.get("queued_prompt")
    assert read_turn_marker(held.home, "session-key") is None


def test_idle_stop_still_clears_an_ordinary_queue(stoppable):
    """Negative control: only the update-held envelope survives; an unlabelled queued prompt is cancelled."""
    held = stoppable
    held.session["queued_prompt"] = {"text": "ordinary", "transport": None}
    generation = held.session.get("_queued_prompt_generation", 0)
    _interrupt(origin="esc")
    assert not held.session.get("queued_prompt")
    assert held.session["_queued_prompt_generation"] == generation + 1
    assert held.session["_turn_cancel_requested"] is True


def test_stop_of_a_running_turn_still_clears_the_queue(stoppable, monkeypatch):
    held = stoppable
    _submit()
    held.session["running"] = True  # e.g. the drain claimed it: the Stop is for that turn
    monkeypatch.setattr("agent.interrupt_compat.request_hard_interrupt", lambda *a, **k: None)
    _interrupt(origin="esc")
    assert not held.session.get("queued_prompt")
    assert held.session["_turn_cancel_requested"] is True


@pytest.mark.parametrize("reason", ["backend_exit", "lease_takeover", "session_close"])
def test_non_user_teardown_clears_the_held_queue_even_if_asked_to_keep(held, monkeypatch, reason):
    monkeypatch.setattr(server, "_clear_pending", lambda *a, **k: None)
    _submit()
    server._interrupt_session_turn("sid", held.session, reason=reason, keep_update_held_queue=True)
    assert not held.session.get("queued_prompt")
    from tui_gateway.turn_marker import read_turn_marker
    assert read_turn_marker(held.home, "session-key")["reason"] == "update_hold_queue"


def test_orphan_reap_with_keep_still_clears(held, monkeypatch):
    monkeypatch.setattr(server, "_clear_pending", lambda *a, **k: None)
    _submit()
    server._interrupt_session_turn("sid", held.session, orphan=True, keep_update_held_queue=True)
    assert not held.session.get("queued_prompt")


def test_idle_stop_on_an_unreachable_compute_host_keeps_the_held_prompt(stoppable, monkeypatch):
    """The recovery path (host dial failed) clears the queue too; an idle Stop keeps the held one there."""
    held = stoppable
    _submit()
    held.session["_compute_host_active"] = True

    class Supervisor:
        def interrupt(self, *a, **k):
            raise BrokenPipeError("host gone")

        def has_pending_turn(self, sid):
            return False

    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *a, **k: True)
    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda: Supervisor())
    resp = _interrupt(origin="esc")
    assert resp["result"]["status"] == "interrupted", resp
    assert held.session["queued_prompt"]["text"] == "hello there"
    # Control: the same failure with a delete clears it.
    _interrupt(origin="delete")
    assert not held.session.get("queued_prompt")


# ── t_01a1ceb141b10c7c: one hold-queued submit is ONE user row in state.db, not an accept row + a replay row ──
# Live (hub, 2026-11-12, t_01a1c8d8a5c1f725): a submit queued with queued_reason=core_update left msg 1217133
# (accept, deactivated) and msg 1217162 (drain re-append) for one prompt; the turn ran once.
_REAL_PERSIST_QUEUED_USER_ROW = server._persist_queued_user_row
_REAL_REPLACE_QUEUED_USER_ROW = server._replace_queued_user_row_for_turn


def _real_rows_drain(held, monkeypatch):
    """Real accept-time persist, real claim/dispatch and real drain row handling over a real state.db; only the
    turn body is recorded (the slot it would adopt via ``_adopt_submit_user_row``)."""
    from hermes_state import SessionDB
    db = SessionDB(held.home / "state.db")
    monkeypatch.setattr(server, "_persist_queued_user_row", _REAL_PERSIST_QUEUED_USER_ROW)
    monkeypatch.setattr(server, "_dispatch_claimed_queue", _REAL_DISPATCH_CLAIMED_QUEUE)
    monkeypatch.setattr(server, "_replace_queued_user_row_for_turn", _REAL_REPLACE_QUEUED_USER_ROW)
    turns = []

    def turn_body(rid, sid, session, text, **kw):
        turns.append((text, session.get("_submit_user_row")))
        session["running"] = False
        server._clear_inflight_turn(session)

    monkeypatch.setattr(server, "_run_prompt_submit", turn_body)
    return db, turns


def _user_rows(db, text):
    with db._lock:
        rows = db._conn.execute(
            "SELECT id, active, display_metadata FROM messages WHERE session_id = ? AND role = 'user' "
            "AND content = ? ORDER BY id", ("session-key", text)).fetchall()
    return [(r[0], r[1], json.loads(r[2]) if r[2] else None) for r in rows]


def test_hold_queued_submit_then_replay_writes_exactly_one_user_row(held, monkeypatch):
    db, turns = _real_rows_drain(held, monkeypatch)
    resp = _submit()
    assert resp["result"]["queued_reason"] == "core_update", resp
    accepted = _user_rows(db, "hello there")
    assert len(accepted) == 1 and accepted[0][1] == 1, "durable and visible at accept"
    af.clear_hold(held.home)
    held.timers.pop().target()
    assert len(turns) == 1, "the turn ran once"
    rows = _user_rows(db, "hello there")
    assert len(rows) == 1, f"one submit must be one user row, got {rows}"
    row_id, active, meta = rows[0]
    assert active == 1 and not (meta or {}).get("_queued_prompt"), "drained: active and no longer marked queued"
    assert row_id == accepted[0][0], "the accept-time row itself is the turn's row"
    text, slot = turns[0]
    assert text == "hello there" and slot and slot["_row_id"] == row_id, "the turn adopts that row (no 2nd write)"
    assert not (slot.get("display_metadata") or {}).get("_queued_prompt")
    assert db.retire_undrained_queue_rows("session-key") == 0, "a restart after the drain does not retire it"


def test_queued_row_behind_a_later_row_is_still_replaced_at_the_end(held, monkeypatch):
    """Control: when the transcript grew past the accept row (the busy-queue shape: the in-flight turn's reply
    landed after it), the drain must still re-place it at the end so alternation stays [uA, aA, uB]."""
    db, turns = _real_rows_drain(held, monkeypatch)
    _submit()
    db.append_message("session-key", "assistant", content="a reply that landed after the accept")
    af.clear_hold(held.home)
    held.timers.pop().target()
    rows = _user_rows(db, "hello there")
    assert [r[1] for r in rows] == [0, 1], rows
    assert turns[0][1]["_row_id"] == rows[1][0]
    with db._lock:
        order = [r[0] for r in db._conn.execute(
            "SELECT role FROM messages WHERE session_id = ? AND active = 1 ORDER BY id", ("session-key",))]
    assert order == ["assistant", "user"]


def test_a_compaction_resequenced_accept_row_is_adopted_at_its_live_id(held, monkeypatch):
    """An in-place compaction can re-sequence the accept row to a clone at a higher id (#123675): the drain
    adopts the LIVE clone and appends nothing."""
    db, turns = _real_rows_drain(held, monkeypatch)
    _submit()
    (orig, _, _), = _user_rows(db, "hello there")
    with db._lock:
        db._conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp, display_metadata) SELECT session_id, role, "
            "content, timestamp, display_metadata FROM messages WHERE id = ?", (orig,))
        db._conn.execute("UPDATE messages SET active = 0 WHERE id = ?", (orig,))
        db._conn.commit()
    clone = _user_rows(db, "hello there")[1][0]
    af.clear_hold(held.home)
    held.timers.pop().target()
    rows = _user_rows(db, "hello there")
    assert [(r[0], r[1]) for r in rows] == [(orig, 0), (clone, 1)], rows
    assert not (rows[1][2] or {}).get("_queued_prompt")
    assert turns[0][1]["_row_id"] == clone


def test_a_still_queued_envelope_is_never_unmarked_in_place(held, monkeypatch):
    """Only the DISPATCHING envelope adopts in place: a still-queued one must keep its never-drained marker so a
    restart before its own drain retires it (#125577) instead of gluing it into the previous turn."""
    db, _ = _real_rows_drain(held, monkeypatch)
    _submit()
    envelope = held.session["queued_prompt"]
    server._replace_queued_user_row_for_turn(held.session, envelope)
    rows = _user_rows(db, "hello there")
    assert [(r[1], (r[2] or {}).get("_queued_prompt")) for r in rows] == [(0, True), (1, True)], rows
    assert db.retire_undrained_queue_rows("session-key") == 1


def test_hold_started_at_reads_the_marker(tmp_path):
    root = tmp_path / "core-update"
    assert af.hold_started_at(root) is None, "no hold: unknown, never a guess"
    af.set_hold(tmp_path)
    _backdate_hold(tmp_path)
    af.set_hold(tmp_path)  # re-set while held keeps the original start
    assert af.hold_started_at(root) == _HOLD_SET_AT
    af.clear_hold(tmp_path)
    assert af.hold_started_at(root) is None


# ── Review fixes: an idle held Stop never reaches the compute-host child; marker retirement is the helper's ───
# own decision (t_01a1aa2a13e2e65d)


class _RecordingSupervisor:
    def __init__(self):
        self.interrupts = []

    def interrupt(self, sid, **kw):
        self.interrupts.append(sid)

    def has_pending_turn(self, sid):
        return False


def _route_through_compute_host(monkeypatch, supervisor):
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *a, **k: True)
    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda *a, **k: supervisor)


def test_idle_held_stop_after_an_earlier_isolated_turn_sends_no_child_interrupt(stoppable, monkeypatch):
    """``_compute_host_active`` outlives the previous turn. An idle Stop that keeps the held queue must not
    send the child an interrupt frame: the child bumps its own queue generation, the parent (queue kept)
    does not, and the drained turn would then be rejected by the child as stale."""
    held = stoppable
    _submit()
    held.session["_compute_host_active"] = True
    supervisor = _RecordingSupervisor()
    _route_through_compute_host(monkeypatch, supervisor)
    generation = held.session.get("_queued_prompt_generation", 0)
    resp = _interrupt(origin="esc")
    assert resp["result"]["status"] == "interrupted", resp
    assert supervisor.interrupts == [], "nothing runs in the child: no interrupt frame"
    assert held.session["queued_prompt"]["text"] == "hello there"
    assert held.session.get("_queued_prompt_generation", 0) == generation
    # Control: the same session with a delete still interrupts the child and clears the queue.
    _interrupt(origin="delete")
    assert supervisor.interrupts == ["sid"] and not held.session.get("queued_prompt")


def test_stop_while_a_child_turn_is_in_flight_interrupts_it_even_if_parent_running_lags(stoppable, monkeypatch):
    """The compute-host turn is in flight from dispatch to completion (``_compute_host_turn_id``), whatever
    the parent's ``running`` mirror says: such a Stop is for that turn, so it interrupts and clears."""
    held = stoppable
    _submit()
    held.session.update(_compute_host_active=True, _compute_host_turn_id="t-live")
    supervisor = _RecordingSupervisor()
    _route_through_compute_host(monkeypatch, supervisor)
    _interrupt(origin="esc")
    assert supervisor.interrupts == ["sid"]
    assert not held.session.get("queued_prompt")
    assert held.session["_turn_cancel_requested"] is True


def test_idle_held_stop_then_hold_lift_runs_the_turn_on_a_real_compute_host(stoppable, monkeypatch, tmp_path):
    """End to end over the real child: a previous isolated turn left the host active; the user Stops while
    only the update-held message waits; the hold lifts; the real drain sends it to a real ``ComputeHost``,
    which must START it (a stale generation ends it silently as ``interrupted`` and the message is lost)."""
    import io

    from tests.tui_gateway.test_compute_host_turn_protocol import _agent, _frames, _session, _wait
    from tui_gateway.compute_host import ComputeHost
    held = stoppable
    # The compute-host protocol tests' ``turn_env``: only the turn pipeline's environment-heavy side paths.
    for name, stub in (("_wire_callbacks", lambda sid: None), ("_sync_agent_model_with_config", lambda *a: None),
                       ("_session_cwd", lambda session: str(tmp_path)), ("_register_session_cwd", lambda s: None),
                       ("_tts_stream_begin", lambda: None), ("_sync_session_key_after_compress", lambda *a, **k: None),
                       ("_get_usage", lambda agent: {})):
        monkeypatch.setattr(server, name, stub)
    out = io.StringIO()
    host = ComputeHost(stdout=out, heartbeat_secs=0)
    child = _session(_agent(["done"]))
    server._sessions["child"] = child  # the child's own session record (separate process in production)
    completions = []

    class Relay:
        """HostSupervisor's wire role: forward frames to the child host, retargeted at its session."""

        def interrupt(self, sid, request_id=None, **kw):
            host.handle_frame({"type": "interrupt", "sid": "child", "request_id": request_id or "i", **kw})
            _wait(out, lambda f: f["type"] == "interrupt.ack")

        def has_pending_turn(self, sid):
            return False

        def submit_turn(self, frame, on_complete):
            host.handle_frame({**frame, "sid": "child"})
            completions.append(_wait(out, lambda f: f["type"] in ("turn.end", "turn.error")))

    try:
        _route_through_compute_host(monkeypatch, Relay())
        monkeypatch.setattr(server, "_load_dashboard_process_isolation_config", lambda: {"turn_isolation": True})
        monkeypatch.setattr(server, "_dispatch_claimed_queue", _REAL_DISPATCH_CLAIMED_QUEUE)
        monkeypatch.setattr(server, "_replace_queued_user_row_for_turn", lambda *a, **k: None)
        held.session["_compute_host_active"] = True  # left over from the previous isolated turn
        _submit()
        assert _interrupt(origin="esc")["result"]["turn_isolation"] is True
        af.clear_hold(held.home)
        held.timers.pop().target()
        assert len(completions) == 1, "the drain dispatched the kept message to the host"
        frames = _frames(out)
        assert any(f["type"] == "turn.started" for f in frames), frames
        assert completions[0]["type"] == "turn.end" and not completions[0].get("interrupted"), completions
        assert [m["content"] for m in child["history"] if m["role"] == "user"] == ["hello there"]
    finally:
        server._sessions.pop("child", None)
        host.close()


def test_marker_retirement_uses_the_stops_own_decision_not_a_later_reread(stoppable, monkeypatch):
    """Between the Stop's keep decision and its marker handling the hold can lift and the retry can claim the
    queue and start the turn (it records its own marker). The Stop must not then retire that new turn's
    marker or take its active-marker key: it decided to keep before the turn existed."""
    from tui_gateway.turn_marker import read_turn_marker
    held = stoppable
    _submit()
    started = []

    def turn_body(rid, sid, session, text, **kw):
        started.append(text)
        server._record_turn_marker(session, text)  # the drained turn is now live and owns the marker

    def retry_claims_in_the_window(agent, **kw):
        """Runs inside ``_interrupt_session_turn`` after its keep decision (the background-review cancel a
        kept Stop still makes), before the RPC's marker step."""
        if not started:
            _run_real_drain(held, monkeypatch, turn_body)

    monkeypatch.setattr("agent.background_review.cancel_background_review_for_live_turn", retry_claims_in_the_window)
    resp = _interrupt(origin="esc")
    assert resp["result"]["status"] == "interrupted", resp
    assert started == ["hello there"], "the retry claimed and started the kept message in the window"
    assert held.session["running"] is True
    marker = read_turn_marker(held.home, "session-key")
    assert marker is not None and "reason" not in marker, "the live turn's marker survives the earlier Stop"
    assert held.session.get("_active_turn_marker_key") == "session-key", "the live turn keeps its key"


def test_interrupt_helper_keeps_its_bool_contract_and_reports_the_keep_outcome(stoppable):
    held = stoppable
    _submit()
    outcome = {}
    assert server._interrupt_session_turn("sid", held.session, keep_update_held_queue=True, outcome=outcome) is False
    assert outcome["kept_update_held_queue"] is True
    outcome = {}
    assert server._interrupt_session_turn("sid", held.session, outcome=outcome) is False
    assert outcome["kept_update_held_queue"] is False and not held.session.get("queued_prompt")
