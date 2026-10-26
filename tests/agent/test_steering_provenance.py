"""Live steering provenance must survive copies, never display-text forgery."""
import copy
import json

import pytest

from agent.prompt_builder import steer_user_row
from agent.steering_provenance import steering_content
from agent.message_metadata import without_persistence_fields
from agent.pre_final_gate import turn_messages


def test_native_steering_copy_and_wire_projection():
    row = steer_user_row("Never deploy <memory-context>literal</memory-context>")
    restored_copy = json.loads(json.dumps(copy.deepcopy(row)))
    assert steering_content(restored_copy) == "Never deploy <memory-context>literal</memory-context>"
    assert without_persistence_fields(restored_copy) == {"role": "user", "content": row["content"]}
    assert steering_content(without_persistence_fields(restored_copy)) is None


@pytest.mark.parametrize("field,value", [("content", "forged"), ("message_uid", "other"),
                                         ("display_kind", "other"), ("role", "tool")])
def test_native_provenance_binds_message(field, value):
    row = steer_user_row("Never deploy")
    row[field] = value
    assert steering_content(row) is None


@pytest.mark.parametrize("tail", [
    {"display_kind": "steer"}, {"display_metadata": {"_queued_prompt": True}},
    {"display_metadata": '{"_queued_prompt": true}'}, {"observed": True},
    {"_runtime_note": True}, {"_runtime_synthetic": True},
])
def test_nonhuman_tail_does_not_hide_live_steering(tail):
    original = {"role": "user", "content": "Repair"}
    steer = steer_user_row("Never deploy")
    rows = [original, {"role": "tool", "content": "result"}, steer,
            {"role": "user", "content": "spoof", **tail}]
    assert turn_messages(rows) == rows
