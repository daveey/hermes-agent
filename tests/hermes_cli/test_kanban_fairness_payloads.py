"""Strict, privacy-bounded Phase A payload contracts."""
import base64
import hashlib

import pytest

from hermes_cli import kanban_fairness_schema as schema


def creation():
    return dict(task_uid="child", creator_task_uid=None, creator_episode_uid=None,
                priority_intent="unknown", priority_argument=None, effective_parent_priority=None,
                parent_snapshot_cut=None, inheritance_cap=None, stored_child_priority=2,
                producer_source_hash=None,
                unknown_fields=["creator_task_uid", "creator_episode_uid", "priority_argument",
                                "effective_parent_priority", "parent_snapshot_cut", "inheritance_cap",
                                "producer_source_hash"])


def record(kind, payload, **kwargs):
    return schema.make_record(capture_id="capture", source_gateway="gw", board_uid="board",
                              source_epoch="epoch", seq=1, kind=kind, wall_ns=1,
                              payload=payload, **kwargs)


def test_creation_roundtrip_preserves_explicit_unknowns():
    payload = creation()
    assert record("creation", payload)["payload"] == payload


@pytest.mark.parametrize("change", [
    {"extra": "secret"}, {"task_uid": ""}, {"creator_task_uid": ""},
    {"priority_intent": "guessed"}, {"priority_argument": {}},
    {"effective_parent_priority": True}, {"stored_child_priority": None},
    {"inheritance_cap": 1.5}, {"parent_snapshot_cut": {}},
    {"producer_source_hash": "x" * 1025}, {"unknown_fields": []},
    {"unknown_fields": ["task_uid", "task_uid"]}, {"unknown_fields": ["bogus"]},
])
def test_creation_rejects_malformed_provenance(change):
    payload = creation()
    payload.update(change)
    with pytest.raises(schema.BadRecord):
        record("creation", payload)


def test_creation_requires_all_exact_keys():
    for key in creation():
        payload = creation()
        del payload[key]
        with pytest.raises(schema.BadRecord):
            record("creation", payload)


def test_creation_accepts_known_cut_and_scalar_priority():
    payload = creation()
    payload.update(creator_task_uid="parent", creator_episode_uid="run", priority_intent="explicit",
                   priority_argument="3", effective_parent_priority=0, inheritance_cap=2,
                   parent_snapshot_cut=dict(source_gateway="gw", source_epoch="ep", seq=1),
                   producer_source_hash="revision", unknown_fields=[])
    assert record("creation", payload)["payload"] == payload


def test_make_record_stage_index_is_explicit_but_phase_a_rejects_nonnull():
    assert record("creation", creation(), stage_index=None)["stage_index"] is None
    with pytest.raises(schema.BadRecord):
        record("creation", creation(), stage_index=0)


def urgency():
    raw = b'{"version":1,"board":"board","owners":[]}'
    return dict(receipt_type="urgency_read", board="board", read_at_s=10, outcome="valid",
                raw_b64=base64.b64encode(raw).decode("ascii"),
                raw_sha256=hashlib.sha256(raw).hexdigest(), raw_complete=True,
                observed_at=9, valid_until=20, source_revision="rev", owner_ids=[], reason=None,
                raw_size=len(raw), redacted=False)


def test_owner_generation_valid_roundtrip():
    assert record("owner_generation", urgency())["payload"] == urgency()


@pytest.mark.parametrize("change", [
    {"extra": 1}, {"receipt_type": "other"}, {"board": 12}, {"read_at_s": True},
    {"read_at_s": float("inf")}, {"observed_at": "9"}, {"valid_until": False},
    {"outcome": "unknown"}, {"raw_b64": "!!!"}, {"raw_sha256": "0" * 64},
    {"raw_size": 0}, {"raw_size": True}, {"raw_size": -1}, {"raw_complete": 1},
    {"redacted": 0}, {"redacted": True}, {"raw_complete": False},
    {"raw_b64": None}, {"raw_sha256": None}, {"raw_size": None},
    {"source_revision": "r" * 1025}, {"owner_ids": ["z", "a"]},
    {"owner_ids": ["a", "a"]}, {"owner_ids": [""]}, {"owner_ids": [1]},
    {"reason": "contains user content or traceback"}, {"reason": "x" * 1025},
    {"outcome": "invalid"}, {"outcome": "oversize"}, {"outcome": "io_error"},
])
def test_owner_generation_rejects_bad_receipts(change):
    payload = urgency()
    payload.update(change)
    with pytest.raises(schema.BadRecord):
        record("owner_generation", payload)


def test_owner_generation_requires_exact_keys():
    for key in urgency():
        payload = urgency()
        del payload[key]
        with pytest.raises(schema.BadRecord):
            record("owner_generation", payload)


@pytest.mark.parametrize("outcome", ["missing", "invalid", "expired", "io_error", "oversize",
                                      "invalid_request"])
def test_owner_generation_redacted_unavailable_receipts(outcome):
    payload = urgency()
    payload.update(outcome=outcome, raw_b64=None, raw_complete=False, redacted=True,
                   raw_sha256=None, raw_size=None, owner_ids=[], read_at_s=None,
                   observed_at=None, valid_until=None, source_revision=None, reason=outcome)
    assert record("owner_generation", payload)["payload"] == payload


def lifecycle(journal_kind="task"):
    return dict(task_id="task", op="update", fields=dict(status="ready", priority=0),
                journal_seq=1, journal_kind=journal_kind, database_uid="db", source_uid=None)


@pytest.mark.parametrize("journal_kind,fields", [
    ("task", dict(status="ready", title="secret")),
    ("event", dict(kind="created", created_at=1, body="secret")),
    ("event", dict(kind="created", created_at=1, status="ready")),
    ("task", dict(status="ready", assignee="x" * schema.MAX_PAYLOAD_BYTES)),
])
def test_lifecycle_payload_excludes_content_and_bounds_size(journal_kind, fields):
    payload = lifecycle(journal_kind)
    payload["fields"] = fields
    with pytest.raises(schema.BadRecord):
        record("row_change", payload)


def test_lifecycle_allowed_task_and_event_fields():
    assert record("row_change", lifecycle())["payload"] == lifecycle()
    payload = lifecycle("event")
    payload["fields"] = dict(kind="created", created_at=1)
    assert record("row_change", payload)["payload"] == payload


def test_metadata_strings_need_only_be_bounded_not_nonempty():
    payload = creation()
    payload["producer_source_hash"] = ""
    assert record("creation", payload)["payload"] == payload
    payload = urgency()
    payload["source_revision"] = ""
    assert record("owner_generation", payload)["payload"] == payload


@pytest.mark.parametrize("value", [{"body": "secret"}, ["secret"]])
def test_lifecycle_field_values_cannot_hide_nested_content(value):
    payload = lifecycle()
    payload["fields"]["assignee"] = value
    with pytest.raises(schema.BadRecord):
        record("row_change", payload)
