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
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *a: None)
    monkeypatch.setattr(server, "_emit", lambda *a, **kw: None)
    calls, done, platforms = [], threading.Event(), []
    def submit(rid, sid, session, text, **kw):
        calls.append((text, kw))
        platforms.append(server._deferred_build_agent_kwargs(session, None)["platform_override"])
        record_turn_start(tmp_path, "paused", "finish the request", pauses=1)
        done.set()
    monkeypatch.setattr(server, "_run_prompt_submit", submit)
    before = set(server._sessions)
    yield types.SimpleNamespace(home=tmp_path, calls=calls, done=done, platforms=platforms)
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
    sr.start_startup_resume_sweep()
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
    # Idle with a live watchdog: retirement is imminent, so the successor owns the continuation.
    clock = _VirtualClock()
    _watch(monkeypatch, recovery, clock, max_s=up.LEAVE_GRACE_S - up.WATCH_POLL_S)
    assert clock.now >= up.NO_REPLACEMENT_S * 3
    assert recovery.calls == [] and not recovery.done.is_set()


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
