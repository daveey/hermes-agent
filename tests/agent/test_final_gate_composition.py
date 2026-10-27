"""Replacement gates must not skip other registered validators."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.pre_final_gate import evaluate


def _evaluate(callbacks):
    with (
        patch("agent.pre_final_gate.eligible_callbacks", return_value=callbacks),
        patch("agent.pre_final_gate.max_attempts", return_value=2),
        patch("agent.pre_final_gate.timeout_s", return_value=1),
    ):
        return evaluate(SimpleNamespace(), "Please reconnect the retired account.", [], 0)


def test_replacement_runs_later_validators_without_appending_rejected_text():
    seen = []

    def later(**kwargs):
        seen.append(kwargs["final_response"])
        return {"append": "Please reconnect the retired account."}

    result = _evaluate([lambda **kw: {"replace": "Request suppressed."}, later])
    assert seen == ["Please reconnect the retired account."]
    assert result.replacement == "Request suppressed."
    assert result.append == ""


def test_conflicting_replacements_deliver_neither_candidate():
    result = _evaluate([
        lambda **kw: {"replace": "First replacement."},
        lambda **kw: {"replace": "Second replacement."},
    ])
    assert result.replacement == "Response withheld because validation gates disagreed."
    assert not result.hold


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("other", [{"hold": True, "nudge": "Try again."}, {"append": "Rejected ask."}])
def test_replacement_overrides_hold_and_append_in_both_orders(reverse, other):
    callbacks = [lambda **kw: {"replace": "Request suppressed."}, lambda **kw: other]
    result = _evaluate(list(reversed(callbacks)) if reverse else callbacks)
    assert result.replacement == "Request suppressed."
    assert not result.hold
    assert result.nudge == result.append == ""


def test_later_failure_does_not_erase_replacement():
    def broken(**kw):
        raise RuntimeError("synthetic validator failure")

    result = _evaluate([lambda **kw: {"replace": "Request suppressed."}, broken])
    assert result.replacement == "Request suppressed."


def test_identical_replacements_are_not_a_conflict():
    seen = []

    def same(**kw):
        seen.append(True)
        return {"replace": "Request suppressed."}

    result = _evaluate([same, same])
    assert len(seen) == 2
    assert result.replacement == "Request suppressed."
