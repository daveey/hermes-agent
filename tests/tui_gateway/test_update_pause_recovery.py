"""Paused updates recover without a user send, including a warm Desktop reattach."""
import json
import threading
import time
import types

import pytest

from hermes_cli import admission_fence as af
from hermes_state import SessionDB
from tui_gateway import server, startup_resume as sr, update_pause as up
from tui_gateway.turn_marker import mark_turn_paused, record_turn_start

CFG = {"desktop": {"auto_continue": {"enabled": True, "resume_on_startup": True,
       "freshness_minutes": 240, "resume_on_startup_delay_seconds": 0}}}


@pytest.fixture
def recovery(tmp_path, monkeypatch, request):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("paused", source=getattr(request, "param", "desktop"))
    db.append_message("paused", "user", "finish the request")
    db.append_message("paused", "assistant", "completed step one")
    record_turn_start(tmp_path, "paused", "finish the request")
    mark_turn_paused(tmp_path, "paused", up.PAUSE_REASON)
    path = tmp_path / "desktop" / "interrupted_turns.json"
    entries = json.loads(path.read_text())
    entries["paused"]["writer_pid"] = 2 ** 22 - 3
    entries["paused"].pop("writer_start_time", None)
    path.write_text(json.dumps(entries))
    monkeypatch.setattr(server, "_hermes_home", tmp_path)
    monkeypatch.setattr(server, "_load_cfg", lambda: CFG)
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_profile_home", lambda p: tmp_path)
    monkeypatch.setattr(server, "_profile_session_db", lambda home: (db, False))
    monkeypatch.setattr(server, "_default_session_cwd", lambda: str(tmp_path))
    monkeypatch.setattr(server, "_enable_gateway_prompts", lambda: None)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda: None)
    monkeypatch.setattr(server, "_schedule_agent_build", lambda *a: None)
    monkeypatch.setattr(server, "_start_agent_build", lambda *a: None)
    monkeypatch.setattr(server, "_wait_agent", lambda *a, **kw: None)
    real_slot = server._ensure_active_session_slot
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *a, **kw: None)
    monkeypatch.setattr(server, "_emit", lambda *a, **kw: None)
    calls, done, platforms = [], threading.Event(), []
    def submit(rid, sid, session, text, **kw):
        calls.append((text, kw))
        platforms.append(server._deferred_build_agent_kwargs(session, None)["platform_override"])
        record_turn_start(tmp_path, "paused", "finish the request", pauses=1)
        done.set()
    monkeypatch.setattr(server, "_run_prompt_submit", submit)
    before = set(server._sessions)
    yield types.SimpleNamespace(home=tmp_path, calls=calls, done=done, platforms=platforms, real_slot=real_slot)
    for sid in set(server._sessions) - before:
        server._sessions.pop(sid, None)
    db.close()


def mount():
    response = server.handle_request({"id": "resume", "method": "session.resume", "params": {
        "session_id": "paused", "source": "desktop", "omit_messages": True}})
    assert "error" not in response, response
    return response["result"]


def assert_continued(r):
    assert r.done.wait(5), "no automatic continuation dispatched"
    assert len(r.calls) == 1
    assert r.calls[0][0] == up.pause_note("finish the request")
    assert r.calls[0][1]["display_kind"] == "auto_continue"


