"""Strict wire policy for non-human containment; authorization is separate.

Parsing this policy NEVER grants permission or releases a task. Mutation callers
must additionally establish current scoped authority or new producer evidence.
"""
from __future__ import annotations

import re
from typing import Any

_IDENTITY_KEYS = frozenset({"gateway", "board", "task_id"})
_POLICY_KEYS = frozenset({"version", "authority", "producer"})
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_TASK = re.compile(r"t_[0-9a-f]{8,16}\Z")


class ContainmentPolicyError(ValueError):
    """Malformed, ambiguous or self-authorizing containment policy."""

    code = "invalid_containment_policy"


def identity(value: Any) -> dict[str, str]:
    """Copy one qualified task identity without repairing malformed identifiers."""
    if not isinstance(value, dict) or value.keys() != _IDENTITY_KEYS:
        raise ContainmentPolicyError("identity requires exactly gateway, board and task_id")
    for key, pattern in (("gateway", _NAME), ("board", _NAME), ("task_id", _TASK)):
        text = value[key]
        if not isinstance(text, str) or not pattern.fullmatch(text):
            raise ContainmentPolicyError(f"invalid identity {key}")
    return dict(value)


def policy(value: Any, *, target: Any) -> dict[str, Any]:
    """A version-1 policy naming a distinct authority and optional distinct producer.

    Keep the returned value independent of the caller's mutable objects: the
    containment writer freezes it into its durable episode. Unknown keys fail
    closed instead of silently promising a predicate the verifier cannot enforce.
    """
    subject = identity(target)
    if not isinstance(value, dict) or not {"version", "authority"} <= value.keys() <= _POLICY_KEYS:
        raise ContainmentPolicyError("policy requires version and authority; only producer is optional")
    if type(value["version"]) is not int or value["version"] != 1:
        raise ContainmentPolicyError("unsupported containment policy version")
    authority = identity(value["authority"])
    if authority["board"] != subject["board"]:
        raise ContainmentPolicyError("authority must be on the contained task's board")
    # Task IDs are board-global; gateway is an observed home, not identity.
    # Rehome must neither strand a grant nor permit self-authorization.
    if authority["task_id"] == subject["task_id"]:
        raise ContainmentPolicyError("contained task cannot authorize its own resumption")
    result: dict[str, Any] = {"version": 1, "authority": authority}
    if "producer" in value:
        producer = identity(value["producer"])
        if producer["board"] != subject["board"]:
            raise ContainmentPolicyError("producer must be on the contained task's board")
        if producer["task_id"] == subject["task_id"]:
            raise ContainmentPolicyError("contained task cannot be its own readiness producer")
        result["producer"] = producer
    return result
