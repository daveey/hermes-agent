"""Strict, privacy-bounded Phase A payload contracts."""
import base64
import hashlib
import json

import pytest

from hermes_cli import kanban_fairness_schema as schema


def creation():
    return dict(task_uid="child", creator_task_uid=None, creator_episode_uid=None,
                priority_intent="unknown", priority_argument=None, effective_parent_priority=None,
                parent_snapshot_cut=None, inheritance_cap=None, stored_child_priority=2,
                producer_source_hash=None,
                unknown_fields=["creator_task_uid", "creator_episode_uid", "priority_intent",
                                "priority_argument", "effective_parent_priority", "parent_snapshot_cut",
                                "inheritance_cap", "producer_source_hash"])


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


def known_creation(**over):
    payload = dict(task_uid="child", creator_task_uid="parent", creator_episode_uid="run",
                   priority_intent="omitted", priority_argument=None, effective_parent_priority=0,
                   parent_snapshot_cut=dict(source_gateway="gw", source_epoch="ep", seq=1),
                   inheritance_cap=2, stored_child_priority=2, producer_source_hash="revision",
                   unknown_fields=[])
    payload.update(over)
    return payload


def test_creation_accepts_known_cut_and_scalar_priority():
    payload = known_creation(priority_intent="explicit", priority_argument="3")
    assert record("creation", payload)["payload"] == payload


def test_creation_known_omitted_and_explicit_null():
    omitted = known_creation()
    assert record("creation", omitted)["payload"] == omitted
    explicit_null = known_creation(priority_intent="explicit", priority_argument=None)
    assert record("creation", explicit_null)["payload"] == explicit_null


def test_creation_unknown_intent_requires_marker():
    missing = known_creation(priority_intent="unknown", priority_argument=3)
    with pytest.raises(schema.BadRecord):
        record("creation", missing)
    marked = known_creation(priority_intent="unknown", priority_argument=3,
                            unknown_fields=["priority_intent"])
    assert record("creation", marked)["payload"] == marked


def test_creation_rejects_omitted_scalar():
    with pytest.raises(schema.BadRecord):
        record("creation", known_creation(priority_argument=3))


def loss(**over):
    payload = dict(reason="dropped", count=1, first_seq=None, last_seq=None,
                   start_wall_ns=None, end_wall_ns=None)
    payload.update(over)
    return payload


def test_loss_roundtrip_and_known_ranges():
    assert record("loss", loss())["payload"] == loss()
    payload = loss(first_seq=2, last_seq=4, start_wall_ns=0, end_wall_ns=5, reason="oversize", count=3)
    assert record("loss", payload)["payload"] == payload


@pytest.mark.parametrize("change", [
    {"extra": 1}, {"reason": "other"}, {"count": 0}, {"count": True}, {"count": -1},
    {"first_seq": 1}, {"last_seq": 1}, {"first_seq": 0, "last_seq": 1},
    {"first_seq": 3, "last_seq": 2}, {"first_seq": True, "last_seq": 1},
    {"start_wall_ns": 1}, {"end_wall_ns": 1}, {"start_wall_ns": -1, "end_wall_ns": 0},
    {"start_wall_ns": 5, "end_wall_ns": 4}, {"reason": None}, {"membership": []},
])
def test_loss_rejects_malformed_and_banned_keys(change):
    payload = loss()
    payload.update(change)
    with pytest.raises(schema.BadRecord):
        record("loss", payload)


def test_membership_records_are_unsupported():
    with pytest.raises(schema.BadRecord, match="unsupported"):
        record("membership", {})
    with pytest.raises(schema.BadRecord, match="unsupported"):
        record("membership", {"roster": [], "extra": 1})
    with pytest.raises(schema.BadRecord, match="unsupported"):
        record("membership", loss())


def test_make_record_stage_index_is_explicit_but_phase_a_rejects_nonnull():
    assert record("creation", creation(), stage_index=None)["stage_index"] is None
    with pytest.raises(schema.BadRecord):
        record("creation", creation(), stage_index=0)


def source_doc(**changes):
    doc = dict(version=1, board="board", source_revision="rev", observed_at=9, valid_until=20,
               owners=[dict(task_id="t1", source="core-lane", ref="r", head="h", expires_at=30)])
    doc.update(changes)
    return doc


def urgency(raw=None, **changes):
    if raw is None:
        raw = json.dumps(source_doc()).encode()
    payload = dict(receipt_type="urgency_read", board="board", read_at_s=10, outcome="valid",
                   raw_b64=base64.b64encode(raw).decode("ascii"),
                   raw_sha256=hashlib.sha256(raw).hexdigest(), raw_complete=True,
                   observed_at=9, valid_until=20, source_revision="rev", owner_ids=["t1"], reason=None,
                   raw_size=len(raw), redacted=False)
    payload.update(changes)
    return payload


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


