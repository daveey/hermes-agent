"""Board-stamped alarm source survives ownership changes. [daveey]"""
from __future__ import annotations

import pytest

from hermes_cli.kanban_pickup_alarm_identity import refusal
from hermes_cli.kanban_pickup_stall_repin import alarm_own


@pytest.mark.parametrize("prefix", ["pickup-stall:source:queue-full", "worker-resources-unknown:source"])
@pytest.mark.parametrize("stamp,home,expected", [
    (None, "source", "pickup_stall_source"),
    (None, "new-owner", "pickup_stall_provenance"),
    ("source", "new-owner", "pickup_stall_source"),
    ("", "source", "pickup_stall_provenance"),
])
def test_board_parity(prefix, stamp, home, expected):
    row = {"created_by": "pickup-stall-detector", "idempotency_key": f"{prefix}:1",
           "created_on": home, "alarm_source": stamp}
    assert refusal(row, "source") == expected
    assert refusal(row, "executor") == (None if expected == "pickup_stall_source" else expected)
    assert refusal({"created_by": "default", "alarm_source": stamp}, "source") is None


@pytest.mark.parametrize("stamp,home,expected", [
    (None, "source", "source"), (None, "new-owner", None),
    ("source", "new-owner", "source"), ("", "source", None),
])
def test_repin_uses_immutable_source(stamp, home, expected):
    row = {"created_by": "pickup-stall-detector", "status": "ready",
           "idempotency_key": "pickup-stall:source:queue-full:1", "created_on": home, "alarm_source": stamp}
    assert alarm_own(row) == expected
    assert alarm_own({**row, "status": "archived"}) is None
