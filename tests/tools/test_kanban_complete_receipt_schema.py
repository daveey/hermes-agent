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