def test_replaced_backend_notice_requeues_before_real_continuation(recovery, monkeypatch):
    from agent import memory_provider
    from hermes_cli import config
    import hermes_constants

    # Fresh backend record: only the dead writer's durable pause survives.
    af.set_hold(recovery.home)
    sid = mount()["session_id"]
    af.clear_hold(recovery.home)
    session = server._sessions[sid]
    assert not any(k.startswith("_update_pause_") for k in session)
    clock, attempts, submitted = [0.0], [], []
    monkeypatch.setattr(server, "_start_session_work", lambda fn, **kw: fn() or True)
    monkeypatch.setattr(server, "_ensure_active_session_slot",
                        lambda *a, **kw: attempts.append(clock[0]))
    monkeypatch.setattr(server, "_collect_kanban_notifications", lambda *a: [])
    session["_kanban_pending"] = ["a queued owner notice"]
    session["running"] = True  # a notice already in flight when the sweep first sees us
    original_submit = server._run_prompt_submit
    def submit(*a, **kw):
        submitted.append(clock[0])
        original_submit(*a, **kw)
    monkeypatch.setattr(server, "_run_prompt_submit", submit)
    def sleep(seconds):
        if not seconds:
            return
        clock[0] += seconds
        assert attempts == [] and recovery.calls == []
        session["running"] = False  # the old notice ends before the retry poll
        server._notif_poll_kanban_scoped(sid, session)
        assert session["_kanban_pending"] == ["a queued owner notice"]
        assert not session["running"]  # queued notice refused; pause wins
    monkeypatch.setattr(config, "load_config", lambda: CFG)
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: recovery.home)
    monkeypatch.setattr(sr, "time", types.SimpleNamespace(
        sleep=sleep, time=time.time, monotonic=lambda: clock[0]))
    class Inline:
        def __init__(self, target):
            self.target = target
        def start(self):
            self.target()
    monkeypatch.setattr(memory_provider, "spawn_context_thread", lambda target, **kw: Inline(target))
    # Drive the real startup worker + resume handler, not an explicit scheduling call.
    sr.start_startup_resume_sweep(stop=recovery.done)
    assert_continued(recovery)
    assert attempts == submitted == [5.0]
    assert submitted[0] <= 120
    assert not up.continuation_due(session)


def test_replaced_backend_slot_refusal_backs_off(recovery, monkeypatch):
    af.set_hold(recovery.home)
    sid = mount()["session_id"]
    af.clear_hold(recovery.home)
    session = server._sessions[sid]
    clock, attempts = [0.0], []
    monkeypatch.setattr(server, "time", types.SimpleNamespace(time=time.time, monotonic=lambda: clock[0]))
    monkeypatch.setattr(server, "_start_session_work", lambda fn, **kw: fn() or True)
    def slot(*args, **kwargs):
        assert kwargs == {"takeover_idle_only": True}
        attempts.append(clock[0])
        return "another live owner" if len(attempts) == 1 else None
    monkeypatch.setattr(server, "_ensure_active_session_slot", slot)
    assert server._maybe_schedule_auto_continue(sid, session, "paused") is not None
    for second in range(1, 30):
        clock[0] = float(second)
        assert server._maybe_schedule_auto_continue(sid, session, "paused") is None
    assert attempts == [0.0]
    clock[0] = 30.0
    assert server._maybe_schedule_auto_continue(sid, session, "paused") is not None
    assert_continued(recovery)
    assert attempts == [0.0, 30.0]


def test_two_readers_do_not_replay_a_consumed_pause(recovery, monkeypatch):
    af.set_hold(recovery.home)
    sid = mount()["session_id"]
    af.clear_hold(recovery.home)
    first = server._sessions[sid]
    second = {**first, "history_lock": threading.Lock()}
    pending = []
    monkeypatch.setattr(server, "_start_session_work", lambda fn, **kw: pending.append(fn) or True)
    assert server._maybe_schedule_auto_continue(sid, first, "paused") is not None
    assert server._maybe_schedule_auto_continue("second", second, "paused") is not None
    pending[0]()  # writes the accepted continuation's marker, as the real prompt path does
    pending[1]()  # previously read the same dead writer; must revalidate after admission
    assert_continued(recovery)
    assert second["running"] is False


def test_pause_readers_use_exclusive_slot_without_detached_takeover(recovery, monkeypatch):
    af.set_hold(recovery.home)
    sid = mount()["session_id"]
    af.clear_hold(recovery.home)
    first = server._sessions[sid]
    second = {**first, "history_lock": threading.Lock()}
    server._sessions["second"] = second
    pending = []
    monkeypatch.setattr(server, "_ensure_active_session_slot", recovery.real_slot)
    monkeypatch.setattr(server, "_start_session_work", lambda fn, **kw: pending.append(fn) or True)
    assert server._maybe_schedule_auto_continue(sid, first, "paused") is not None
    assert server._maybe_schedule_auto_continue("second", second, "paused") is not None
    original = server._run_prompt_submit
    def submit(*args, **kwargs):
        # Winner owns the real registry slot but has not replaced the pause yet.
        assert first["active_session_lease"] is not None
        pending[1]()
        assert not second["running"]
        assert second.get("active_session_lease") is None
        assert first["running"] and not first.get("_turn_cancel_requested")
        original(*args, **kwargs)
    monkeypatch.setattr(server, "_run_prompt_submit", submit)
    try:
        pending[0]()
        assert_continued(recovery)
    finally:
        server._release_active_session_slot(first)
        server._release_active_session_slot(second)


