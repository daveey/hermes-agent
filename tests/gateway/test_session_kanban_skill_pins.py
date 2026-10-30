"""Delayed dispatcher pins report loss without borrowing the owner's ambient task identity."""
from unittest.mock import MagicMock

import pytest

from gateway import session_kanban
from hermes_cli.kanban_skill_check import unavailable_pin_note


@pytest.mark.parametrize("loaded", [False, True], ids=["all-missing", "partial"])
def test_missing_pin_uses_canonical_recovery_guidance(monkeypatch, loaded):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr("agent.skill_commands.build_preloaded_skills_prompt",
                        lambda *a, **k: ("LOADED_BODY" if loaded else "", ["a"] if loaded else [], ["gone"]))
    agent = MagicMock(session_id="s")
    agent.run_conversation.return_value = {"final_response": "ok", "messages": []}
    monkeypatch.setattr("agent.kanban_turn_recovery.recover_failed_kanban_turns", lambda *a, **k: None)
    context = {"skills": ["gone"], "context": "CTX", "goal_mode": False,
               "task_id": "t_1", "run_id": 1, "claim_lock": "l"}
    session_kanban._run_task_turns(agent, {"text": "FRAME"}, [], context)
    prompt = agent.run_conversation.call_args.args[0]
    assert unavailable_pin_note(["gone"]) in prompt and prompt.endswith("FRAME")
    assert "kind='assignment'" in prompt
    assert ("LOADED_BODY" in prompt) == loaded


def test_no_missing_pin_has_no_recovery_note():
    assert unavailable_pin_note([]) == ""
