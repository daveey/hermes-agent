"""Shared failure classification and notices for Kanban run finalization.

Provider content refusals stop for review, never automatic retries or rerouting.
This module only reads evidence; the failure accountant applies the disposition.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

# Provider/model refusals. Deliberately specific: a bare "refused" also matches "connection refused".
_REFUSAL_RE = re.compile(
    r"model declined|safety refusal|declined (?:to|the) (?:assist|help|comply|answer|request)"
    r"|refusal by the model|(?:model|assistant) refus|stop_reason\W+refusal"
    r"|cannot (?:assist|help) with (?:that|this)|can'?t (?:assist|help) with (?:that|this)"
    r"|violates? (?:our )?(?:usage|safety) polic"
    r"|This content was flagged for possible cybersecurity risk",
    re.IGNORECASE,
)

NO_USER_ACTION = "Hermes needs to recover; no user action identified."


def clean_text(value: Any, limit: int) -> str:
    """Strip ANSI/control bytes, collapse whitespace, clip."""
    text = _CTRL_RE.sub(" ", _ANSI_RE.sub("", str(value or "")))
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def classify_failure(kind: str, payload: Mapping[str, Any]) -> str:
    """One of: ``provider_refusal``, ``provider_config``, ``protocol_violation``, ``spawn_failed``,
    ``timed_out``, ``crash``."""
    haystack = " ".join(str(payload.get(k) or "") for k in ("error", "worker_output", "reason"))
    trigger = str(payload.get("trigger_outcome") or ("crashed" if kind == "crashed" else ""))
    if payload.get("terminal_provider"):
        return "provider_config"
    if trigger == "spawn_failed":
        return "spawn_failed"
    if _REFUSAL_RE.search(_ANSI_RE.sub("", haystack)):
        return "provider_refusal"
    if payload.get("protocol_violation") or payload.get("protocol_violations"):
        return "protocol_violation"
    if trigger == "timed_out":
        return "timed_out"
    return "crash"


_CAUSE = {
    "provider_refusal": "the model refused the request (provider safety refusal), not a launch failure",
    "provider_config": "the provider reported an unrecoverable configuration problem for this profile",
    "protocol_violation": "the worker finished without reporting a result to the board",
    "spawn_failed": "the worker could not be launched",
    "timed_out": "the worker hit its time limit",
    "crash": "the worker process exited unexpectedly",
}


def _details(payload: Mapping[str, Any]) -> str:
    parts = []
    if payload.get("pid") is not None:
        parts.append(f"pid {payload['pid']}")
    if payload.get("exit_code") is not None:
        parts.append(f"exit {payload['exit_code']}")
    if payload.get("exit_kind"):
        parts.append(str(payload["exit_kind"]))
    for key in ("error", "worker_output"):
        if payload.get(key):
            parts.append(clean_text(payload[key], 300))
            break
    return "; ".join(parts)


def retry_promised(payload: Mapping[str, Any]) -> bool:
    """True only when the event records that the task went back to a dispatchable phase."""
    return str(payload.get("retry_status") or "") in ("ready", "review")


def describe_failure(kind: str, title: str, payload: Mapping[str, Any]) -> str:
    """Notice body (everything after ``Kanban <id>``) for a ``gave_up`` or ``crashed`` event."""
    cls = classify_failure(kind, payload)
    if cls == "provider_refusal":
        raw = str(payload.get("worker_output") or payload.get("error") or payload.get("reason") or "")
        message = raw.partition("Provider said:")[2].strip() if "Provider said:" in raw else raw
        message = clean_text(message[:200], 201)
        return (
            f" Provider {clean_text(payload.get('provider') or 'unknown', 100)} "
            f"({clean_text(payload.get('model') or 'unknown', 100)}) refused the content of run "
            f"{payload.get('run_number', payload.get('run_uid', 'unknown'))}: '{message}'. "
            "Not a worker crash; no automatic retry. Next: read what the run was doing, "
            "rephrase or re-scope the card in neutral wording, then promote it, or archive it."
        )
    cause = _CAUSE[cls]
    failures = _as_int(payload.get("failures"))
    limit = _as_int(payload.get("effective_limit"))
    attempts = ""
    if failures:
        attempts = f"{min(failures, limit)} of {limit}" if limit else str(failures)

    retry = str(payload.get("final_status") if kind == "failure_finalized" else payload.get("retry_status") or "")
    if kind == "failure_finalized" and retry == "triage":
        state = (f"stopped after {attempts} failed attempt(s); task is in automatic triage" if attempts
                 else "task is in automatic triage")
    elif kind == "gave_up":
        state = (f"stopped after {attempts} failed attempt(s); task is Blocked" if attempts
                 else "stopped after repeated failed attempts; task is Blocked")
    elif retry == "blocked" or cls == "provider_config":
        state = "task is Blocked"
    elif retry in ("ready", "review"):
        # Only the recorded final disposition may promise a retry.
        state = f"task returned to {retry}; the dispatcher's normal retry policy applies"
    else:
        state = "retry state was not recorded"

    if cls == "provider_config":
        # Exit 78 is raised for several provider reasons (auth, model not found, TLS, upstream
        # block) and the event does not record which one, so do not name a specific cause.
        who = "Check this profile's provider configuration (the worker log has the exact reason), then unblock the task."
    elif cls == "provider_refusal":
        who = "The model's refusal needs review before this task is retried; it is reported as-is."
    else:
        who = NO_USER_ACTION

    head = f' "{title}" failed: {cause}; {state}. {who}'
    if details := _details(payload):
        head += f"\nDetails: {details}"
    return head
