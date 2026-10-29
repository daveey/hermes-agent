"""Deferred resume cancellation tolerates a slow controller without hiding deadlocks."""

import threading

from tui_gateway import server


def test_session_resume_deferred_history_close_cancels_build(monkeypatch):
    history_started = threading.Event()
    release_history = threading.Event()
    build_started = threading.Event()
    history_loaded = threading.Event()
    history_workers = []

    class FakeDB:
        def get_session(self, target):
            return {"id": target, "message_count": 1}

        def resolve_resume_session_id(self, target):
            return target

        def reopen_session(self, _target):
            pass

        def get_resume_conversations(self, _target):
            history_workers.append(threading.current_thread())
            history_started.set()
            assert release_history.wait(timeout=60.0)
            history_loaded.set()
            loaded = [{"role": "user", "content": "late"}]
            return loaded, loaded

        def get_ancestor_display_prefix(self, _target):
            return []

    monkeypatch.setattr(server, "_get_db", lambda: FakeDB())
    monkeypatch.setattr(server, "_enable_gateway_prompts", lambda: None)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda: None)
    monkeypatch.setattr(server, "_foreign_turn_owner", lambda _session: None)
    monkeypatch.setattr(server, "_schedule_agent_build", lambda *a, **k: None)
    monkeypatch.setattr(
        server,
        "_start_agent_build",
        lambda _sid, _session: build_started.set(),
    )

    response = {}
    try:
        response = server._methods["session.resume"](
            "r1",
            {"session_id": "cancel-session", "defer_history": True},
        )
        sid = response["result"]["session_id"]
        session = server._sessions[sid]
        assert history_started.wait(timeout=30.0)
        # Exercise a controller delayed beyond the former 2s deadlock guard.
        assert not release_history.wait(timeout=2.1)

        assert server._close_session_by_id(sid, end_reason="tui_close") is True
        assert session["resume_history_ready"].is_set()
        assert session["resume_history_error"] == "session resume cancelled"

        release_history.set()
        history_workers[0].join(timeout=60.0)
        assert not history_workers[0].is_alive(), "history worker was not released"
        assert history_loaded.is_set(), "history read timed out before release"
        assert not build_started.is_set()
        assert sid not in server._sessions
    finally:
        release_history.set()
        for worker in history_workers:
            worker.join(timeout=60.0)
        server._sessions.pop(response.get("result", {}).get("session_id", ""), None)
