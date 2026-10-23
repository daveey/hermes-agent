"""``server._wake_session_turn``: one agent turn in a chat without the user typing. [daveey]

Plugins use it to close a loop (e.g. chat-closeout: all of a chat's kanban cards finished while no backend held
it). A chat live here is used as is; otherwise it is resumed in-process first, like the re-adopted-exit waker.
"""

from __future__ import annotations

import threading

import pytest

from tui_gateway import server

KEY = "20261022_170000_abcdef"


def _session(**kw) -> dict:
    ready = threading.Event()
    ready.set()
    return {"history_lock": threading.RLock(), "running": False, "agent_ready": ready, "session_key": KEY,
            "profile_home": None, **kw}


@pytest.fixture
def wired(monkeypatch, tmp_path):
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    live: dict = {}
    calls: dict = {"resume": [], "submit": [], "released": 0}
    monkeypatch.setattr(server, "_find_live_session_by_key",
                        lambda key, profile_home=None: (live["sid"], live["session"]) if live and key == KEY else None)
    monkeypatch.setattr(server, "_start_agent_build", lambda sid, session: None)
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda sid, session: None)

    def submit(rid, sid, session, text, what, **kwargs):
        calls["submit"].append((sid, text, kwargs))
        return True

    monkeypatch.setattr(server, "_notif_submit", submit)
    real_release = server._notif_release_turn

    def release(session):
        calls["released"] += 1
        real_release(session)

    monkeypatch.setattr(server, "_notif_release_turn", release)
    return live, calls


def test_unloaded_chat_is_resumed_then_runs_one_turn(wired, monkeypatch):
    live, calls = wired

    def resume(rid, params):
        calls["resume"].append(params)
        live.update(sid="sid-1", session=_session())
        return {"result": {"session_id": "sid-1"}}

    monkeypatch.setitem(server._methods, "session.resume", resume)
    got = server._wake_session_turn(KEY, "close out", display_kind="kanban_notice",
                                    display_metadata={"display_text": "x"}, label="closeout")
    assert got == "started"
    assert calls["resume"] and calls["resume"][0]["session_id"] == KEY and calls["resume"][0]["omit_messages"]
    assert calls["submit"] == [("sid-1", "close out",
                                {"display_kind": "kanban_notice", "display_metadata": {"display_text": "x"}})]
    assert live["session"]["running"] is True  # claimed for the turn (the turn thread releases it)


def test_chat_live_here_is_not_resumed(wired, monkeypatch):
    live, calls = wired
    live.update(sid="sid-live", session=_session())
    monkeypatch.setitem(server._methods, "session.resume", lambda rid, params: pytest.fail("resumed a live chat"))
    assert server._wake_session_turn(KEY, "close out") == "started"
    assert calls["submit"][0][0] == "sid-live" and calls["submit"][0][2] == {}


def test_busy_or_stopped_chat_is_left_alone(wired, monkeypatch):
    live, calls = wired
    live.update(sid="s", session=_session(running=True))
    assert server._wake_session_turn(KEY, "close out") == "busy"
    live.update(sid="s", session=_session(_turn_cancel_requested=True))
    assert server._wake_session_turn(KEY, "close out") == "busy"
    assert calls["submit"] == []


def test_resume_refused_by_another_owner(wired, monkeypatch):
    _live, calls = wired
    monkeypatch.setitem(server._methods, "session.resume",
                        lambda rid, params: {"error": {"code": 4011, "message": "open in another Hermes window"}})
    assert server._wake_session_turn(KEY, "close out") == "refused"
    assert calls["submit"] == []


def test_chat_in_no_store_is_missing(wired, monkeypatch):
    monkeypatch.setitem(server._methods, "session.resume",
                        lambda rid, params: {"error": {"code": 4007, "message": "session not found"}})
    assert server._wake_session_turn(KEY, "close out") == "missing"


def test_owner_slot_refusal_releases_the_claim(wired, monkeypatch):
    live, calls = wired
    live.update(sid="s", session=_session())
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda sid, session: "owned elsewhere")
    assert server._wake_session_turn(KEY, "close out") == "refused"
    assert calls["released"] == 1 and live["session"]["running"] is False
    assert calls["submit"] == []