@pytest.mark.parametrize("holder_running", [False, True])
def test_pause_reader_takes_over_only_an_idle_detached_holder(recovery, monkeypatch, holder_running):
    _detached_ws_transport = server._detached_ws_transport
    af.set_hold(recovery.home)
    sid = mount()["session_id"]
    af.clear_hold(recovery.home)
    first = server._sessions[sid]
    # A same-process sibling runtime of this chat lost its client but still holds the lease.
    holder = {**first, "history_lock": threading.Lock(), "transport": _detached_ws_transport,
              "running": holder_running}
    holder.pop("active_session_lease", None)
    server._sessions["holder"] = holder
    assert recovery.real_slot("holder", holder) is None
    monkeypatch.setattr(server, "_ensure_active_session_slot", recovery.real_slot)
    monkeypatch.setattr(server, "_start_session_work", lambda fn, **kw: fn() or True)
    monkeypatch.setattr(server, "_interrupt_session_turn", lambda *a, **kw: None)
    try:
        assert server._maybe_schedule_auto_continue(sid, first, "paused") is not None
        if holder_running:
            # Mid-turn holder (a notice, another reader's continuation): never interrupted; retry backs off.
            assert recovery.calls == [] and not first["running"]
            assert holder.get("active_session_lease") is not None
            assert first["_auto_continue_retry_after"] > time.monotonic()
        else:
            # Idle holder: no wait for its reap, the paused turn continues now.
            assert_continued(recovery)
            assert holder.get("active_session_lease") is None
    finally:
        server._release_active_session_slot(first)
        server._release_active_session_slot(holder)


def test_replaced_backend_durable_hold_is_bounded(recovery, monkeypatch):
    # Fresh backend whose continuation is refused forever (a live foreign owner): notices wait for the
    # paused turn at most HOLD_MAX_S, like the in-place hold, then flow; re-reading never re-arms it.
    af.set_hold(recovery.home)
    sid = mount()["session_id"]
    af.clear_hold(recovery.home)
    session = server._sessions[sid]
    mono = [1000.0]
    monkeypatch.setattr(up, "_mono", lambda: mono[0])
    monkeypatch.setattr(server, "_start_session_work", lambda fn, **kw: fn() or True)
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *a, **kw: "another live owner")
    assert server._maybe_schedule_auto_continue(sid, session, "paused") is not None
    assert recovery.calls == []
    def claim():
        claimed = server._notif_claim_turn(session)
        session["running"] = False
        return claimed
    for dt in (0.0, 60.0, up.HOLD_MAX_S - 1):
        mono[0] = 1000.0 + dt
        assert up.continuation_due(session) and not claim(), dt
    for dt in (up.HOLD_MAX_S, 900.0, 3600.0, 3 * 3600.0):
        mono[0] = 1000.0 + dt
        monkeypatch.setattr(up, "_now", lambda dt=dt: time.time() + min(dt, 3600.0))
        assert not up.continuation_due(session) and claim(), dt
    # A new pause (fresh paused_at) by a dead writer is a new hold, bounded the same way.
    mark_turn_paused(recovery.home, "paused", up.PAUSE_REASON)
    path = recovery.home / "desktop" / "interrupted_turns.json"
    entries = json.loads(path.read_text())
    entries["paused"]["writer_pid"] = 2 ** 22 - 3
    entries["paused"].pop("writer_start_time", None)
    path.write_text(json.dumps(entries))
    monkeypatch.setattr(up, "_now", time.time)
    assert up.continuation_due(session) and not claim()
    mono[0] += up.HOLD_MAX_S
    assert not up.continuation_due(session) and claim()


def test_typed_message_during_build_retires_scheduled_pause(recovery, monkeypatch):
    from tui_gateway.turn_marker import clear_turn_marker
    af.set_hold(recovery.home)
    sid = mount()["session_id"]
    af.clear_hold(recovery.home)
    session = server._sessions[sid]
    pending = []
    monkeypatch.setattr(server, "_start_session_work", lambda fn, **kw: pending.append(fn) or True)
    assert server._maybe_schedule_auto_continue(sid, session, "paused") is not None
    # The typed turn starts and ends before the old continuation finishes building.
    record_turn_start(recovery.home, "paused", "new typed request")
    clear_turn_marker(recovery.home, "paused")
    pending[0]()
    assert recovery.calls == []
    assert not session["running"]
    assert not up.continuation_due(session)


