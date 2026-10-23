"""Plain-language classification of kanban ``gave_up`` / ``crashed`` events for user-facing notices.

The dispatcher records every non-success worker outcome as ``crashed`` (process died, any exit code) or
``spawn_failed`` (the launch itself failed) and then, after the failure budget, ``gave_up``. A worker that
ran for minutes and then exited because the model refused the request is a ``crashed`` outcome with the
refusal text in ``error`` / ``worker_output`` — it is NOT a spawn failure and the user can do nothing about it.

This module only reads event payloads and words them. It never retries, re-routes providers, or touches
safety refusals: a refusal is reported as a refusal and left to the dispatcher's normal bounded policy.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

# Provider/model refusals. Deliberately specific: a bare "refused" also matches "connection refused".
_REFUSAL_RE = re.compile(
    r"model declined|safety refusal|declined (?:to|the) (?:assist|help|comply|answer|request)"
    r"|refus(?:ed|al) (?:to|by the model)|(?:model|assistant) refus|stop_reason\W+refusal"
    r"|cannot (?:assist|help) with (?:that|this)|can'?t (?:assist|help) with (?:that|this)"
    r"|violates? (?:our )?(?:usage|safety) polic",
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
    cause = _CAUSE[cls]
    failures = _as_int(payload.get("failures"))
    limit = _as_int(payload.get("effective_limit"))
    attempts = ""
    if failures:
        attempts = f"{failures} of {limit}" if limit else str(failures)

    retry = str(payload.get("retry_status") or "")
    if kind == "gave_up":
        state = (f"stopped after {attempts} failed attempt(s); task is Blocked" if attempts
                 else "stopped after repeated failed attempts; task is Blocked")
    elif retry == "blocked" or cls == "provider_config":
        state = "task is Blocked"
    elif retry_promised(payload):
        # Only the recorded retry policy may promise a retry.
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
