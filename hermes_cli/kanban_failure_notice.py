"""Failure-notice protocol shared by chat consumers. [daveey]"""
from __future__ import annotations


def provisional_failure(event) -> bool:
    """Upgraded producers retain raw diagnostics, but only finalization is public.

    Explicit producer opt-in retains compatibility with historical/legacy events;
    no batching or assumption that a later final event has already arrived.
    """
    return (getattr(event, "kind", "") in {"crashed", "reclaimed", "timed_out", "gave_up", "spawn_failed", "protocol_violation"}
            and (getattr(event, "payload", None) or {}).get("failure_notice") == "canonical")


def event_key(board: str, event) -> str:
    """Portable run identity for the transcript's existing durable dedup ledger."""
    payload = getattr(event, "payload", None) or {}
    if getattr(event, "kind", "") == "failure_finalized" and payload.get("run_uid"):
        return f"{board}:failure:{payload['run_uid']}"
    return f"{board}:{int(event.id)}"


def failure_uid(event):
    payload = getattr(event, "payload", None) or {}
    return payload.get("run_uid") if getattr(event, "kind", "") == "failure_finalized" else None


def fresh_events(events, delivered):
    seen = set(delivered)
    result = []
    for event in events:
        uid = failure_uid(event)
        if uid and uid in seen:
            continue
        result.append(event)
        if uid:
            seen.add(uid)
    return result