@pytest.mark.parametrize("unrecoverable", ["disabled", "stale", "exhausted", "live_writer"])
def test_durable_hold_does_not_starve_unrecoverable_or_live_writer(recovery, monkeypatch, unrecoverable):
    af.set_hold(recovery.home)
    sid = mount()["session_id"]
    af.clear_hold(recovery.home)
    session = server._sessions[sid]
    assert up.continuation_due(session)
    if unrecoverable == "disabled":
        monkeypatch.setattr(server, "_load_cfg", lambda: {"desktop": {"auto_continue": {"enabled": False}}})
    elif unrecoverable == "stale":
        monkeypatch.setattr(up, "_now", lambda: time.time() + 24 * 3600)
    elif unrecoverable == "exhausted":
        monkeypatch.setattr(server, "_auto_continue_config", lambda: (True, 24 * 3600, 0))
    else:
        record_turn_start(recovery.home, "paused", "finish the request")
        mark_turn_paused(recovery.home, "paused", up.PAUSE_REASON)
    assert not up.continuation_due(session)


def test_connected_desktop_reattach_retries_after_admission_hold(recovery):
    af.set_hold(recovery.home)
    first = mount()
    assert not first.get("auto_continue")
    af.clear_hold(recovery.home)
    second = mount()  # same live record, no prompt.submit / notification
    assert second["session_id"] == first["session_id"]
    assert second.get("auto_continue")
    assert_continued(recovery)


def test_old_connected_backend_waits_for_new_head(recovery, monkeypatch):
    # The old runtime wrote this marker itself and is still connected after the swap.
    record_turn_start(recovery.home, "paused", "finish the request")
    mark_turn_paused(recovery.home, "paused", up.PAUSE_REASON)
    af.set_hold(recovery.home)
    first = mount()
    af.clear_hold(recovery.home)
    monkeypatch.setattr(up, "_detect_skew", lambda: ("old-head", "new-head"))
    second = mount()
    assert second["session_id"] == first["session_id"]
    assert not second.get("auto_continue")
    assert recovery.calls == []
    # Replacement runtime, once the checkout and loaded revision match.
    monkeypatch.setattr(up, "_detect_skew", lambda: None)
    assert mount().get("auto_continue")
    assert_continued(recovery)


@pytest.mark.parametrize("flag", ["running", "resume_hydrating", "_closing", "_finalized"])
def test_live_record_not_ready_for_recovery_is_left_alone(recovery, flag):
    af.set_hold(recovery.home)
    first = mount()
    af.clear_hold(recovery.home)
    session = server._sessions[first["session_id"]]
    session[flag] = True
    if flag == "_finalized":
        assert server._maybe_schedule_auto_continue(first["session_id"], session, "paused") is None
    else:
        assert not mount().get("auto_continue")
    assert recovery.calls == []
    session[flag] = False
    assert mount().get("auto_continue")
    assert_continued(recovery)


def test_startup_sweep_retries_warm_record_after_hold(recovery):
    af.set_hold(recovery.home)
    first = sr.resume_interrupted_sessions(server._methods["session.resume"], recovery.home, CFG)
    assert first[0]["scheduled"] is False
    af.clear_hold(recovery.home)
    second = sr.resume_interrupted_sessions(server._methods["session.resume"], recovery.home, CFG)
    assert second[0]["scheduled"] is True
    assert_continued(recovery)


@pytest.mark.parametrize("obstacle", ["hold", "live_writer"])
def test_boot_worker_retries_without_another_client_message(recovery, monkeypatch, obstacle):
    from hermes_cli import config
    import hermes_constants
    from agent import memory_provider

    af.set_hold(recovery.home)
    live = [obstacle == "live_writer"]
    if live[0]:
        af.clear_hold(recovery.home)
        monkeypatch.setattr(sr, "marker_writer_state", lambda marker: "alive" if live[0] else "dead")
    monkeypatch.setattr(config, "load_config", lambda: CFG)
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: recovery.home)
    ticks = []
    def sleep(seconds):
        ticks.append(seconds)
        if len(ticks) == 2:
            af.clear_hold(recovery.home)
            live[0] = False
    monkeypatch.setattr(sr, "time", types.SimpleNamespace(sleep=sleep, monotonic=time.monotonic,
                                                       time=time.time))
    # Keep the worker synchronous; continuation dispatch still uses a real admitted thread.
    class Inline:
        def start(self):
            self.target()
    def spawn(target, **kwargs):
        thread = Inline()
        thread.target = target
        return thread
    monkeypatch.setattr(memory_provider, "spawn_context_thread", spawn)
    # Older implementation uses Thread directly; capture only the sweep, not dispatch threads.
    real_thread = threading.Thread
    def thread_factory(*args, **kwargs):
        return spawn(kwargs["target"]) if kwargs.get("name") == "startup-resume-sweep" else real_thread(*args, **kwargs)
    monkeypatch.setattr(sr.threading, "Thread", thread_factory)
    sr.start_startup_resume_sweep(stop=recovery.done)
    assert_continued(recovery)


