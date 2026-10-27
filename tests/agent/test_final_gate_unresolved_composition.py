"""Replacement suppression must not erase unresolved-goal ownership."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.pre_final_gate import UnresolvedFallback, evaluate


def _hold(**kwargs):
    return {"hold": True, "nudge": "Repair it.", "unresolved_fallback": {
        "goal": "Repair example", "evidence_refs": ["tool:probe"],
    }}


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("attempt", [0, 2])
def test_replacement_cannot_erase_unresolved_rejection(reverse, attempt):
    callbacks = [_hold, lambda **kw: {"replace": "Request suppressed."}]
    if reverse:
        callbacks.reverse()
    with patch("agent.pre_final_gate.eligible_callbacks", return_value=callbacks):
        result = evaluate(SimpleNamespace(), "Rejected diagnosis.", [], attempt)
    assert result.replacement is None
    assert result.unresolved_fallback.recheck_callback is _hold
    assert result.hold is (attempt == 0)


@pytest.mark.parametrize("available", [False, True])
def test_other_replacement_cannot_accept_unavailable_owner(available):
    def owner(**kwargs):
        raise RuntimeError("owning check unavailable")

    callbacks = [lambda **kw: {"replace": "Request suppressed."}]
    if available:
        callbacks.append(owner)
    pending = UnresolvedFallback("Repair example", ("tool:probe",), owner)
    with patch("agent.pre_final_gate.eligible_callbacks", return_value=callbacks):
        result = evaluate(SimpleNamespace(), "Rejected diagnosis.", [], 1, pending)
    assert result.unresolved_fallback is pending
    assert result.boundary == "continuation unavailable"
    assert result.replacement is None


def test_owner_can_accept_via_replacement_with_hold_budget():
    def owner(hold_budget, **kwargs):
        assert hold_budget == 0
        return {"replace": "Request suppressed."}

    owner.accepts_hold_budget = True
    pending = UnresolvedFallback("Repair example", ("tool:probe",), owner)
    with (
        patch("agent.pre_final_gate.eligible_callbacks", return_value=[owner]),
        patch("agent.pre_final_gate.max_attempts", return_value=0),
    ):
        result = evaluate(SimpleNamespace(), "Rejected diagnosis.", [], 1, pending)
    assert result.recheck_accepted
    assert result.replacement == "Request suppressed."
    assert result.unresolved_fallback is None


def test_budget_summary_rejection_clears_native_carriers():
    from agent.final_gate_fallback import validate_budget_response
    from unittest.mock import Mock

    agent = SimpleNamespace(_interrupt_requested=False, _discard_final_gate_buffer=Mock())
    messages = [{"role": "assistant", "content": "Rejected diagnosis.",
                 "codex_message_items": [{"text": "Rejected diagnosis."}]}]
    with patch("agent.pre_final_gate.eligible_callbacks", return_value=[_hold]):
        response, changed = validate_budget_response(agent, "Rejected diagnosis.", messages)
    assert changed
    assert "Unresolved goal: Repair example" in response
    assert "turn budget exhausted" in response
    assert messages == [{"role": "assistant", "content": response}]
    assert agent._final_gate_buffer is None
