"""Full text-stop contracts for callback ownership and interacting verification gates."""

from unittest.mock import patch

import pytest

from tests.agent.test_pre_final_gate import Run, _buffered
pytest_plugins = ["tests.agent.test_pre_final_gate"]


def _hold():
    return {"hold": True, "nudge": "Repair it.", "unresolved_fallback": {
        "goal": "Repair example", "evidence_refs": ["tool:probe"],
    }}


@pytest.mark.parametrize("unrelated_hold", [False, True])
@pytest.mark.parametrize("change", ["removed", "reloaded", "reversed"])
@pytest.mark.parametrize("owner_first", [False, True])
def test_held_recheck_belongs_to_original_callback(agent, change, owner_first, unrelated_hold):
    run = Run(agent, ["Rejected diagnosis."] * 3)
    owner_calls = []
    callbacks = []

    @_buffered
    def replacement(**kw):
        return None

    @_buffered
    def unrelated(attempt, **kw):
        # A different gate must not preempt the owning recheck after reordering.
        return {"hold": True, "nudge": "Other check."} if attempt and unrelated_hold else None

    @_buffered
    def owner(attempt, **kw):
        owner_calls.append(attempt)
        if not attempt:
            if change == "removed":
                callbacks[:] = [unrelated]
            elif change == "reloaded":
                callbacks[:] = [unrelated, replacement]
            else:
                callbacks[:] = [unrelated, owner]
            return _hold()
        raise RuntimeError("owning recheck unavailable")

    callbacks[:] = [owner, unrelated] if owner_first else [unrelated, owner]
    with (
        patch("hermes_cli.lifecycle.has_hook", side_effect=lambda name: name == "pre_final_gate"),
        patch("hermes_cli.plugins.iter_hook_callbacks", side_effect=lambda name: tuple(callbacks)
              if name == "pre_final_gate" else ()),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation("Repair example")
    assert "continuation unavailable" in result["final_response"]
    assert "Unresolved goal: Repair example" in result["final_response"]
    assert len(run.requests) == 2
    assert owner_calls == ([0, 1] if change == "reversed" else [0])
    assert "Rejected diagnosis" not in run.shown + str(result["messages"])
    assert not result["completed"]


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("budget", [1, 2, 3])
def test_pre_verify_cannot_preview_rejected_candidate_or_double_nudge(agent, streaming, budget):
    agent.max_iterations = budget
    agent.iteration_budget.max_total = budget
    run = Run(agent, ["Rejected diagnosis.", "Repair verified.", "Final verified answer."])
    if not streaming:
        agent.stream_delta_callback = None
    judged, verified, persisted = [], [], []
    agent._flush_messages_to_session_db = lambda messages, history: persisted.extend(
        dict(m) for m in messages if not m.get("_pre_final_gate_synthetic")
    )

    @_buffered
    def owner(final_response, **kw):
        judged.append(final_response)
        return _hold() if final_response == "Rejected diagnosis." else None

    def verify(final_response, **kw):
        verified.append(final_response)
        return "Verify once more." if len(verified) == 1 else None

    with (
        patch("hermes_cli.lifecycle.has_hook", side_effect=lambda name: name in ("pre_final_gate", "pre_verify")),
        patch("hermes_cli.plugins.iter_hook_callbacks", side_effect=lambda name: (owner,)
              if name == "pre_final_gate" else ()),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
        patch("agent.verify_hooks.pre_verify_every_turn", return_value=True),
        patch("hermes_cli.plugins.get_pre_verify_continue_message", side_effect=verify),
    ):
        result = agent.run_conversation("Repair example")
    assert "Rejected diagnosis" not in run.shown + str(result["messages"]) + str(persisted)
    assert judged[0] == "Rejected diagnosis."
    assert "Rejected diagnosis." not in verified
    assert agent._pre_final_gate_attempts == 1
    if budget == 1:
        assert "turn budget exhausted" in result["final_response"]
        assert verified == []
    elif budget == 2:
        assert result["final_response"] == "Repair verified."
        assert verified == ["Repair verified."]
        assert len(run.requests) == 2
    else:
        assert result["final_response"] == "Final verified answer."
        assert verified == ["Repair verified.", "Final verified answer."]
        assert agent._pre_verify_nudges == 1
        assert len(run.requests) == 3


def test_unbuffered_legacy_gate_keeps_verification_interim_order(agent):
    run = Run(agent, ["Legacy interim.", "Legacy final."])
    judged = []

    def legacy(final_response, **kw):
        judged.append(final_response)
        return {"append": "Legacy note."}

    with (
        patch("hermes_cli.lifecycle.has_hook", side_effect=lambda name: name in ("pre_final_gate", "pre_verify")),
        patch("hermes_cli.plugins.iter_hook_callbacks", side_effect=lambda name: (legacy,)
              if name == "pre_final_gate" else ()),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
        patch("agent.verify_hooks.pre_verify_every_turn", return_value=True),
        patch("hermes_cli.plugins.get_pre_verify_continue_message", side_effect=["Verify.", None]),
    ):
        result = agent.run_conversation("Give an answer")
    assert judged == ["Legacy final."]
    assert "Legacy interim." in run.shown
    assert result["final_response"] == "Legacy final.\n\nLegacy note."
    assert len(run.requests) == 2


def test_owner_acceptance_survives_another_prefinal_hold(agent):
    agent.max_iterations = 2
    agent.iteration_budget.max_total = 2
    run = Run(agent, ["Rejected diagnosis.", "Repair verified."])

    @_buffered
    def owner(attempt, **kw):
        return _hold() if not attempt else None

    @_buffered
    def unrelated(attempt, **kw):
        return {"hold": True, "nudge": "Another check."} if attempt else None

    with (
        patch("hermes_cli.lifecycle.has_hook", side_effect=lambda name: name == "pre_final_gate"),
        patch("hermes_cli.plugins.iter_hook_callbacks", return_value=(owner, unrelated)),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation("Repair example")
    assert result["final_response"] == "Repair verified."
    assert "Rejected diagnosis" not in run.shown + str(result["messages"])
