"""[daveey] t_01a1d31bf400d56c: the gateway marks only continuation turns (kind ``auto_continue``) as able to yield
to a core-update pause before their first model request, for that turn alone (see
tests/agent/test_step_boundary_pause_first_request.py for the agent side)."""

from __future__ import annotations

import threading
import types

import pytest

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
    monkeypatch.setattr(server.threading, "Thread", _InlineThread)
    for name, value in {"_emit": lambda *a, **k: None, "_wire_callbacks": lambda sid: None,
                        "_sync_agent_model_with_config": lambda sid, s: None,
                        "_session_cwd": lambda s: str(tmp_path), "_register_session_cwd": lambda s: None,
                        "_tts_stream_begin": lambda: None, "_sync_session_key_after_compress": lambda *a, **k: None,
                        "_get_usage": lambda a: {}}.items():
        monkeypatch.setattr(server, name, value)
    return tmp_path


def _submit(home, **kwargs):
    seen = []

    def _run(message, **_kw):
        seen.append((getattr(agent, "update_pause_first_request", None), callable(agent.step_boundary_pause)))
        return {"final_response": "done", "messages": []}

    agent = types.SimpleNamespace(session_id="session-key", run_conversation=_run, clear_interrupt=lambda: None)
    session = {"agent": agent, "session_key": "session-key", "history": [], "history_lock": threading.RLock(),
               "history_version": 0, "running": True, "attached_images": [], "image_counter": 0, "cols": 80,
               "slash_worker": None, "show_reasoning": False, "tool_progress_mode": "all", "inflight_turn": None,
               "profile_home": str(home)}
    server._run_prompt_submit("rid", "ui-sid", session, "go", **kwargs)
    return seen, agent


def test_a_continuation_turn_may_yield_before_its_first_request(home):
    seen, agent = _submit(home, display_kind="auto_continue")
    assert seen == [(True, True)]
    assert agent.update_pause_first_request is False  # for that turn only


@pytest.mark.parametrize("kind", [None, "kanban_notice", "process_complete"])
def test_any_other_turn_waits_for_a_real_step_boundary(home, kind):
    seen, _agent = _submit(home, display_kind=kind)
    assert seen == [(False, True)]
