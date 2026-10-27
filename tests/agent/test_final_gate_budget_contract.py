"""Opt-in budget contract preserves strict legacy callback signatures."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent import pre_final_gate as gate


@pytest.mark.parametrize("budget,attempt", [(0, 0), (1, 1), (3, 2)])
def test_budget_is_total_and_only_passed_to_opted_in_callback(budget, attempt):
    seen = []

    def legacy(*, session_id, platform, model, final_response, turn_messages, attempt):
        seen.append(("legacy", attempt))

    def modern(*, session_id, platform, model, final_response, turn_messages, attempt, hold_budget):
        seen.append(("modern", attempt, hold_budget))
        return {"replace": "Checked."}

    modern.accepts_hold_budget = True
    with patch.object(gate, "eligible_callbacks", return_value=[legacy, modern]), \
            patch.object(gate, "max_attempts", return_value=budget):
        decision = gate.evaluate(SimpleNamespace(), "Draft", [], attempt)
    assert seen == [("legacy", attempt), ("modern", attempt, budget)]
    assert decision.replacement == "Checked."
