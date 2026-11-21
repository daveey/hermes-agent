"""Reviewer survivor controls: Stop ordering, declined custody, refused-turn and exact clear."""
from types import SimpleNamespace
import time

import pytest

from agent import conversation_loop, provider_retry_deadline as prd, turn_iteration_prep as tip
from agent import update_pause_cancel as cancel
from tests.tui_gateway import test_update_pause_model_request as helpers
from tui_gateway import server, turn_marker as tm

_quiet = helpers._quiet
MESSAGES = [{"role": "tool", "content": "completed", "tool_call_id": "done"}]


def test_stop_arriving_in_restored_boundary_wins(tmp_path, monkeypatch):
    agent = helpers._make_agent(tmp_path)
    monkeypatch.setattr(prd, "wait_restored", lambda *a: "yield")

    def boundary():
        agent.interrupt()
        return "core_update_pause"

    agent.step_boundary_pause = boundary
    result = prd.before_provider_work(agent, MESSAGES, [], 1)
    assert result["interrupted"] and result["turn_exit_reason"] == "interrupted_by_user"
    assert not result.get("paused_for_update")


def test_declined_restored_boundary_serves_remaining_epoch(tmp_path, monkeypatch):
    agent = helpers._make_agent(tmp_path)
    now = [time.time()]
    deadline = now[0] + 60
    prd.install(agent, stamp=lambda d: True, clear=None, restored=deadline)
    monkeypatch.setattr(prd, "_now", lambda: now[0])
    monkeypatch.setattr(prd, "_sleep", lambda s: now.__setitem__(0, now[0] + s))
    # The real restored wait yields once; declining the real boundary must loop,
    # not return to provider-capable iteration work while custody is still owed.
    agent.interrupt(tool_reason=cancel.CANCEL_TOOL_REASON)
    monkeypatch.setattr(cancel, "model_yield_requested", lambda a: a._interrupt_requested)
    seen = []
    agent.step_boundary_pause = lambda: seen.append(now[0]) or None
    assert prd.before_provider_work(agent, MESSAGES, [], 1) is None
    assert len(seen) == 1 and seen[0] < deadline
    assert now[0] >= deadline and prd.RESTORED_ATTR not in vars(agent)


def test_iteration_custody_precedes_provider_capable_preparation(tmp_path, monkeypatch):
    agent = helpers._make_agent(tmp_path)
    now = [time.time()]
    deadline = now[0] + 60
    prd.install(agent, stamp=None, clear=None, restored=deadline)
    monkeypatch.setattr(prd, "_now", lambda: now[0])
    monkeypatch.setattr(prd, "_sleep", lambda s: now.__setitem__(0, now[0] + s))
    agent._checkpoint_mgr = SimpleNamespace(new_turn=lambda: None)
    agent.step_boundary_pause = None
    seen = []
    # This preparation gate runs before compression/hooks; checking only the
    # final provider call would be masked by perform_api_call's second gate.
    monkeypatch.setattr(conversation_loop, "_review_input_budget_exhausted", lambda a: seen.append(now[0]) or True)
    verdict = tip.begin_iteration(agent, messages=MESSAGES, conversation_history=[],
                                 original_user_message="continue", api_call_count=1,
                                 interrupted=False, _turn_exit_reason=None)
    assert verdict.action == "break"
    assert seen and min(seen) >= deadline


@pytest.mark.parametrize("reason, expected", [(None, "interrupted_by_user"),
                                               ("backend_exit", "interrupted_by_system(backend_exit)")])
def test_restored_wait_interrupt_retains_issuer(tmp_path, monkeypatch, reason, expected):
    agent = helpers._make_agent(tmp_path)
    prd.install(agent, stamp=None, clear=None, restored=time.time() + 600)

    def stop(_seconds):
        agent.interrupt(tool_reason=reason)

    monkeypatch.setattr(prd, "_sleep", stop)
    result = prd.before_provider_work(agent, MESSAGES, [], 1)
    assert result["turn_exit_reason"] == expected
    assert result["interrupted"] and not result.get("paused_for_update")


def test_refused_combined_prompt_keeps_retry_epoch(tmp_path, monkeypatch):
    key, deadline = "refused-key", time.time() + 600
    assert tm.record_turn_start(tmp_path, key, "latest", provider_retry_not_before=deadline)
    session = {"session_key": key, "profile_home": str(tmp_path), "_refused_turn_earlier": "earlier"}
    monkeypatch.setattr(server, "_load_cfg", lambda: helpers.CFG)
    monkeypatch.setattr(server, "_session_home", lambda s: tmp_path)
    monkeypatch.setattr(server, "_auto_continue_config", lambda: (True, 14400, 2))
    monkeypatch.setattr(server, "_active_goal_manager", lambda s: None)
    monkeypatch.setattr(server, "read_turn_marker", tm.read_turn_marker)
    monkeypatch.setattr(server, "record_turn_start", tm.record_turn_start)
    # Gateway test isolation disables refused timers; restore bounded policy data
    # without scheduling a timer (this exercises only the durable hold producer).
    monkeypatch.setattr(server, "_REFUSED_RETRY_DELAYS_S", (45.0, 180.0))
    server._hold_refused_turn(session, key, "auto_continue", {"retryable": True})
    assert "_refused_turn_earlier" not in session, session
    marker = tm.read_turn_marker(tmp_path, key)
    assert marker["prompt"] == "earlier\n\nlatest"
    assert marker[tm.PROVIDER_RETRY_FIELD] == deadline
    assert session["_refused_turn_marker_key"] == key


def test_clear_old_epoch_cannot_remove_new_epoch_in_same_generation(tmp_path):
    key, deadline = "clear-key", time.time() + 600
    assert tm.record_turn_start(tmp_path, key, "original", provider_retry_not_before=deadline)
    generation = tm.read_turn_marker(tmp_path, key)
    assert not tm.clear_provider_retry_deadline(tmp_path, key, deadline - 1, generation)
    assert tm.read_turn_marker(tmp_path, key)[tm.PROVIDER_RETRY_FIELD] == deadline
    assert tm.clear_provider_retry_deadline(tmp_path, key, deadline, generation)
    assert tm.PROVIDER_RETRY_FIELD not in tm.read_turn_marker(tmp_path, key)
