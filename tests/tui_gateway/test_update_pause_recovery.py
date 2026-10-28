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
def recovery(tmp_path, monkeypatch):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("paused", source="desktop")
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
    calls, done = [], threading.Event()
    def submit(rid, sid, session, text, **kw):
        calls.append((text, kw))
        record_turn_start(tmp_path, "paused", "finish the request", pauses=1)
        done.set()
    monkeypatch.setattr(server, "_run_prompt_submit", submit)
    before = set(server._sessions)
    yield types.SimpleNamespace(home=tmp_path, calls=calls, done=done)
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
