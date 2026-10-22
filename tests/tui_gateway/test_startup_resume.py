"""desktop.auto_continue.resume_on_startup: the backend resumes every interrupted Desktop turn at boot."""

import json
import os
import time

from tui_gateway import startup_resume as sr

DEAD_PID = 2 ** 22 - 3  # far above macOS/Linux pid_max defaults: never a live process


def _write(home, entries):
    p = home / "desktop" / "interrupted_turns.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(entries))


def _entry(prompt="do the thing", age=60, attempts=0, pid=DEAD_PID, auto=True):
    return {"prompt": prompt, "started_at": time.time() - age, "attempts": attempts, "auto_continue": auto,
            "writer_pid": pid}


CFG = {"desktop": {"auto_continue": {"enabled": True, "resume_on_startup": True, "freshness_minutes": 60,
                                     "max_attempts": 2}}}


def test_disabled_by_default():
    assert sr._settings({})[0] is False
    assert sr._settings(CFG)[0] is True
    off = {"desktop": {"auto_continue": {"enabled": False, "resume_on_startup": True}}}
    assert sr._settings(off)[0] is False


def test_filters_stale_capped_live_and_opted_out(tmp_path):
    _write(tmp_path, {
        "fresh": _entry(age=60),
        "stale": _entry(age=2 * 3600),
        "capped": _entry(attempts=2),
        "live": _entry(pid=os.getpid()),
        "mailbox": _entry(auto=False),
        "empty": _entry(prompt="  "),
    })
    keys = [r["session_key"] for r in sr.pending_resumes(tmp_path, CFG)]
    assert keys == ["fresh"]


def test_profiles_are_swept_with_profile_param(tmp_path):
    _write(tmp_path, {"a": _entry(age=30)})
    _write(tmp_path / "profiles" / "work", {"b": _entry(age=10)})
    calls = []

    def resume(rid, params):
        calls.append(params)
        return {"result": {"auto_continue": {"attempt": 1}}}

    out = sr.resume_interrupted_sessions(resume, tmp_path, CFG)
    assert calls == [{"session_id": "b", "omit_messages": True, "profile": "work"},
                     {"session_id": "a", "omit_messages": True}]
    assert all(r["ok"] and r["scheduled"] for r in out)


def test_one_failure_does_not_stop_the_sweep(tmp_path):
    _write(tmp_path, {"x": _entry(age=10), "y": _entry(age=20)})
    seen = []

    def resume(rid, params):
        seen.append(params["session_id"])
        if params["session_id"] == "x":
            raise RuntimeError("boom")
        return {"error": {"code": 4007, "message": "session not found"}}

    out = sr.resume_interrupted_sessions(resume, tmp_path, CFG)
    assert seen == ["x", "y"]
    assert [r["ok"] for r in out] == [False, False]


def test_start_is_noop_when_disabled(monkeypatch):
    from hermes_cli import config
    monkeypatch.setattr(config, "load_config", dict)
    assert sr.start_startup_resume_sweep() is None
