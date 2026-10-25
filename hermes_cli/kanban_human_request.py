"""Human-facing escalation evidence, separate from execution authorization.

Missing/legacy evidence is an operational issue owned by the assignee, not a
request for approval. Classification never changes task or execution state.
"""
from typing import Any

INTERACTIONS = frozenset({"approval", "reply", "user_terminal", "physical", "external"})
FIELDS = ("action", "why_human", "interaction", "scope", "evidence")
NONHUMAN_KINDS = frozenset({"dependency", "transient", "time"})


def validate_human_request(value: Any, *, kind: str | None = None) -> dict | None:
    """Validate explicitly supplied evidence; never manufacture it from prose."""
    if value is None:
        return None
    if not isinstance(value, dict) or any(
        not isinstance(value.get(field), str) or not value[field].strip() for field in FIELDS
    ):
        raise ValueError("human_request requires action, why_human, interaction, scope and evidence")
    if value["interaction"] not in INTERACTIONS:
        raise ValueError("human_request interaction must be approval, reply, user_terminal, physical or external")
    if kind in NONHUMAN_KINDS:
        raise ValueError("human_request cannot accompany dependency, transient or time waits")
    return {field: value[field].strip() for field in FIELDS}


def needs_human(value: Any, *, kind: str | None = None) -> bool:
    """Defensive read-side predicate shared by local and replica consumers."""
    try:
        return validate_human_request(value, kind=kind) is not None
    except ValueError:
        return False


def validate_initial_request(value, initial_status, triage):
    """Creation evidence is meaningful only for an explicitly parked card."""
    request = validate_human_request(value)
    if request is not None and (initial_status != "blocked" or triage):
        raise ValueError("human_request requires initial_status='blocked' without triage")
    return request
