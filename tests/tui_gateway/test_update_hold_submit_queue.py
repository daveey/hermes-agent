"""An idle prompt.submit refused only by the core-update admission hold is queued, never bounced.

The update drainer holds admission for a bounded drain window while in-flight turns finish (hermes-sync
``drain_window``, t_01a181e61e88e5d4). Desktop has no handling for 5035, so a refusal there showed the
user an error for the message they had just sent. The message now joins the durable queue (persisted at
accept) and the admission retry runs it once the hold lifts. A retiring backend keeps its 5035.
"""
import json
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
    assert resp["result"] == {"status": "queued"}
    assert PromptSubmitResult.model_validate(resp["result"]).status == "queued"


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


def test_stop_under_the_hold_discards_the_prompt_for_the_next_backend_too(held, monkeypatch):
    from tui_gateway.turn_marker import read_turn_marker
    _submit()
    monkeypatch.setattr(server, "_clear_pending", lambda *a, **k: None)
    monkeypatch.setattr(server, "_resume_wake_after_interrupt", lambda: None)
    resp = server._methods["session.interrupt"]("r3", {"session_id": "sid"})
    assert resp["result"]["status"] == "interrupted", resp
    assert not held.session.get("queued_prompt")
    assert read_turn_marker(held.home, "session-key") is None, "a stopped message is not replayed after the update"
    ran = []
    reborn = _restart(held, monkeypatch, lambda rid, sid, session, text, **kw: ran.append(text))
    assert server._maybe_schedule_auto_continue("sid", reborn, "session-key") is None
    assert ran == []
