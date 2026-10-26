"""Disposition assessment cannot be starved by an earlier ordinary hold."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.pre_final_gate import UnresolvedFallback, evaluate


@pytest.mark.parametrize("reverse", [False, True])
def test_initial_assessor_runs_before_ordinary_hold(reverse):
    calls = []

    def ordinary(**kwargs):
        calls.append("ordinary")
        return {"hold": True, "nudge": "Check the ask."}

    def assessor(**kwargs):
        calls.append("assessor")
        return {"hold": True, "nudge": "Continue the repair.", "unresolved_fallback": {
            "goal": "Repair example", "evidence_refs": ["user:0", "candidate"],
        }}

    assessor.assess_before_holds = True
    callbacks = [ordinary, assessor]
    if reverse:
        callbacks.reverse()
    with patch("agent.pre_final_gate.eligible_callbacks", return_value=callbacks):
        result = evaluate(SimpleNamespace(), "Diagnosis", [], 0)
    assert calls == ["assessor"]
    assert result.hold and result.unresolved_fallback.recheck_callback is assessor


def test_pending_owner_precedes_new_assessor_and_legacy_order_is_unchanged():
    calls = []

    def ordinary(**kwargs):
        calls.append("ordinary")
        return {"hold": True, "nudge": "Check the ask."}

    def assessor(**kwargs):
        calls.append("assessor")

    callbacks = [ordinary, assessor]
    with patch("agent.pre_final_gate.eligible_callbacks", return_value=callbacks):
        assert evaluate(SimpleNamespace(), "Diagnosis", [], 0).hold
        assert calls == ["ordinary"]
        calls.clear()
        assessor.assess_before_holds = True
        pending = UnresolvedFallback("Repair example", ("user:0",), ordinary)
        result = evaluate(SimpleNamespace(), "Diagnosis", [], 1, pending)
    assert calls == ["ordinary"]
    assert result.unresolved_fallback is pending
