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
    # Only the named fields are kept. In particular a caller-supplied ``release_target`` (the release offer the
    # stranded-park brief stamps on its own ask, kanban_release.offer) never survives validation: every agent,
    # tool and CLI ingress passes through here, so no supplied request can make an Unblock click a release.
    request = {field: value[field].strip() for field in FIELDS}
    if value.get("choices") is not None:
        request["choices"] = _validate_choices(value["choices"])
    request.update(_validate_resolved_check(value))
    return request


# [daveey] ``resolved_check``: a probe that exits 0 once David's step happened outside Hermes (kanban_ask_resolved).
RESOLVED_CHECK_EVERY_DEFAULT, RESOLVED_CHECK_EVERY_MIN, RESOLVED_CHECK_EVERY_MAX = 900, 120, 21600
RESOLVED_CHECK_MAX_CHARS = 2000
RESOLVED_CHECK_SECRET = "resolved_check_secret"


class ResolvedCheckRefused(ValueError):
    """A resolved_check refused before or at filing; ``code`` is the stable refusal code (``kanban_ask_resolved``).
    ``str()`` is the whole message, starting with the code; tool handlers render ``tool_error_fields``."""

    def __init__(self, message: str, code: str):
        self.code = code
        self.tool_error_fields = {"rejection": code}
        super().__init__(message)


def _refuse_secret(cmd: str) -> None:
    """The stored ask is redacted (``kanban_db.redact_review_value``) and the sweep runs the stored text, so a
    probe whose redacted form differs would be stored broken, and with its secret if it were not redacted. Refuse
    it before anything runs it; the message shows only the redacted form."""
    from agent.redact import redact_sensitive_text

    redacted = redact_sensitive_text(cmd, force=True)
    if redacted == cmd:
        return
    raise ResolvedCheckRefused(
        f"{RESOLVED_CHECK_SECRET}: human_request.resolved_check contains a secret or a credential-shaped value "
        f"(stored it would read `{redacted}`, a different command), so it was not run or filed. Never inline a "
        "secret: put the check in a script the probe runs (e.g. `sh ~/bin/check-pairing.sh`) that fetches the "
        "secret itself (`aws secretsmanager get-secret-value ...`, token-broker), or read a header file "
        "(`curl -H @$HOME/.hub-auth ...`).", RESOLVED_CHECK_SECRET)


def _validate_resolved_check(value: dict) -> dict:
    cmd, every = value.get("resolved_check"), value.get("resolved_check_every")
    if cmd is None:
        if every is not None:
            raise ValueError("human_request resolved_check_every needs a resolved_check")
        return {}
    if not isinstance(cmd, str) or not cmd.strip():
        raise ValueError("human_request resolved_check must be a non-empty shell command string")
    if len(cmd.strip()) > RESOLVED_CHECK_MAX_CHARS:
        raise ValueError(f"human_request resolved_check is longer than {RESOLVED_CHECK_MAX_CHARS} chars; "
                         "put it in a script")
    _refuse_secret(cmd)  # the raw text: redact(raw) == raw is what makes the stored command the filed one
    if every is None:
        every = RESOLVED_CHECK_EVERY_DEFAULT
    if (not isinstance(every, int) or isinstance(every, bool)
            or not RESOLVED_CHECK_EVERY_MIN <= every <= RESOLVED_CHECK_EVERY_MAX):
        raise ValueError(f"human_request resolved_check_every must be an integer number of seconds between "
                         f"{RESOLVED_CHECK_EVERY_MIN} and {RESOLVED_CHECK_EVERY_MAX}")
    return {"resolved_check": cmd.strip(), "resolved_check_every": every}


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
