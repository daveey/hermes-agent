"""Direct parser/reducer contracts avoid upstream validators masking clauses."""
from __future__ import annotations

import copy

import pytest

from hermes_cli import kanban_containment_policy as policy
from hermes_cli import kanban_containment_state as state
from hermes_cli.kanban_db_sync import uuid7
from tests.hermes_cli import test_kanban_containment_state as fixtures


@pytest.mark.parametrize("fault", ["upper", "hex", "v4", "type"])
def test_uid_requires_exact_canonical_uuid7(fault):
    uid = "019a98b4-0000-7000-8000-00000000000a"
    assert state._uid(uid) is True
    malformed = {"upper": uid.upper(), "hex": uid.replace("-", ""),
                 "v4": uid.replace("-7000-", "-4000-"), "type": 7}[fault]
    assert state._uid(malformed) is False


@pytest.mark.parametrize("key", ["_recovery_history_v1", "_wake_episode_v1"])
@pytest.mark.parametrize("value", [[], None, "invalid", 1])
def test_append_owned_keys_require_object_and_do_not_change_predicate(key, value):
    event = fixtures.stop()
    expected = copy.deepcopy(event["payload"])
    event["payload"][key] = {"fixture": True}
    assert state._payload(event) == expected
    event["payload"][key] = value
    assert state._payload(event) is None


def test_no_verifier_never_releases_and_unknown_episode_never_reaches_verifier():
    stop = fixtures.stop()
    receipt = fixtures.release(stop)
    assert state.reduce_events([stop, receipt]).episodes == (stop["uid"],)
    seen = []
    def verifier(*args):
        seen.append(args)
        return True
    assert state.reduce_events([stop, receipt], verify_release=verifier) is None
    assert len(seen) == 1
    seen.clear()
    receipt["payload"]["episode"] = uuid7()
    assert state.reduce_events([stop, receipt], verify_release=verifier).episodes == (stop["uid"],)
    assert seen == []


@pytest.mark.parametrize("field,value", [
    ("gateway", "bad/name"), ("board", "bad/name"), ("task_id", "T_0123456789abcdef"),
    ("gateway", 1), ("board", None), ("task_id", 1),
])
def test_identity_checks_each_exact_field_pattern(field, value):
    identity = dict(fixtures.TARGET)
    assert policy.identity(identity) == identity
    identity[field] = value
    with pytest.raises(policy.ContainmentPolicyError, match="invalid identity " + field):
        policy.identity(identity)


@pytest.mark.parametrize("fault", ["extra", "missing", "type"])
def test_identity_requires_exact_key_set(fault):
    identity = dict(fixtures.TARGET)
    if fault == "extra":
        identity["unknown"] = 1
    elif fault == "missing":
        del identity["gateway"]
    else:
        identity = []
    with pytest.raises(policy.ContainmentPolicyError, match="requires exactly"):
        policy.identity(identity)


@pytest.mark.parametrize("version", [True, 2])
def test_policy_version_is_exact_integer_one(version):
    spec = {"version": 1, "authority": dict(fixtures.AUTHORITY)}
    assert policy.policy(spec, target=fixtures.TARGET) == spec
    spec["version"] = version
    with pytest.raises(policy.ContainmentPolicyError, match="unsupported containment policy version"):
        policy.policy(spec, target=fixtures.TARGET)


def test_producer_cannot_cross_board_even_when_authority_matches():
    spec = {"version": 1, "authority": dict(fixtures.AUTHORITY), "producer": dict(fixtures.PRODUCER)}
    assert policy.policy(spec, target=fixtures.TARGET) == spec
    spec["producer"]["board"] = "other"
    with pytest.raises(policy.ContainmentPolicyError, match="producer must be on"):
        policy.policy(spec, target=fixtures.TARGET)