def test_in_place_watcher_retries_until_hold_lifts(recovery, monkeypatch):
    af.set_hold(recovery.home)
    result = mount()
    session = server._sessions[result["session_id"]]
    monkeypatch.setattr(up, "_detect_skew", lambda: None)
    # Run the actual watch loop without real poll delays.
    ticks = []
    def sleep(seconds):
        ticks.append(seconds)
        if len(ticks) == 2:
            af.clear_hold(recovery.home)
    monkeypatch.setattr(up, "time", types.SimpleNamespace(sleep=sleep, monotonic=time.monotonic))
    watcher = up.start_in_place_watcher(result["session_id"], session, "paused")
    watcher.join(5)
    assert not watcher.is_alive()
    assert len(ticks) >= 2
    assert_continued(recovery)


# ── The checkout moved: hand off to a successor only while one is actually coming (t_01a18300e42c3364) ──


class _VirtualClock:
    """Monotonic time that only moves when the watcher sleeps; ``on_tick`` runs after each sleep."""

    def __init__(self, on_tick=None):
        self.now, self.on_tick = 0.0, on_tick

    def sleep(self, seconds):
        self.now += seconds
        if self.on_tick is not None:
            self.on_tick(self.now)

    def monotonic(self):
        return self.now


@pytest.fixture
def skewed(recovery, monkeypatch):
    """A paused turn on a backend whose checkout moved, with a fresh retirement fence per test."""
    from hermes_cli import backend_retirement, web_server_idle_proof, web_server_skew_exit
    fence = backend_retirement.RetirementFence()
    monkeypatch.setattr(backend_retirement, "retirement", fence)
    monkeypatch.setattr(up, "_detect_skew", lambda: ("boot-rev", "disk-rev"))
    monkeypatch.setattr(web_server_skew_exit, "_update_in_progress", lambda: False)
    state = types.SimpleNamespace(armed=True, idle=True, fence=fence)
    monkeypatch.setattr(web_server_skew_exit, "skew_watchdog_armed", lambda: state.armed)
    monkeypatch.setattr(web_server_idle_proof, "idle_proof", lambda *a, **kw: {"idle": state.idle})
    return state


def _watch(monkeypatch, recovery, clock, *, max_s=None):
    if max_s is not None:
        monkeypatch.setattr(up, "WATCH_MAX_S", max_s)
    monkeypatch.setattr(up, "time", types.SimpleNamespace(sleep=clock.sleep, monotonic=clock.monotonic,
                                                          time=time.time))
    af.set_hold(recovery.home)  # a live backend's paused session: mounting it must not continue it yet
    result = mount()
    af.clear_hold(recovery.home)
    session = server._sessions[result["session_id"]]
    watcher = up.start_in_place_watcher(result["session_id"], session, "paused")
    watcher.join(10)
    assert not watcher.is_alive()
    return result["session_id"]


def test_no_replacement_coming_continues_in_place_within_the_bound(recovery, skewed, monkeypatch):
    # Another chat keeps this backend busy, so its skew watchdog cannot retire it: nobody replaces it.
    skewed.idle = False
    clock = _VirtualClock()
    _watch(monkeypatch, recovery, clock)
    assert_continued(recovery)
    # The first poll saw the move; the turn continued one bound later, not after the old 900 s grace.
    assert clock.now <= up.WATCH_POLL_S + up.NO_REPLACEMENT_S + up.WATCH_POLL_S
    assert clock.now < up.LEAVE_GRACE_S


