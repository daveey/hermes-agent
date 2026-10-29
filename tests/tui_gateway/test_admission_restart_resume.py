"""An admitted decision whose turn a backend restart stops before any tool ran is continued at the next boot.

Regression for self-heal desktop-3a6213ce74: a hub update restarted the backend 15 s into an approved For-you
decision (model wait, no tool call). The admission turn recorded its crash marker with ``auto_continue: false``,
so the boot sweep skipped it without a log line and the approval was lost. A decision turn now stays
continuable until its first tool starts; after that, at-most-once execution keeps it out of auto-continue.
"""
import json
import logging

import pytest

from tests.tui_gateway.test_prompt_admission import call, owner
from tui_gateway import server
from tui_gateway import startup_resume as sr
from tui_gateway.turn_marker import _marker_path

DEAD_PID = 2 ** 22 - 3
CFG = {"desktop": {"auto_continue": {"enabled": True, "resume_on_startup": True, "freshness_minutes": 240,
                                     "max_attempts": 2}}}


def _start_admitted_turn(session, params, monkeypatch):
    """prompt.admit, then run its dispatch the way ``_run_after_agent_ready`` -> ``_run_prompt_submit`` records
    the turn marker (prompt_turn.py: ``_record_turn_marker(session, text, auto_continue=...)``)."""
    def run(rid, sid, sess, text, *args, **kw):
        server._record_turn_marker(sess, text, auto_continue=kw.get("auto_continue", True))
    monkeypatch.setattr(server, "_run_after_agent_ready", run)
    assert "result" in call("prompt.admit", params)


def _backend_exit(session):
    """What ``_stop_turns_before_exit`` + the interrupted turn's terminal frame do, then the process dies."""
    session["_exit_keeps_turn_marker"] = True
    server._retire_turn_marker(session)
    path = _marker_path(server._session_home(session))
    entries = json.loads(path.read_text())
    for entry in entries.values():
        entry["writer_pid"] = DEAD_PID
        entry.pop("writer_start_time", None)
    path.write_text(json.dumps(entries))
    return path.parent.parent


def test_backend_exit_marker_survives_and_boot_sweep_continues(owner, monkeypatch):
    _db, session, params, started = owner
    _start_admitted_turn(session, params, monkeypatch)
    started[0]()
    home = _backend_exit(session)
    pending = sr.pending_resumes(home, CFG)
    assert [r["session_key"] for r in pending] == [params["stored_session_id"]]


def test_admitted_turn_is_not_continued_once_a_tool_started(owner, monkeypatch, caplog):
    _db, session, params, started = owner
    _start_admitted_turn(session, params, monkeypatch)
    started[0]()
    sid = next(s for s, v in server._sessions.items() if v is session)
    server._on_tool_start(sid, "call-1", "terminal", {"command": "true"})
    home = _backend_exit(session)
    with caplog.at_level(logging.INFO, logger="tui_gateway.startup_resume"):
        assert sr.pending_resumes(home, CFG) == []
    assert any("skipped" in r.getMessage() and params["stored_session_id"] in r.getMessage()
               and "auto_continue off" in r.getMessage() for r in caplog.records), caplog.text


@pytest.mark.parametrize("why,entry,needle", [
    ("stale", {"age": 9 * 3600}, "older than"),
    ("capped", {"attempts": 2}, "attempt cap"),
])
def test_boot_sweep_logs_every_skipped_marker(tmp_path, caplog, why, entry, needle):
    import time
    m = {"prompt": "p", "started_at": time.time() - entry.get("age", 60), "attempts": entry.get("attempts", 0),
         "auto_continue": True, "writer_pid": DEAD_PID}
    p = _marker_path(tmp_path)
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({why: m}))
    with caplog.at_level(logging.INFO, logger="tui_gateway.startup_resume"):
        assert sr.pending_resumes(tmp_path, CFG) == []
    assert any(why in r.getMessage() and needle in r.getMessage() for r in caplog.records), caplog.text
