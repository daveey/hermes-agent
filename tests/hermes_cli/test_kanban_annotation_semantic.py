"""Display semantic tokens must never certify strict replica observations."""
from copy import deepcopy
import hashlib
import json

import pytest

from hermes_cli import kanban_annotation_semantic as semantic


def signed(**updates):
    task = {"id": "t_one", "status": "done", "activity_at": 1, "activity_kind": "heartbeat",
            "sessions": [{"gw": "origin", "profile": "default", "session": "s", "rel": "shepherd"}]}
    task.update(updates)
    task["origin_observation"] = {"schema": 1, "verified": True, "observed_at": 10,
        "task_hash": hashlib.sha256(json.dumps(task, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}
    return task


def test_liveness_resign_is_equal_without_mutating_input():
    first = signed()
    saved = deepcopy(first)
    second = signed(activity_at=2, activity_kind="lease")
    assert semantic.task_token(first) == semantic.task_token(second)
    assert first == saved


@pytest.mark.parametrize("field,value", [("status", "running"), ("title", "new"),
    ("sessions", []), ("origin_bindings", [{"binding_id": "new"}]), ("unknown_future_field", 1)])
def test_substantive_payload_remains_visible(field, value):
    assert semantic.task_token(signed()) != semantic.task_token(signed(**{field: value}))


@pytest.mark.parametrize("field,value", [("observed_at", 11), ("verified", False),
    ("schema", 2), ("receipt", "new"), ("task_hash", "invalid")])
def test_evidence_changes_remain_visible(field, value):
    before = signed()
    after = deepcopy(before)
    after["origin_observation"][field] = value
    assert semantic.task_token(before) != semantic.task_token(after)


def test_invalid_hashes_are_not_normalized():
    first = signed()
    first["origin_observation"]["task_hash"] = "bad-one"
    second = deepcopy(first)
    second["origin_observation"]["task_hash"] = "bad-two"
    assert semantic.task_token(first) != semantic.task_token(second)


def test_liveness_edit_without_resign_breaks_validity():
    first = signed()
    second = deepcopy(first)
    second["activity_at"] = 2
    assert semantic.task_token(first) != semantic.task_token(second)


@pytest.mark.parametrize("observation", [None, [], "bad", {"schema": True}, {}])
def test_malformed_evidence_preserved(observation):
    first = signed()
    first["origin_observation"] = observation
    other = deepcopy(first)
    other["origin_observation"] = {"schema": "other"}
    assert semantic.task_token(first) != semantic.task_token(other)


def test_key_order_does_not_change_token():
    task = signed()
    assert semantic.task_token(task) == semantic.task_token(dict(reversed(list(task.items()))))


def test_numeric_type_changes_not_erased_by_python_equality():
    assert semantic.task_token(signed(priority=1)) != semantic.task_token(signed(priority=True))


def test_non_json_and_non_finite_are_unkeyable():
    with pytest.raises((TypeError, ValueError)):
        semantic.task_token(signed(extra=object()))
    task = signed()
    task["extra"] = float("nan")
    with pytest.raises(ValueError):
        semantic.task_token(task)