def test_no_retirement_watchdog_continues_in_place_at_once(recovery, skewed, monkeypatch):
    skewed.armed = False  # e.g. a stdio TUI: nothing in this process ever retires it
    clock = _VirtualClock()
    _watch(monkeypatch, recovery, clock)
    assert_continued(recovery)
    assert clock.now == up.WATCH_POLL_S


def test_skew_with_a_replacement_coming_does_not_continue_in_place(recovery, skewed, monkeypatch):
    # Idle with a live watchdog: retirement is imminent, so the successor owns the continuation for as long
    # as a watchdog needs to begin retiring an idle backend.
    clock = _VirtualClock()
    _watch(monkeypatch, recovery, clock, max_s=up.HANDOFF_WAIT_S - up.WATCH_POLL_S)
    assert clock.now >= up.NO_REPLACEMENT_S
    assert recovery.calls == [] and not recovery.done.is_set()


def test_replacement_outlook_that_never_retires_continues_within_the_hand_off_bound(recovery, skewed, monkeypatch):
    # 2nd attempt (live 2026-10-28 17:22): the idle proof held but no retirement began; the turn used to sit
    # until the 300 s backstop. Not even preparing two watchdog polls after the move means nobody is coming.
    clock = _VirtualClock()
    _watch(monkeypatch, recovery, clock)
    assert_continued(recovery)
    assert up.HANDOFF_WAIT_S <= clock.now <= up.WATCH_POLL_S + up.HANDOFF_WAIT_S + up.WATCH_POLL_S


def test_always_on_successor_still_warming_keeps_the_turn_for_it_past_the_hand_off_bound(recovery, skewed,
                                                                                         monkeypatch):
    # Review B1 of #1611 (t_01a1d09123dbb9c4): an always-on blue/green backend's successor boots and warms
    # before the old backend can retire (live: ~60-150 s), so HANDOFF_WAIT_S used to continue the paused turn
    # here on stale code. While the superseded watchdog reports the successor on its way, the turn waits.
    from hermes_cli import web_server_skew_exit
    monkeypatch.setattr(web_server_skew_exit, "successor_pending", lambda: True)
    clock = _VirtualClock()
    _watch(monkeypatch, recovery, clock, max_s=up.SUCCESSOR_WARMING_GRACE_S - up.WATCH_POLL_S)
    assert clock.now > up.HANDOFF_WAIT_S + up.WATCH_POLL_S
    assert recovery.calls == [] and not recovery.done.is_set()


def test_always_on_successor_that_never_switches_in_is_bounded(recovery, skewed, monkeypatch):
    from hermes_cli import web_server_skew_exit
    monkeypatch.setattr(web_server_skew_exit, "successor_pending", lambda: True)
    clock = _VirtualClock()
    _watch(monkeypatch, recovery, clock)
    assert_continued(recovery)
    assert up.SUCCESSOR_WARMING_GRACE_S <= clock.now <= up.SUCCESSOR_WARMING_GRACE_S + 2 * up.WATCH_POLL_S


def test_always_on_successor_does_not_hold_a_busy_backend(recovery, skewed, monkeypatch):
    # Other work keeps the old backend busy, so it cannot retire even once the successor serves: as before,
    # the turn continues here after NO_REPLACEMENT_S.
    from hermes_cli import web_server_skew_exit
    monkeypatch.setattr(web_server_skew_exit, "successor_pending", lambda: True)
    skewed.idle = False
    clock = _VirtualClock()
    _watch(monkeypatch, recovery, clock)
    assert_continued(recovery)
    assert clock.now <= up.WATCH_POLL_S + up.NO_REPLACEMENT_S + up.WATCH_POLL_S


def test_fluctuating_outlook_counts_from_the_move_not_from_the_last_flip(recovery, skewed, monkeypatch):
    # The outlook flips between coming and blocked every poll: the bound still counts from the move.
    def flip(now):
        skewed.idle = not skewed.idle
    clock = _VirtualClock(on_tick=flip)
    _watch(monkeypatch, recovery, clock)
    assert_continued(recovery)
    assert clock.now <= up.WATCH_POLL_S + up.NO_REPLACEMENT_S + 2 * up.WATCH_POLL_S


