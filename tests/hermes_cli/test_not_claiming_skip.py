"""Board not_claiming refusals describe only the recording machine."""
from types import SimpleNamespace

import pytest

from hermes_cli.kanban_capacity_evidence import _task_reasons
from hermes_cli.kanban_dispatch_skip import recorder_cannot_lease


@pytest.mark.parametrize("reason", ["skipped_ineligible", "claim_deferred"])
def test_not_claiming_is_recorder_only(reason):
    assert recorder_cannot_lease({"reason": reason, "detail": "not_claiming"})


def test_not_claiming_lease_refusal_is_placement_evidence():
    result = SimpleNamespace(skipped_ineligible=[], claim_deferred=[("card", "not_claiming")],
                             rate_limited=[], clock_held={}, respawn_guarded=[])
    reasons = _task_reasons(result, "card", "tick")
    assert len(reasons) == 1
    assert reasons[0]["native_code"] == "not_claiming"
    assert reasons[0]["code"] == "capacity.placement"
