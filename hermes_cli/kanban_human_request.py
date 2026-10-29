"""Human-facing escalation evidence, separate from execution authorization.

Missing/legacy evidence is an operational issue owned by the assignee, not a
request for approval. Classification never changes task or execution state.
"""
from typing import Any

INTERACTIONS = frozenset({"approval", "reply", "user_terminal", "physical", "external"})
FIELDS = ("action", "why_human", "interaction", "scope", "evidence")
NONHUMAN_KINDS = frozenset({"dependency", "transient", "time"})
# [daveey] Optional decision buttons for a blocked card (session-outcome contract): 2-6 short {label, value}.
MIN_CHOICES, MAX_CHOICES = 2, 6
CHOICE_LABEL_MAX, CHOICE_VALUE_MAX = 60, 120


def _validate_choices(value: Any) -> list[dict]:
    if not isinstance(value, list) or not MIN_CHOICES <= len(value) <= MAX_CHOICES:
        raise ValueError(f"human_request choices must be a list of {MIN_CHOICES}-{MAX_CHOICES} {{label, value}} items")
    out = []
    for choice in value:
        label = choice.get("label") if isinstance(choice, dict) else None
        val = choice.get("value") if isinstance(choice, dict) else None
        if (not isinstance(label, str) or not label.strip() or len(label.strip()) > CHOICE_LABEL_MAX
                or not isinstance(val, str) or not val.strip() or len(val.strip()) > CHOICE_VALUE_MAX):
            raise ValueError(f"each human_request choice needs a label (<= {CHOICE_LABEL_MAX} chars) and a value "
                             f"(<= {CHOICE_VALUE_MAX} chars)")
        out.append({"label": label.strip(), "value": val.strip()})
    if len({c["value"] for c in out}) != len(out):
        raise ValueError("human_request choice values must be distinct")
    return out


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
    request = {field: value[field].strip() for field in FIELDS}
    if value.get("choices") is not None:
        request["choices"] = _validate_choices(value["choices"])
    return request


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


def reject_timed_request(value):
    """Automatic wake-up and a pending human action cannot share a block."""
    if value is not None:
        raise ValueError("human_request cannot be combined with automatic timed resumption")
