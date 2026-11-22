"""[daveey] The gateway marks continuations and durably secured own-marker notices as able to yield
to a core-update pause before their first model request, for that turn alone (see
tests/agent/test_step_boundary_pause_first_request.py for the agent side)."""

from __future__ import annotations

import threading
import types

import pytest
pytestmark = pytest.mark.usefixtures("no_background_link_refresh")

from tests.tui_gateway.test_update_pause_mid_tool import _InlineThread
from tui_gateway import server
from tui_gateway import update_pause as up

CFG = {"desktop": {"auto_continue": {"enabled": True, "resume_on_startup": True, "freshness_minutes": 240,
                                     "max_attempts": 2}}}


@pytest.fixture()
def home(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_hermes_home", tmp_path)
    monkeypatch.setattr(server, "_load_cfg", lambda: CFG)
    monkeypatch.setattr(up, "start_mid_tool_cancel", lambda *a: (lambda: None))
    monkeypatch.setattr(up, "start_in_place_watcher", lambda *a: None)
    monkeypatch.setattr(server.threading, "Thread", _InlineThread)
    for name, value in {"_emit": lambda *a, **k: None, "_wire_callbacks": lambda sid: None,
                        "_sync_agent_model_with_config": lambda sid, s: None,
                        "_session_cwd": lambda s: str(tmp_path), "_register_session_cwd": lambda s: None,
                        "_tts_stream_begin": lambda: None, "_sync_session_key_after_compress": lambda *a, **k: None,
                        "_get_usage": lambda a: {}}.items():
        monkeypatch.setattr(server, name, value)
    return tmp_path


def _submit(home, *, text="go", session_values=None, **kwargs):
    seen = []

    def _run(message, **_kw):
        seen.append((getattr(agent, "update_pause_first_request", None), callable(agent.step_boundary_pause)))
        return {"final_response": "done", "messages": []}

    agent = types.SimpleNamespace(session_id="session-key", run_conversation=_run, clear_interrupt=lambda: None)
    session = {"agent": agent, "session_key": "session-key", "history": [], "history_lock": threading.RLock(),
               "history_version": 0, "running": True, "attached_images": [], "image_counter": 0, "cols": 80,
               "slash_worker": None, "show_reasoning": False, "tool_progress_mode": "all", "inflight_turn": None,
               "profile_home": str(home)}
    session.update(session_values or {})
    server._run_prompt_submit("rid", "ui-sid", session, text, **kwargs)
    return seen, agent


def test_a_notice_with_its_own_durable_prompt_may_yield(home):
    seen, agent = _submit(home, display_kind="kanban_notice")
    assert seen == [(True, True)]
    assert agent.update_pause_first_request is False


@pytest.mark.parametrize("stale", [False, True])
def test_a_notice_without_a_successful_marker_write_cannot_yield(home, monkeypatch, stale):
    from tui_gateway.turn_marker import record_turn_start
    if stale:
        # Same text isolates write custody from the independent full-prompt equality guard.
        assert record_turn_start(home, "session-key", "go")
    monkeypatch.setattr(server, "record_turn_start", lambda *a, **k: False)
    seen, _ = _submit(home, display_kind="kanban_notice")
    assert seen == [(False, True)]


@pytest.mark.parametrize("values", [
    {"_auto_continue_prompt": "different root"},
    {"_turn_cancel_requested": True},
])
def test_notice_without_its_exact_resume_prompt_is_not_first_request_eligible(home, values):
    seen, _ = _submit(home, display_kind="kanban_notice", session_values=values)
    assert seen[0][0] is False


def test_notice_carrying_a_previous_pause_cannot_yield_before_persisting_itself(home):
    from tui_gateway.turn_marker import mark_turn_paused, record_turn_start
    assert record_turn_start(home, "session-key", "original paused turn")
    assert mark_turn_paused(home, "session-key", up.PAUSE_REASON)
    seen, _ = _submit(home, display_kind="kanban_notice", session_values={"_synthesized_turn": True})
    assert seen == [(False, True)]


def test_notice_with_truncated_marker_cannot_yield_before_persisting_full_prompt(home):
    from tui_gateway.turn_marker import _MAX_PROMPT_CHARS
    seen, _ = _submit(home, text="x" * (_MAX_PROMPT_CHARS + 1), display_kind="kanban_notice")
    assert seen[0][0] is False


def test_a_continuation_turn_may_yield_before_its_first_request(home):
    seen, agent = _submit(home, display_kind="auto_continue")
    assert seen == [(True, True)]
    assert agent.update_pause_first_request is False  # for that turn only


@pytest.mark.parametrize("kind", [None, "process_complete"])
def test_any_other_turn_waits_for_a_real_step_boundary(home, kind):
    seen, agent = _submit(home, display_kind=kind)
    assert seen[0][0] is False  # a literal False, not 0 or another falsy value (== below rejects None)
    assert seen == [(False, True)]
    assert agent.update_pause_first_request is False
