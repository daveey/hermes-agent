"""[daveey] A paused continuation refused by the REAL update admission (no stubbed submit).

The hub incident (defect gateway-833d39a686) end to end through ``_run_prompt_submit``: the kickoff waits for its
agent build, the drain re-takes the update hold meanwhile, so ``prepare_work`` cannot reserve (returns None) and
the submit's ``stage``/``settle`` give the claim back. The kickoff then logs the refusal, re-arms the pause watcher
and leaves no marker inputs behind: a user turn that starts before the re-armed continuation records its own
marker, not the paused prompt's attempt count or at-most-once flag.
"""

from __future__ import annotations

import logging
import threading
import types

import pytest

from hermes_cli import admission_fence as af, backend_retirement
from tui_gateway import server
from tui_gateway import update_pause as up
from tui_gateway.turn_marker import mark_turn_paused, read_turn_marker, record_turn_start

pytestmark = [pytest.mark.platforms("posix"), pytest.mark.usefixtures("no_background_link_refresh")]

CFG = {"desktop": {"auto_continue": {"enabled": True, "resume_on_startup": True, "freshness_minutes": 240,
                                     "max_attempts": 2}}}


class _InlineThread:
    def __init__(self, target=None, daemon=None, args=(), kwargs=None, name=None):
        self._target, self._args, self._kwargs = target, args, kwargs or {}

    def start(self):
        if self._target is not None:
            self._target(*self._args, **self._kwargs)

    def is_alive(self):
        return False

    def join(self, timeout=None):
        return None


@pytest.fixture()
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(backend_retirement, "retirement", backend_retirement.RetirementFence())
    monkeypatch.setattr(server, "_hermes_home", tmp_path)
    monkeypatch.setattr(server, "_load_cfg", lambda: CFG)
    monkeypatch.setattr(server.threading, "Thread", _InlineThread)
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(server, "_wire_callbacks", lambda sid: None)
    monkeypatch.setattr(server, "_sync_agent_model_with_config", lambda sid, session: None)
    monkeypatch.setattr(server, "_session_cwd", lambda session: str(tmp_path))
    monkeypatch.setattr(server, "_register_session_cwd", lambda session: None)
    monkeypatch.setattr(server, "_tts_stream_begin", lambda: None)
    monkeypatch.setattr(server, "_sync_session_key_after_compress", lambda *a, **k: None)
    monkeypatch.setattr(server, "_get_usage", lambda agent: {})
    monkeypatch.setattr(server, "_start_agent_build", lambda sid, session: None)

    def build_wait(session, rid, timeout=30.0):
        af.set_hold(tmp_path)  # the drain re-takes the hold while the kickoff waits for its agent build

    monkeypatch.setattr(server, "_wait_agent", build_wait)
    watchers: list = []
    monkeypatch.setattr(up, "start_in_place_watcher", lambda sid, session, key: watchers.append(key))
    yield types.SimpleNamespace(home=tmp_path, watchers=watchers)
    af.clear_hold(tmp_path)


def _session(home, seen):
    def run_conversation(message, **kw):
        seen.append(read_turn_marker(home, "session-key"))  # the marker the running turn recorded
        return {"final_response": "done"}

    agent = types.SimpleNamespace(session_id="session-key", clear_interrupt=lambda: None,
                                  run_conversation=run_conversation)
    return {"agent": agent, "session_key": "session-key", "history": [], "history_lock": threading.RLock(),
            "history_version": 0, "running": False, "attached_images": [], "image_counter": 0, "cols": 80,
            "slash_worker": None, "show_reasoning": False, "tool_progress_mode": "all", "inflight_turn": None,
            "profile_home": str(home)}


def test_continuation_refused_by_the_real_admission_hold_releases_and_rearms(env, caplog):
    record_turn_start(env.home, "session-key", "fix the flaky test", attempts=1, until_tool=True)
    mark_turn_paused(env.home, "session-key", up.PAUSE_REASON)
    seen: list = []
    session = _session(env.home, seen)
    caplog.set_level(logging.INFO)

    assert server._maybe_schedule_auto_continue("sid", session, "session-key") is not None

    msgs = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("tui prompt refused") and "cause=admission" in m for m in msgs)
    assert any("continuation of session-key never started" in m for m in msgs)
    assert not seen  # the continuation never ran
    assert env.watchers == ["session-key"]
    # stage/settle gave the claim back: no admission token, no running flag, no reservation left behind.
    assert "_turn_admission" not in session and not session.get("running")
    assert not session.get("_auto_continue_scheduled")
    assert af.state(env.home)["reservations"] == 0
    assert read_turn_marker(env.home, "session-key")["reason"] == up.PAUSE_REASON  # still the turn's record

    # The hold lifts and the user types before the re-armed continuation: their turn's marker is their own.
    af.clear_hold(env.home)
    assert server._run_prompt_submit("rid", "sid", session, "a new question") is True
    assert len(seen) == 1
    marker = seen[0]
    assert marker["prompt"] == "a new question"
    assert marker["attempts"] == 0, "the user turn inherited the continuation's attempt count"
    assert not marker.get("until_tool"), "the user turn inherited the continuation's at-most-once flag"
    assert not marker.get("pauses")