def test_automatic_turn_waits_for_the_paused_turn_once_the_window_is_over(recovery, skewed, monkeypatch):
    # Live 2026-10-28 17:22:19 → 17:30:39: a kanban notice claimed the idle paused session 2 s after the
    # checkout moved, ran 8 min over the pause and kept the backend busy. Now it is refused (callers requeue)
    # until the paused turn's continuation has started.
    session_box, refused = {}, []
    def notice(now):
        session = session_box.get("session")
        if session is not None and now <= up.HANDOFF_WAIT_S:
            refused.append(not server._notif_claim_turn(session))
            assert not session.get("running")
    clock = _VirtualClock(on_tick=notice)
    real_start = up.start_in_place_watcher
    def start(sid, session, key):
        session_box["session"] = session
        return real_start(sid, session, key)
    monkeypatch.setattr(up, "start_in_place_watcher", start)
    _watch(monkeypatch, recovery, clock)
    assert refused and all(refused)
    assert_continued(recovery)
    assert clock.now <= up.WATCH_POLL_S + up.HANDOFF_WAIT_S + up.WATCH_POLL_S
    # The hold is bounded: a kickoff that never records the continuation's marker frees automatic turns again.
    assert up.continuation_due(session_box["session"])
    clock.sleep(up.KICKOFF_HOLD_S + 1)
    assert not up.continuation_due(session_box["session"])


def test_a_continuation_that_never_schedules_stops_holding_automatic_turns(recovery, skewed, monkeypatch):
    # e.g. a live foreign marker writer refuses every schedule: the watcher keeps retrying, but the chat's
    # notices, loop ticks and heartbeats wait at most HOLD_MAX_S past the window's end, not 6 h.
    skewed.armed = False
    monkeypatch.setattr(server, "_maybe_schedule_auto_continue", lambda *a, **kw: None)
    session_box, due = {}, []
    def probe(now):
        if (session := session_box.get("session")) is not None:
            due.append((now, up.continuation_due(session)))
    clock = _VirtualClock(on_tick=probe)
    real_start = up.start_in_place_watcher
    def start(sid, session, key):
        session_box["session"] = session
        return real_start(sid, session, key)
    monkeypatch.setattr(up, "start_in_place_watcher", start)
    _watch(monkeypatch, recovery, clock, max_s=up.HOLD_MAX_S + 60)
    assert any(d for t, d in due if t <= up.HOLD_MAX_S)
    assert not any(d for t, d in due if t >= up.HOLD_MAX_S + 3 * up.WATCH_POLL_S)


def test_synthesized_turn_over_the_pause_then_continues_at_once(recovery, skewed, monkeypatch):
    # A notice already running over the pause when the window ends: the move is clocked while it runs, so
    # the paused turn continues on the first poll after it, not one more hand-off wait (or backstop) later.
    session_box = {}
    def carried(now):
        session = session_box.get("session")
        if session is None:
            return
        with session["history_lock"]:
            if now <= 480:
                session["running"], session["_paused_marker_carried"] = True, "paused"
            elif session.get("_paused_marker_carried"):
                session["running"] = False
                session.pop("_paused_marker_carried")
                session_box["ended"] = now
    clock = _VirtualClock(on_tick=carried)
    real_start = up.start_in_place_watcher
    def start(sid, session, key):
        session_box["session"] = session
        return real_start(sid, session, key)
    monkeypatch.setattr(up, "start_in_place_watcher", start)
    _watch(monkeypatch, recovery, clock, max_s=up.LEAVE_GRACE_S * 3)
    assert_continued(recovery)
    assert clock.now <= session_box["ended"] + up.WATCH_POLL_S


def test_requested_replacement_hands_off_and_the_successor_continues_once(recovery, skewed, monkeypatch):
    # The watchdog proves this backend idle and commits retirement: admission closes for good.
    def retire(now):
        if now >= 3 * up.WATCH_POLL_S and not skewed.fence.retiring():
            permit = skewed.fence.prepare()
            assert permit["ok"] and skewed.fence.commit(permit["token"])["ok"]
            skewed.idle = False  # a busy-looking ledger must not make the watcher grab the turn back
    clock = _VirtualClock(on_tick=retire)
    sid = _watch(monkeypatch, recovery, clock, max_s=up.LEAVE_GRACE_S * 2)
    # Even past every bound the retired backend never continued the turn here.
    assert recovery.calls == [] and not recovery.done.is_set()
    # The successor process: fresh fence, no live session, its startup sweep continues the turn once.
    from hermes_cli import backend_retirement
    monkeypatch.setattr(backend_retirement, "retirement", backend_retirement.RetirementFence())
    server._sessions.pop(sid, None)
    monkeypatch.setattr(up, "_detect_skew", lambda: None)
    resumed = sr.resume_interrupted_sessions(server._methods["session.resume"], recovery.home, CFG)
    assert resumed[0]["scheduled"] is True
    assert_continued(recovery)


