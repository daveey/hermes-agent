"""R4-F F4b: lifespan sweepers reserve before agent-producing work; idle polling stays un-reserved."""
import contextlib
import json
import threading
import time
from pathlib import Path

import pytest

import hermes_state_producer_admission as pa
from hermes_cli import local_work
from hermes_cli.local_work import Registry
from hermes_constants import get_default_hermes_root, get_hermes_home, hermes_home_key
from hermes_state import SessionDB
from tests.waiting import wait_until
from tui_gateway import kanban_unloaded as ku
from tui_gateway import producer_admission as delivery
from tui_gateway import server
from tui_gateway import startup_resume as sr

SID = "20261022_204322_4d8ce1"
PRODUCER = json.dumps(["user-terminal", "job-1", "final"], separators=(",", ":"))
TEXT = "[User-terminal completion - tool result, not user input]\njob_id: job-1\nstatus: exited\nexit_code: 0"


@pytest.fixture
def registry(monkeypatch):
    reg = Registry(homes=[hermes_home_key(get_hermes_home())])
    monkeypatch.setattr(local_work, "_registry", reg)
    return reg


def _write_marker(home, key="k1"):
    p = home / "desktop" / "interrupted_turns.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({key: {"prompt": "do it", "started_at": time.time() - 60, "attempts": 0,
                                   "auto_continue": True, "writer_pid": 2 ** 22 - 3}}))


CFG = {"desktop": {"auto_continue": {"enabled": True, "resume_on_startup": True, "freshness_minutes": 60,
                                     "max_attempts": 2}}}


def test_startup_resume_reserves_around_each_resume_and_idle_scan_is_free(registry, tmp_path):
    epoch = registry.snapshot().epoch
    assert sr.resume_interrupted_sessions(lambda rid, p: {}, tmp_path, CFG) == []  # no markers: pure read
    assert registry.snapshot().epoch == epoch and registry.snapshot().active == 0
    _write_marker(tmp_path)
    seen = []

    def resume(rid, params):
        seen.append(registry.snapshot().active)
        return {"result": {"auto_continue": {"attempt": 1}}}

    [row] = sr.resume_interrupted_sessions(resume, tmp_path, CFG)
    assert row["ok"] and seen == [1]  # reserved BEFORE session.resume's build/prompt prep
    assert registry.snapshot().active == 0
    assert "unmatched-finish" not in registry.snapshot().unknowns


def test_kanban_sweep_spawn_reserves_before_thread_and_thread_keeps_custody(registry):
    gate, inside = threading.Event(), threading.Event()
    seen = []

    def job():
        seen.append(registry.snapshot().active)
        inside.set()
        assert gate.wait(10)
        return "started"

    assert ku._spawn_thread(job) == "dispatched"
    assert inside.wait(5)
    assert registry.snapshot().active >= 1  # held by the live wake thread, not by the sweeper loop
    assert seen[0] >= 1
    gate.set()
    wait_until(lambda: registry.snapshot().active == 0, what="wake thread exit release")
    assert "unmatched-finish" not in registry.snapshot().unknowns


def test_delegation_sweep_spawn_reserves_before_thread_and_thread_keeps_custody(registry):
    from tui_gateway import delegation_unloaded as du
    gate, inside = threading.Event(), threading.Event()
    seen = []
    epoch = registry.snapshot().epoch

    def job():
        seen.append(registry.snapshot().active)
        inside.set()
        assert gate.wait(10)

    assert du._spawn_thread(job) == "dispatched"
    assert inside.wait(5)
    assert registry.snapshot().active >= 1 and seen[0] >= 1
    assert registry.snapshot().epoch != epoch
    gate.set()
    wait_until(lambda: registry.snapshot().active == 0, what="delegation wake thread exit release")
    assert "unmatched-finish" not in registry.snapshot().unknowns


@pytest.fixture
def producer_env(monkeypatch):
    root = Path(get_default_hermes_root())
    root.mkdir(parents=True, exist_ok=True)
    db = SessionDB(root / "state.db")
    db.create_session(SID, "desktop")
    db.append_message(SID, "user", "run the build")
    db.append_message(SID, "assistant", "Started it.")

    @contextlib.contextmanager
    def opener(session):
        yield db

    monkeypatch.setattr(server, "_session_db", opener)
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda sid, session, **kw: None)
    yield root
    db.close()


def test_producer_sweep_reserves_resume_and_delivery_and_idle_sweep_is_free(registry, producer_env, monkeypatch):
    epoch = registry.snapshot().epoch
    assert delivery.sweep_once() == []  # nothing queued: passive poll, zero reservations
    assert registry.snapshot().epoch == epoch
    seen = {}
    live = {}

    def resume(rid, params):
        seen["resume"] = registry.snapshot().active
        session = {"session_key": params["session_id"], "history_lock": threading.RLock(), "running": False,
                   "agent": object()}
        live["ui"] = session
        return {"result": {"session_id": "ui"}}

    def submit(rid, sid, session, text, **kwargs):
        seen["submit"] = registry.snapshot().active
        with session["history_lock"]:
            session["running"] = False
        return True

    monkeypatch.setitem(server._methods, "session.resume", resume)
    monkeypatch.setattr(server, "_find_live_session_by_key", lambda key, profile_home=None: (
        ("ui", live["ui"]) if "ui" in live else None))
    monkeypatch.setattr(server, "_run_prompt_submit", submit)
    pa.admit_producer_completion(producer=PRODUCER, profile_home=producer_env, stored_session_id=SID, text=TEXT)

    [rec] = delivery.sweep_once()
    assert rec["verdict"] == "started"
    assert seen == {"resume": 1, "submit": 1}  # both reserved before the work they start
    assert registry.snapshot().active == 0
