"""Boot sweep -> real session.resume -> scheduler -> full gateway deadline wait."""
import time

from tests.tui_gateway import test_retry_deadline_gateway as gw
from tui_gateway import server, startup_resume as sr, turn_marker

rig = gw.rig


def test_startup_sweep_runs_deadline_continuation_through_resume(rig, monkeypatch):
    home, make = rig
    calls = []
    agent = make(lambda *a, **k: calls.append(time.time()) or gw.base._response())
    db = agent._session_db
    assert turn_marker.record_turn_start(home, gw.KEY, "finish the request")
    marker = turn_marker.read_turn_marker(home, gw.KEY)
    deadline = time.time() + 4
    assert turn_marker.stamp_provider_retry_deadline(home, gw.KEY, deadline, marker)
    assert turn_marker.mark_turn_paused(home, gw.KEY, "core_update_pause")
    monkeypatch.setattr(sr, "marker_writer_state", lambda marker: "dead")
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_profile_home", lambda p: home)
    monkeypatch.setattr(server, "_profile_session_db", lambda h: (db, False))
    monkeypatch.setattr(server, "_default_session_cwd", lambda: str(home))
    monkeypatch.setattr(server, "_enable_gateway_prompts", lambda: None)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda: None)
    monkeypatch.setattr(server, "_schedule_agent_build", lambda *a: None)

    def build(sid, session):
        session["agent"] = agent

    monkeypatch.setattr(server, "_start_agent_build", build)
    monkeypatch.setattr(server, "_wait_agent", lambda *a, **k: None)
    before = set(server._sessions)
    try:
        result = sr.resume_interrupted_sessions(server._methods["session.resume"], home, gw.base.CFG)
        assert len(result) == 1 and result[0]["scheduled"], result
        gw._join_scheduler()
        sessions = [server._sessions[sid] for sid in set(server._sessions) - before]
        assert len(sessions) == 1
        gw._join_turn(sessions[0])
        assert len(calls) == 1 and calls[0] >= deadline, (calls, deadline)
        assert turn_marker.read_turn_marker(home, gw.KEY) is None
        assert sr.resume_interrupted_sessions(server._methods["session.resume"], home, gw.base.CFG) == []
    finally:
        for sid in set(server._sessions) - before:
            server._sessions.pop(sid, None)
