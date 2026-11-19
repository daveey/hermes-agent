"""Phase A capture record envelope: exact keys, strict types, lifecycle payloads."""
from __future__ import annotations

import copy

import pytest

from hermes_cli.kanban_fairness_schema import BadRecord, decode_record, make_record

ENVELOPE_KEYS = ("version", "capture_id", "source_gateway", "board_uid", "source_epoch", "seq", "kind",
                 "wall_ns", "mono_ns", "clock_error_ns", "tx_uid", "tick_uid", "stage_index", "payload")


def _payload(**over):
    p = {"task_id": "t1", "op": "insert", "journal_seq": 7, "journal_kind": "task",
         "database_uid": "db-1", "source_uid": "src-1",
         "fields": {"status": "ready", "priority": 0, "ready_since": 5}}
    p.update(over)
    return p


def _rec(**over):
    base = dict(capture_id="c1", source_gateway="gw", board_uid="b1", source_epoch="e1", seq=1,
                kind="row_change", wall_ns=10, mono_ns=None, clock_error_ns=None, tx_uid=None,
                tick_uid=None, payload=_payload())
    base.update(over)
    return make_record(**base)


def test_roundtrip_and_null_clocks():
    raw = _rec()
    assert tuple(raw) == ENVELOPE_KEYS
    assert raw["version"] == 1 and raw["stage_index"] is None
    assert raw["mono_ns"] is None and raw["clock_error_ns"] is None
    out = decode_record(copy.deepcopy(raw))
    assert out == raw
    full = _rec(mono_ns=0, clock_error_ns=0, tx_uid="x", tick_uid="y")
    assert decode_record(full)["tick_uid"] == "y"


@pytest.mark.parametrize("key", ENVELOPE_KEYS)
def test_every_missing_field_rejected(key):
    raw = _rec()
    del raw[key]
    with pytest.raises(BadRecord):
        decode_record(raw)


def test_unknown_extra_key_and_non_mapping():
    with pytest.raises(BadRecord):
        decode_record({**_rec(), "extra": 1})
    with pytest.raises(BadRecord):
        decode_record([])


@pytest.mark.parametrize("patch", [
    {"version": True}, {"version": 2}, {"version": "1"}, {"version": 1.0},
    {"capture_id": ""}, {"capture_id": 3}, {"source_gateway": ""}, {"board_uid": None},
    {"source_epoch": ""}, {"seq": 0}, {"seq": True}, {"seq": -1}, {"seq": "1"},
    {"wall_ns": None}, {"wall_ns": -1}, {"wall_ns": True}, {"wall_ns": 1.5},
    {"mono_ns": -1}, {"mono_ns": False}, {"mono_ns": "1"},
    {"clock_error_ns": -1}, {"clock_error_ns": True},
    {"tx_uid": ""}, {"tx_uid": 3}, {"tick_uid": ""}, {"tick_uid": 1},
    {"stage_index": 0}, {"stage_index": 1}, {"stage_index": False},
    {"kind": "stage"}, {"kind": "candidate_fact"}, {"kind": "bogus"}, {"kind": None},
    {"payload": None}, {"payload": []},
])
def test_bad_envelope_values(patch):
    raw = {**_rec(), **patch}
    with pytest.raises(BadRecord):
        decode_record(raw)


def test_make_record_rejects_phase_b_kinds():
    for kind in ("stage", "candidate_fact"):
        with pytest.raises(BadRecord):
            _rec(kind=kind, payload={})


@pytest.mark.parametrize("kind", ["membership", "loss"])
def test_other_kinds_take_bounded_json_mapping(kind):
    raw = _rec(kind=kind, payload={"a": [1, {"b": None}], "c": "x"})
    assert decode_record(raw)["payload"] == {"a": [1, {"b": None}], "c": "x"}
    with pytest.raises(BadRecord):
        decode_record({**raw, "payload": {"a": object()}})
    with pytest.raises(BadRecord):
        decode_record({**raw, "payload": {"a": float("nan")}})
    with pytest.raises(BadRecord):
        decode_record({**raw, "payload": {"a": "x" * (300 * 1024)}})
    with pytest.raises(BadRecord):
        decode_record({**raw, "payload": {1: 2}})


@pytest.mark.parametrize("drop", ["task_id", "op", "fields", "journal_seq", "journal_kind", "database_uid",
                                  "source_uid"])
def test_lifecycle_payload_missing_key(drop):
    p = _payload()
    del p[drop]
    with pytest.raises(BadRecord):
        decode_record({**_rec(), "payload": p})


def test_lifecycle_payload_extra_key():
    with pytest.raises(BadRecord):
        decode_record({**_rec(), "payload": _payload(extra=1)})


@pytest.mark.parametrize("patch", [
    {"task_id": ""}, {"task_id": 3}, {"journal_seq": 0}, {"journal_seq": True}, {"journal_seq": "1"},
    {"journal_kind": "bogus"}, {"journal_kind": "opening"},
    {"database_uid": ""}, {"database_uid": None}, {"database_uid": 4},
    {"source_uid": ""}, {"source_uid": 12}, {"source_uid": True},
    {"op": "upsert"}, {"op": None}, {"fields": []}, {"fields": None},
    {"fields": {"priority": 0}},
    {"fields": {"status": "ready", "priority": True}},
    {"fields": {"status": "ready", "ready_since": "x"}},
])
def test_lifecycle_payload_malformed(patch):
    with pytest.raises(BadRecord):
        decode_record({**_rec(), "payload": _payload(**patch)})


def test_null_source_uid_accepted_and_delete_needs_no_status():
    assert decode_record(_rec(payload=_payload(source_uid=None)))["payload"]["source_uid"] is None
    assert decode_record(_rec(payload=_payload(op="delete", fields={})))


def test_event_and_opening_payloads():
    ev = _payload(journal_kind="event", op="insert", fields={"kind": "claimed", "created_at": 3})
    assert decode_record(_rec(payload=ev))
    with pytest.raises(BadRecord):
        decode_record(_rec(payload=_payload(journal_kind="event", fields={"kind": "claimed"})))
    with pytest.raises(BadRecord):
        decode_record(_rec(payload=_payload(journal_kind="event", fields={"kind": 3, "created_at": 3})))
    op = _payload(journal_kind="opening", op=None)
    assert decode_record(_rec(kind="opening", payload=op))
    with pytest.raises(BadRecord):
        decode_record(_rec(kind="opening", payload=_payload()))  # journal_kind task under opening
    with pytest.raises(BadRecord):
        decode_record(_rec(kind="row_change", payload=op))