def _owner(**changes):
    owner = dict(task_id="t1", source="core-lane", ref="r", head="h", expires_at=30)
    owner.update(changes)
    return owner


def _j(**changes):
    return json.dumps(source_doc(**changes)).encode()


BAD_RAW = [
    b"secret-token-abc123", b"[]", b"", b"\xff\xfe", b"null",
    json.dumps(dict(source_doc(), extra=1)).encode(),
    json.dumps({k: v for k, v in source_doc().items() if k != "board"}).encode(),
    _j(version=2), _j(version=True), _j(version=1.0),
    _j(board=""), _j(board=" "), _j(board=1), _j(board="b" * 1025), _j(source_revision=None),
    _j(observed_at="9"), _j(valid_until=True), _j(owners={}), _j(owners=["x"]),
    _j(valid_until=10**400), _j(owners=[_owner(expires_at=10**400)]),
    _j(owners=[_owner(extra="secret")]),
    _j(owners=[{k: v for k, v in _owner().items() if k != "ref"}]),
    _j(owners=[_owner(source="other")]), _j(owners=[_owner(source=["core-lane"])]),
    _j(owners=[_owner(task_id="")]), _j(owners=[_owner(head=3)]), _j(owners=[_owner(expires_at=None)]),
    _j(owners=[_owner()] * 4097),
    b'{"version":1,"version":1,"board":"b","source_revision":"r","observed_at":1,"valid_until":2,"owners":[]}',
    b'{"version":1,"board":"b","source_revision":"r","observed_at":NaN,"valid_until":2,"owners":[]}',
    b'{"version":1,"board":"b","source_revision":"r","observed_at":1,"valid_until":1e999,"owners":[]}',
    b'{"version":1,"board":"b","source_revision":"r","observed_at":1,"valid_until":2,"owners":[],"x":Infinity}',
    b'{"version":1,"board":"b","source_revision":"r","observed_at":1,"valid_until":2,"owners":['
    b'{"task_id":"a","task_id":"b","source":"core-lane","ref":"r","head":"h","expires_at":3}]}',
    b" " * (128 * 1024 + 1),
]


@pytest.mark.parametrize("raw", BAD_RAW, ids=[f"bad{i}" for i in range(len(BAD_RAW))])
def test_owner_generation_rejects_non_source_raw_bytes(raw):
    with pytest.raises(schema.BadRecord):
        record("owner_generation", urgency(raw))


def test_owner_generation_accepts_empty_and_many_owners():
    for owners in ([], [_owner(task_id=f"t{i}") for i in range(1000)]):
        raw = json.dumps(source_doc(owners=owners), separators=(",", ":")).encode()
        payload = urgency(raw, owner_ids=sorted(o["task_id"] for o in owners))
        assert record("owner_generation", payload)["payload"] == payload


def test_owner_generation_redacted_receipt_keeps_digest_without_bytes():
    raw = b"secret-token-abc123"
    payload = urgency(raw, outcome="invalid", raw_b64=None, raw_complete=False, redacted=True,
                      owner_ids=[], reason="invalid_source")
    assert record("owner_generation", payload)["payload"] == payload


@pytest.mark.parametrize("field", ["valid_until", "expires_at"])
def test_huge_source_time_forged_capture_is_incomplete(tmp_path, field):
    import importlib.util
    from pathlib import Path

    from hermes_cli import kanban_fairness_segments as seg
    from hermes_cli import kanban_fairness_store as store

    item = record("owner_generation", urgency())
    seg.append_records(tmp_path, [item])
    manifest = seg.seal_records(tmp_path, expected_sources=seg.receipts(tmp_path)["sources"])
    doc = source_doc()
    if field == "valid_until":
        doc[field] = 10**400
    else:
        doc["owners"][0][field] = 10**400
    item["payload"] = urgency(json.dumps(doc).encode())
    data = seg.encoded(item) + b"\n"
    entry = manifest["segments"][0]
    (tmp_path / entry["name"]).write_bytes(data)
    entry.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
    manifest["bytes"] = len(data)
    manifest["final_seal"] = seg.final_seal(manifest)
    (tmp_path / seg.MANIFEST).write_bytes(seg.encoded(manifest))
    assert store.load(tmp_path)["gaps"]
    spec = importlib.util.spec_from_file_location(
        "fairness_overflow_cli", Path(__file__).resolve().parents[2] / "scripts" / "kanban_fairness.py")
    assert spec is not None and spec.loader is not None
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    assert cli.main(["report", "--input", str(tmp_path), "--mode", "live",
                     "--output", str(tmp_path / "report")]) == 2