# ── A continuation runs on the session's STORED source, never the absent client's (t_01a183149a00c941) ──


@pytest.fixture
def tui_env(monkeypatch):
    """A backend whose environment resolves the TUI surface: the fallback a client-less resume used to take."""
    monkeypatch.delenv("HERMES_DESKTOP", raising=False)
    monkeypatch.delenv("HERMES_DESKTOP_TERMINAL", raising=False)
    assert server._resolve_session_platform() == "tui"


def test_startup_resume_of_a_desktop_session_continues_as_desktop(recovery, tui_env):
    resumed = sr.resume_interrupted_sessions(server._methods["session.resume"], recovery.home, CFG)
    assert resumed[0]["scheduled"] is True
    assert_continued(recovery)
    assert recovery.platforms == ["desktop"]
    # The record keeps the stored surface for the turns the user sends after opening it, too.
    (session,) = [s for s in server._sessions.values() if s.get("session_key") == "paused"]
    assert session["source"] == "desktop"


@pytest.mark.parametrize("recovery", ["tui"], indirect=True)
def test_startup_resume_of_a_tui_session_stays_tui(recovery, monkeypatch):
    monkeypatch.setenv("HERMES_DESKTOP", "1")  # a Desktop backend: the env default would say desktop
    monkeypatch.delenv("HERMES_DESKTOP_TERMINAL", raising=False)
    resumed = sr.resume_interrupted_sessions(server._methods["session.resume"], recovery.home, CFG)
    assert resumed[0]["scheduled"] is True
    assert_continued(recovery)
    assert recovery.platforms == ["tui"]


@pytest.mark.parametrize("recovery", ["telegram"], indirect=True)
def test_startup_resume_of_an_unknown_source_keeps_the_default(recovery, tui_env):
    sr.resume_interrupted_sessions(server._methods["session.resume"], recovery.home, CFG)
    assert_continued(recovery)
    assert recovery.platforms == ["tui"]


def _in_place_with_built_agent(recovery, monkeypatch, agent_platform):
    """A live record whose agent was already built (it ran the paused turn) on ``agent_platform``, mounted by a
    client that named no source (e.g. a bot-plugin resume), then continued by the in-place watcher."""
    af.set_hold(recovery.home)
    response = server.handle_request({"id": "resume", "method": "session.resume", "params": {
        "session_id": "paused", "omit_messages": True}})
    af.clear_hold(recovery.home)
    sid = response["result"]["session_id"]
    session = server._sessions[sid]
    session["agent"] = types.SimpleNamespace(platform=agent_platform, _session_db=None)
    session["agent_ready"].set()
    rebuilds = []

    def rebuild(rsid, rsession, **kw):
        rebuilds.append(kw.get("platform_override"))
        rsession["agent"] = types.SimpleNamespace(platform=kw.get("platform_override"), _session_db=None)
        return rsession["agent"]
    monkeypatch.setattr(server, "_rebuild_session_agent", rebuild)
    monkeypatch.setattr(up, "_detect_skew", lambda: None)
    clock = _VirtualClock()
    monkeypatch.setattr(up, "time", types.SimpleNamespace(sleep=clock.sleep, monotonic=clock.monotonic,
                                                          time=time.time))
    watcher = up.start_in_place_watcher(sid, session, "paused")
    watcher.join(10)
    assert not watcher.is_alive()
    assert_continued(recovery)
    return session, rebuilds


def test_in_place_continuation_of_a_desktop_session_runs_as_desktop(recovery, tui_env, monkeypatch):
    session, rebuilds = _in_place_with_built_agent(recovery, monkeypatch, "tui")
    assert recovery.platforms == ["desktop"]
    assert rebuilds == ["desktop"]  # the tui-built agent is replaced before the continuation runs
    assert session["agent"].platform == "desktop"


@pytest.mark.parametrize("recovery", ["tui"], indirect=True)
def test_in_place_continuation_of_a_tui_session_stays_tui(recovery, tui_env, monkeypatch):
    _session, rebuilds = _in_place_with_built_agent(recovery, monkeypatch, "tui")
    assert recovery.platforms == ["tui"]
    assert rebuilds == []  # already right: no rebuild
