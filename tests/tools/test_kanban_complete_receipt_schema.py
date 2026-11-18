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


def test_served_complete_schema_declares_review_and_landing_contract():
    from tools import kanban_tools
    from tools.registry import registry

    schema = registry.get_schema("kanban_complete")
    assert schema is not None
    meta = schema["parameters"]["properties"]["metadata"]
    props = meta["properties"]
    assert props["verdict"]["enum"] == ["approve", "request_changes"]
    for key in ("reviewed_sha", "reviewed_pr", "pr", "published_pr", "landing_owner"):
        assert props[key]["type"] == "string"
    assert props["findings"]["type"] == "array"
    assert props["findings"]["items"] == {"type": "string"}
    for key in ("pr_left_open", "contract_waiver"):
        assert props[key]["type"] == "object"
        assert props[key]["properties"]["reason"]["type"] == "string"
    assert meta.get("additionalProperties", True) is True
    assert not meta.get("required")


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


_DELIVERY = {"request_uid": "r", "target_task_id": "t_x", "board": "default", "caller_task_id": "t_y",
             "caller_run_uid": "u", "caller_gateway": "a", "executor_gateway": "b", "comment_id": 7,
             "content_sha256": "0" * 64, "result_event_uid": "e"}


def _admits(delivery) -> bool:
    receipt = {"version": 1, "items": [{
        "scope": "comment on the foreign card", "disposition": "delivered", "remaining": [],
        "evidence": [{"ref": "operator comment 7", "observed_at": "2026-11-13T06:40:00+00:00",
                      "verification": "independently_checked", "operator_delivery": delivery}]}]}
    try:
        hcr.validate_completion_receipt(receipt)
    except ValueError:
        return False
    return True


def test_operator_delivery_schema_is_the_validators_strict_shape():
    # PR #1989 review nit: a bare {type: object} lets a strictly validating client build a delivery proof the
    # writer refuses. The advertised keys, required set and per-field bounds must be what the validator enforces,
    # read from its behaviour here, not from a shared constant, so either side drifting turns this red.
    schema = _evidence_schema()["properties"]["operator_delivery"]
    assert _admits(_DELIVERY)
    validator_required = {key for key in _DELIVERY if not _admits({k: v for k, v in _DELIVERY.items() if k != key})}
    assert validator_required == set(_DELIVERY)
    assert not _admits({**_DELIVERY, "extra": "x"})
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == validator_required
    assert set(schema["required"]) == validator_required
    props = schema["properties"]
    assert props["comment_id"] == {"type": "integer", "minimum": 1}
    assert not _admits({**_DELIVERY, "comment_id": 0}) and not _admits({**_DELIVERY, "comment_id": "7"})
    sha = props["content_sha256"]
    assert sha["type"] == "string" and sha["pattern"] == "^[0-9a-f]{64}$"
    assert not _admits({**_DELIVERY, "content_sha256": "A" * 64})
    for key in validator_required - {"comment_id", "content_sha256"}:
        limit = props[key]["maxLength"]
        assert props[key]["type"] == "string" and props[key]["minLength"] == 1 and limit < 2000
        assert _admits({**_DELIVERY, key: "x" * limit}) and not _admits({**_DELIVERY, key: "x" * (limit + 1)})
        assert not _admits({**_DELIVERY, key: ""})


def _text_field_schemas():
    """(path, schema) of every receipt string field whose writer check is ``_text``."""
    item = _receipt_schema()["properties"]["items"]["items"]["properties"]
    evidence = _evidence_schema()["properties"]
    remaining = item["remaining"]["items"]["properties"]
    delivery = evidence["operator_delivery"]["properties"]
    fields = {"scope": item["scope"], "superseded_by": item["superseded_by"], "evidence.ref": evidence["ref"],
              "evidence.observed_at": evidence["observed_at"], "remaining.scope": remaining["scope"],
              "remaining.owner": remaining["owner"], "remaining.reason": remaining["reason"]}
    fields.update({f"operator_delivery.{key}": schema for key, schema in delivery.items() if key != "comment_id"})
    return fields


def test_text_fields_advertise_the_writers_nonblank_no_control_rule():
    # PR #2015 review nit: minLength:1 alone lets a strictly validating client build `board: " "` or a ref with a
    # control character, which _text refuses at write time. Every _text field must refuse them in the schema too.
    from jsonschema.validators import Draft202012Validator

    fields = _text_field_schemas()
    assert len(fields) == 16
    refused = [" ", "   ", "\t\n", "\u3000", "\u00a0", "ok\x00", "ok\x1b[0m", "a\rb", "\x0b"]
    for path, schema in fields.items():
        check = Draft202012Validator(schema)
        for value in refused:
            assert not check.is_valid(value), (path, value)
    for path, schema in fields.items():
        if path.startswith("operator_delivery.") or path in ("evidence.observed_at", "remaining.owner"):
            continue  # their own length/format bounds; the shared pattern is still asserted below
        check = Draft202012Validator(schema)
        for value in ("x", " padded\tline\none ", "caf\u00e9 \u2014 \U0001f600", "a\x7fb"):
            assert check.is_valid(value), (path, value)
    for path, schema in fields.items():
        assert schema.get("pattern") == (
            "^[0-9a-f]{64}$" if path == "operator_delivery.content_sha256" else hcr.TEXT_PATTERN), path


def test_kanban_complete_schema_stays_within_its_byte_budget():
    # KANBAN_COMPLETE_SCHEMA goes out on every worker request, and completion_receipt repeats TEXT_PATTERN on 15
    # fields (the cost note at TEXT_PATTERN). Budget the whole serialized schema, not the pattern's length, so
    # Explicit review/landing keys cost ~0.9 KB but let schema-guided providers emit the required verdict.
    size = len(json.dumps(KANBAN_COMPLETE_SCHEMA).encode())
    assert size <= 11700, (
        f"KANBAN_COMPLETE_SCHEMA is {size} bytes, over its 11700-byte budget: every worker request carries it. "
        "Trim it, or raise the budget deliberately and say why.")


def test_text_pattern_is_exactly_the_writers_text_rule():
    # _text reads the same constant; prove it still means "no C0 control but tab/newline, not all str.isspace()"
    # for every BMP character, alone and padded, so the advertised pattern is the writer's real rule.
    import re

    rule = re.compile(hcr.TEXT_PATTERN)
    for code in range(0x10000):
        c = chr(code)
        control = code < 32 and c not in "\n\t"
        assert bool(rule.fullmatch(c)) == (not control and not c.isspace()), hex(code)
        assert bool(rule.fullmatch(f" {c}\t")) == (not control and not c.isspace()), hex(code)
        assert bool(rule.fullmatch(f"x{c}")) == (not control), hex(code)
    for value, why in ((" ", "nonempty text"), ("\u3000", "nonempty text"), ("ok\x01", "control characters"),
                       ("\n\t", "nonempty text")):
        try:
            hcr._text(value, "f")
        except ValueError as exc:
            assert why in str(exc), (value, exc)
            continue
        raise AssertionError(f"_text admitted {value!r}")
    assert hcr._text("a\tb\nc", "f") == "a\tb\nc"
