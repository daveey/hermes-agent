"""Restored retry custody precedes turn-start compression and provider-capable hooks."""
import time

from agent import provider_retry_deadline as prd, turn_context
from types import SimpleNamespace
import threading
from tui_gateway import server
from tui_gateway.prompt_turn import _TurnRun
from tests.tui_gateway import test_update_pause_model_request as helpers

_quiet = helpers._quiet


def test_restored_deadline_precedes_turn_start_provider_work(tmp_path, monkeypatch):
    agent = helpers._make_agent(tmp_path)
    now = [time.time()]
    deadline = now[0] + 1200  # finite epochs must not be capped to now + 600
    monkeypatch.setattr(prd, "_now", lambda: now[0])
    monkeypatch.setattr(prd, "_sleep", lambda s: now.__setitem__(0, now[0] + s))
    prd.install(agent, stamp=None, clear=None, restored=deadline)
    seen = []
    original = turn_context._collect_pre_llm_call_context

    def hook(*args, **kwargs):
        seen.append(now[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(turn_context, "_collect_pre_llm_call_context", hook)
    agent._interruptible_api_call = lambda *a, **k: helpers._response()
    assert agent.run_conversation("Continue")["final_response"] == "done"
    assert seen and min(seen) >= deadline


def test_invoke_without_own_marker_still_serves_selected_epoch(tmp_path, monkeypatch):
    agent = helpers._make_agent(tmp_path)
    now = [time.time()]
    deadline = now[0] + 1200
    monkeypatch.setattr(prd, "_now", lambda: now[0])
    monkeypatch.setattr(prd, "_sleep", lambda s: now.__setitem__(0, now[0] + s))
    monkeypatch.setattr(server, "_start_usage_ticker", lambda *a: (
        threading.Event(), SimpleNamespace(join=lambda: None)))
    session = {"agent": agent, "session_key": "key", "profile_home": str(tmp_path),
               "history_lock": threading.Lock()}
    st = _TurnRun(agent, None, None, True, marker_key="key", own_marker=None,
                  provider_retry_not_before=deadline)
    run = agent.run_conversation

    def observed(message, **kwargs):
        assert vars(agent)[prd.RESTORED_ATTR] == deadline
        assert vars(agent)[prd.STAMP_ATTR] is None
        assert agent.step_boundary_pause is None
        return run(message, **kwargs)

    agent.run_conversation = observed
    calls = []
    agent._interruptible_api_call = lambda *a, **k: calls.append(now[0]) or helpers._response()
    monkeypatch.setattr("agent.turn_api_call._should_stream", lambda _agent: False)
    server._invoke_agent("ui", session, st, "Continue", "Continue", None, [], "auto_continue", None, text="Continue")
    assert st.result["final_response"] == "done"
    assert calls and min(calls) >= deadline
    assert prd.RESTORED_ATTR not in vars(agent)
