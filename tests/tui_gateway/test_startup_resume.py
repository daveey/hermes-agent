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


def test_enabled_by_default_and_explicit_false_disables():
    assert sr._settings({})[0] is True  # [daveey] session-outcome contract: on unless turned off
    assert sr._settings({"desktop": {"auto_continue": {"resume_on_startup": False}}})[0] is False
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
    # source_from_store: no client is attached, so the resume runs as the stored session source.
    assert calls == [{"session_id": "b", "omit_messages": True, "source_from_store": True, "profile": "work"},
                     {"session_id": "a", "omit_messages": True, "source_from_store": True}]
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


def test_lifetime_observer_retries_an_ordinary_interrupted_marker(tmp_path, monkeypatch):
    import threading
    from types import SimpleNamespace

    from gateway import code_skew
    from hermes_cli import config

    stop = threading.Event()
    calls = []
    waits = []
    _write(tmp_path, {"ordinary": _entry(pid=os.getpid())})
    assert sr.pending_resumes(tmp_path, CFG) == [], "boot scan must leave the live writer alone"

    def sleep(seconds):
        waits.append(seconds)
        if len(waits) == 1:
            _write(tmp_path, {"ordinary": _entry()})
        else:
            stop.set()

    def resume(rid, params):
        calls.append(params)
        sr.clear_turn_marker(tmp_path, params["session_id"])
        return {"result": {"auto_continue": {"attempt": 1}}}

    monkeypatch.setattr(sr, "time", SimpleNamespace(sleep=sleep, time=time.time))
    monkeypatch.setattr(config, "load_config", lambda: CFG)
    monkeypatch.setattr(code_skew, "detect_code_skew", lambda: None)
    sr._observe_markers(resume, tmp_path, CFG, stop)
    assert calls == [{"session_id": "ordinary", "omit_messages": True, "source_from_store": True}]
    assert waits == [5.0, 30.0]
    assert _markers(tmp_path) == set()


def test_start_is_noop_when_disabled(monkeypatch):
    from hermes_cli import config
    monkeypatch.setattr(config, "load_config", lambda: {"desktop": {"auto_continue": {"resume_on_startup": False}}})
    assert sr.start_startup_resume_sweep() is None


# ── Deleted chats (desktop-03cd4d73b3) ─────────────────────────────────────────────────────────────
# A chat deleted after its writer exited kept its interrupted-turn marker, so every boot sweep in the
# freshness window resumed it again. A ``session_deleted`` refusal is terminal: the sweep retires that
# marker (in the marker's own profile home) and logs an informational skip. Any other failure (an
# unreadable store, a plain 4007 for a merely absent row) keeps the marker for the next sweep.

DELETED = {"error": {"code": 4007, "message": "session not found", "data": {"reason": "session_deleted"}}}


def _markers(home):
    p = home / "desktop" / "interrupted_turns.json"
    return set(json.loads(p.read_text())) if p.exists() else set()


def test_deleted_session_marker_is_retired_and_not_resumed_again(tmp_path, caplog):
    _write(tmp_path, {"gone": _entry(age=10), "kept": _entry(age=20)})
    calls = []

    def resume(rid, params):
        calls.append(params["session_id"])
        return DELETED if params["session_id"] == "gone" else {"result": {"auto_continue": {"attempt": 1}}}

    caplog.set_level("INFO")
    out = sr.resume_interrupted_sessions(resume, tmp_path, CFG)

    by_key = {r["session_key"]: r for r in out}
    assert by_key["gone"].get("deleted") is True
    assert "gone" not in _markers(tmp_path), "a deleted chat's marker is retired"
    assert "kept" in _markers(tmp_path)
    assert not [r for r in caplog.records if r.levelno >= 30]
    assert any("deleted" in r.getMessage() and "gone" in r.getMessage() for r in caplog.records)

    calls.clear()
    sr.resume_interrupted_sessions(resume, tmp_path, CFG)
    assert "gone" not in calls, "no repeated recovery of a deleted chat"


def test_deleted_session_marker_retired_in_its_named_profile_home(tmp_path):
    _write(tmp_path, {"gone": _entry(age=10)})  # same key in the launch home is NOT deleted
    work = tmp_path / "profiles" / "work"
    _write(work, {"gone": _entry(age=5)})

    def resume(rid, params):
        return DELETED if params.get("profile") == "work" else {"error": {"code": 5000, "message": "db unavailable"}}

    sr.resume_interrupted_sessions(resume, tmp_path, CFG)
    assert "gone" not in _markers(work)
    assert "gone" in _markers(tmp_path), "an unreadable store is not proof of deletion"


def test_plain_not_found_is_not_proof_of_deletion(tmp_path):
    _write(tmp_path, {"absent": _entry(age=10)})
    sr.resume_interrupted_sessions(lambda rid, p: {"error": {"code": 4007, "message": "session not found"}},
                                   tmp_path, CFG)
    assert "absent" in _markers(tmp_path)
