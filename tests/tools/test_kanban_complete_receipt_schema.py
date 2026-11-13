"""kanban_complete advertises metadata.completion_receipt (self-heal agent-f82d813de9).

Core 9bc6ed6b taught the card writer to admit a v1 ``completion_receipt`` and chat-closeout to render it, but the
kanban_complete tool never told a worker the field exists: 0 of 1378 hub completions carried one, so every
close-out fell back to prose handoffs and relisted finished asks. The schema must name the field, and its shape
must stay the validator's (hermes_completion_receipt), or the advertised form would be refused at write time.
"""
from __future__ import annotations

import json

import hermes_completion_receipt as hcr
from tools.kanban_tools_schemas import KANBAN_COMPLETE_SCHEMA


def _receipt_schema():
    meta = KANBAN_COMPLETE_SCHEMA["parameters"]["properties"]["metadata"]
    assert meta["type"] == "object"
    return meta["properties"]["completion_receipt"]


def test_complete_schema_advertises_completion_receipt_and_when_to_send_it():
    schema = _receipt_schema()
    assert schema["required"] == ["version", "items"]
    assert "metadata.completion_receipt" in KANBAN_COMPLETE_SCHEMA["description"]
    desc = schema["description"]
    assert "claims delivery" in desc  # when to include it
    assert "independently_checked" in desc  # what the verification claim means


def test_advertised_shape_is_the_validators_shape():
    schema = _receipt_schema()
    items = schema["properties"]["items"]
    assert items["maxItems"] == hcr.MAX_ITEMS
    props = items["items"]["properties"]
    assert tuple(props["disposition"]["enum"]) == hcr.DISPOSITIONS
    evidence = props["evidence"]["items"]["properties"]
    assert tuple(evidence["verification"]["enum"]) == hcr.VERIFICATIONS
    assert props["evidence"]["maxItems"] == hcr.MAX_EVIDENCE
    assert props["remaining"]["maxItems"] == hcr.MAX_REMAINING
    assert set(props["remaining"]["items"]["required"]) == {"scope", "state", "owner", "reason"}


def test_example_in_the_description_validates():
    desc = _receipt_schema()["description"]
    example = json.loads(desc[desc.index("Example: ") + len("Example: "):])
    assert hcr.validate_completion_receipt(example) == example


def _evidence_schema():
    return _receipt_schema()["properties"]["items"]["items"]["properties"]["evidence"]["items"]


def test_evidence_advertises_the_optional_operator_delivery_the_validator_admits():
    # evidence has additionalProperties:false, so a strict client refuses any key the schema omits; the validator
    # accepts (and for operator-gated claims requires) operator_delivery, so the schema must list it.
    evidence = _evidence_schema()
    assert evidence["additionalProperties"] is False
    assert evidence["properties"]["operator_delivery"]["type"] == "object"
    assert "operator_delivery" not in evidence["required"]
    example = {"version": 1, "items": [{
        "scope": "comment on the foreign card", "disposition": "delivered", "remaining": [],
        "evidence": [{"ref": "operator comment 7", "observed_at": "2026-11-13T06:40:00+00:00",
                      "verification": "independently_checked", "operator_delivery": {
                          "request_uid": "r", "target_task_id": "t_x", "board": "default", "caller_task_id": "t_y",
                          "caller_run_uid": "u", "caller_gateway": "a", "executor_gateway": "b", "comment_id": 7,
                          "content_sha256": "0" * 64, "result_event_uid": "e"}}]}]}
    admitted = hcr.validate_completion_receipt(example)
    assert set(admitted["items"][0]["evidence"][0]) <= set(evidence["properties"])


def test_description_states_timezone_and_byte_cap():
    desc = _receipt_schema()["description"]
    assert "timezone-aware" in desc
    assert f"{hcr.MAX_BYTES} bytes" in desc
    assert "operator_delivery" in desc
    assert "timezone" in _evidence_schema()["properties"]["observed_at"]["description"]
    big = {"version": 1, "items": [{"scope": "s" * hcr.MAX_TEXT, "disposition": "delivered", "remaining": [],
                                    "evidence": [{"ref": "r", "observed_at": "2026-11-13T06:40:00+00:00",
                                                  "verification": "reported"}]}] * 20}
    try:
        hcr.validate_completion_receipt(big)
    except ValueError as exc:
        assert str(hcr.MAX_BYTES) in str(exc)
    else:
        raise AssertionError("over-cap receipt admitted")
