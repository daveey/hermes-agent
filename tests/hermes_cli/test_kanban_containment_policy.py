"""Containment wire validation is strict and never confers lifecycle authority."""
from __future__ import annotations

import copy

import pytest

from hermes_cli.kanban_containment_policy import ContainmentPolicyError, identity, policy

TARGET = {"gateway": "fixture-home", "board": "work", "task_id": "t_11111111"}
AUTHORITY = {"gateway": "fixture-home", "board": "work", "task_id": "t_22222222"}
PRODUCER = {"gateway": "fixture-peer", "board": "work", "task_id": "t_33333333"}


def test_policy_is_a_detached_exact_copy_not_an_authorization():
    original = {"version": 1, "authority": dict(AUTHORITY), "producer": dict(PRODUCER)}
    result = policy(original, target=TARGET)
    assert result == original
    original["authority"]["gateway"] = "changed"
    original["producer"]["task_id"] = "t_44444444"
    assert result == {"version": 1, "authority": AUTHORITY, "producer": PRODUCER}
    assert policy({"version": 1, "authority": AUTHORITY}, target=TARGET) == {
        "version": 1, "authority": AUTHORITY,
    }


@pytest.mark.parametrize("value", [None, [], "authority", 1, True])
def test_non_object_policy_is_refused(value):
    with pytest.raises(ContainmentPolicyError):
        policy(value, target=TARGET)


@pytest.mark.parametrize("version", [None, True, False, 1.0, "1", 0, 2])
def test_version_is_exact_integer_one(version):
    with pytest.raises(ContainmentPolicyError, match="version"):
        policy({"version": version, "authority": AUTHORITY}, target=TARGET)


@pytest.mark.parametrize("removed", ["version", "authority"])
def test_required_policy_keys(removed):
    value = {"version": 1, "authority": AUTHORITY}
    del value[removed]
    with pytest.raises(ContainmentPolicyError):
        policy(value, target=TARGET)


def test_unknown_predicate_is_not_silently_ignored():
    with pytest.raises(ContainmentPolicyError):
        policy({"version": 1, "authority": AUTHORITY, "timer": 60}, target=TARGET)


@pytest.mark.parametrize("field", ["gateway", "board", "task_id"])
@pytest.mark.parametrize("bad", [None, [], {}, True, "", " ", "bad value", "\n"])
def test_identity_field_types_and_grammar(field, bad):
    value = dict(AUTHORITY, **{field: bad})
    with pytest.raises(ContainmentPolicyError):
        identity(value)


@pytest.mark.parametrize("value", [None, [], "task", {}, {**AUTHORITY, "profile": "default"},
                                   {"gateway": "fixture-home", "task_id": "t_22222222"}])
def test_identity_requires_exact_object_keys(value):
    with pytest.raises(ContainmentPolicyError):
        identity(value)


@pytest.mark.parametrize("bad", ["t_ABCDEF12", "t_1234567", "t_123456789abcdef01", " t_12345678",
                                 "t_12345678\n", "t_12345678 ", "12345678"])
def test_task_tokens_are_not_normalized(bad):
    with pytest.raises(ContainmentPolicyError):
        identity({**AUTHORITY, "task_id": bad})


@pytest.mark.parametrize("field", ["authority", "producer"])
def test_same_task_id_on_another_gateway_is_still_self(field):
    value = {"version": 1, "authority": AUTHORITY, field: {**TARGET, "gateway": "fixture-peer"}}
    with pytest.raises(ContainmentPolicyError, match="own"):
        policy(value, target=TARGET)


@pytest.mark.parametrize("field", ["authority", "producer"])
def test_cross_board_predicates_are_refused(field):
    value = {"version": 1, "authority": AUTHORITY, field: {**PRODUCER, "board": "other"}}
    with pytest.raises(ContainmentPolicyError, match="board"):
        policy(value, target=TARGET)


def test_different_task_on_another_gateway_is_allowed():
    authority = {**AUTHORITY, "gateway": "fixture-peer"}
    assert policy({"version": 1, "authority": authority}, target=TARGET)["authority"] == authority


@pytest.mark.parametrize("field", ["authority", "producer"])
def test_self_resume_routes_are_refused(field):
    value = {"version": 1, "authority": AUTHORITY, field: copy.deepcopy(TARGET)}
    with pytest.raises(ContainmentPolicyError, match="own"):
        policy(value, target=TARGET)


def test_explicit_null_producer_is_not_an_omission():
    with pytest.raises(ContainmentPolicyError):
        policy({"version": 1, "authority": AUTHORITY, "producer": None}, target=TARGET)


@pytest.mark.parametrize("field", ["gateway", "board"])
def test_identity_name_bounds_and_literal_preservation(field):
    value = {**AUTHORITY, field: "A" + "-" * 63}
    assert identity(value) == value
    with pytest.raises(ContainmentPolicyError):
        identity({**value, field: value[field] + "x"})
