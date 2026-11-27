"""Pure canonical session-status reducer; no transcript, filesystem, clock or UI inputs.

Adapters collect qualified observations. A revision certifies exactly those observations,
not cross-source atomicity. Unknown/expired or epoch-inconsistent evidence cannot certify
completion. Persistence must CAS the captured owner/generation before publishing this value.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

STATUS_VERSION = 1
REQUIRED_SOURCES = frozenset({"cards", "controls", "turn", "subagents", "processes", "goals"})
_IDENTITY_KEYS = ("gateway", "profile", "session_id", "owner_epoch")
_SUCCESS = frozenset({"succeeded", "done", "completed"})
_TURN_REASONS = frozenset({"notice_tail", "turn_unsettled", "subagent_killed"})
_OPEN = frozenset({"ready", "todo", "running", "waiting", "blocked", "scheduled", "review", "triage",
                   "external_running", "external_unknown", "source_unknown", "pending", "queued", "producer_wait", "triage_overdue"})


def _identity(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or any(not isinstance(value.get(k), str) or not value[k] for k in _IDENTITY_KEYS):
        raise ValueError("qualified identity requires gateway/profile/session_id/owner_epoch")
    return {key: value[key] for key in _IDENTITY_KEYS}


def _generation(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("generation must be a nonnegative integer")
    return value


def _coverage(snapshot: dict, identity: dict, generation: int) -> tuple[dict, dict]:
    sources = snapshot.get("sources") or {}
    required = REQUIRED_SOURCES | set(snapshot.get("required_sources") or ())
    reasons, revisions = [], {}
    for name in sorted(required):
        reading = sources.get(name)
        if not isinstance(reading, dict):
            reasons.append(f"{name}:missing")
            continue
        revision = reading.get("revision")
        revisions[name] = revision
        if reading.get("identity") != identity:
            reasons.append(f"{name}:owner_mismatch")
        if reading.get("generation") != generation:
            reasons.append(f"{name}:generation_mismatch")
        if reading.get("coverage") != "complete" or reading.get("expired"):
            reasons.append(f"{name}:{reading.get('reason') or 'unknown'}")
        if revision is None or reading.get("current_revision") != revision:
            reasons.append(f"{name}:inconsistent_revision")
    return {"state": "unknown" if reasons else "complete", "reasons": reasons}, revisions


def _failures(snapshot: dict, previous: dict, generation: int, coverage: dict) -> list[dict]:
    """Lost execution evidence survives inventory removal and pending successor requests."""
    turn = snapshot.get("turn") or {}
    work = snapshot.get("work") or []
    retained = previous.get("failures") or []
    successful_successor = (
        turn.get("generation") == generation and turn.get("state") in _SUCCESS
        and all(generation > failure["generation"] for failure in retained)
        and coverage["state"] == "complete"
        and all(item.get("state") in _SUCCESS or (
            item.get("passive") is True and item.get("state") in {"watching", "scheduled"}) for item in work)
    )
    if successful_successor:
        # A new successful turn is not evidence that a missing child was handled.
        retained = [f for f in retained if f["id"].startswith("subagent:")]
    current = [{"id": str(item.get("id") or "unknown"), "generation": generation, "state": item["state"]}
               for item in work if item.get("state") in {"failed", "lost", "cancelled"}]
    if turn.get("state") == "failed":
        current.append({"id": "own_turn", "generation": generation, "state": "failed"})
    by_key = {(item["id"], item["generation"]): deepcopy(item) for item in [*retained, *current]}
    return sorted(by_key.values(), key=lambda item: (item["generation"], item["id"]))


def _verdict(snapshot: dict, coverage: dict, generation: int, failures: list[dict]) -> dict:
    turn = snapshot.get("turn") or {}
    own_turn = turn.get("generation") == generation
    asks = snapshot.get("asks") or []
    if snapshot.get("muted"):
        return {"kind": "muted", "presentation": {"stripe": "blocked", "reason_codes": ["muted"]}}
    if asks:
        return {"kind": "decision", "items": deepcopy(asks),
                "presentation": {"stripe": "stuck", "reason_codes": ["human_ask"]}}
    if own_turn and turn.get("state") == "running":
        return {"kind": "running", "presentation": {"stripe": "waiting", "reason_codes": ["own_turn_active"]}}
    work = [item for item in snapshot.get("work") or () if not (
        item.get("passive") is True and item.get("state") in {"watching", "scheduled"})]
    open_work = [item for item in work if item.get("state") in _OPEN]
    if any(item.get("kind") == "card" and item.get("state") == "running" for item in open_work):
        return {"kind": "running", "on": deepcopy(open_work),
                "presentation": {"stripe": "waiting", "reason_codes": ["linked_work_running"]}}
    if any(f["id"].startswith("subagent:") for f in failures):
        return {"kind": "unresolved", "reason": "linked_work_unsuccessful",
                "presentation": {"stripe": "attention", "reason_codes": ["linked_work_unsuccessful"]}}
    if any(item.get("state") == "source_unknown" for item in open_work):
        return {"kind": "waiting", "on": deepcopy(open_work),
                "presentation": {"stripe": "none", "reason_codes": ["linked_status_unknown"]}}
    if any(item.get("state") == "producer_wait" for item in open_work):
        return {"kind": "waiting", "on": deepcopy(open_work),
                "presentation": {"stripe": "external", "reason_codes": ["linked_card_producer_wait"]}}
    if any(item.get("state") in ("external_unknown", "external_running") for item in open_work):
        return {"kind": "waiting", "on": deepcopy(open_work),
                "presentation": {"stripe": "external", "reason_codes": ["linked_ci_deploy_wait"]}}
    if open_work:
        return {"kind": "waiting", "on": deepcopy(open_work),
                "presentation": {"stripe": "waiting", "reason_codes": ["linked_work_open"]}}
    reason = None
    if coverage["state"] != "complete":
        reason = "source_unknown"
    elif failures or any(item.get("state") not in _SUCCESS for item in work):
        linked = (any(item["id"] != "own_turn" for item in failures)
                  or any(item.get("state") not in _SUCCESS for item in work))
        reason = "linked_work_unsuccessful" if linked else "turn_failed"
    elif not snapshot.get("started") and turn.get("state") == "never_started":
        return {"kind": "unresolved", "reason": "empty",
                "presentation": {"stripe": "none", "reason_codes": ["never_started"]}}
    elif not own_turn:
        reason = "turn_generation_mismatch"
    elif turn.get("state") not in _SUCCESS:
        reason = "turn_failed" if turn.get("state") == "failed" else (
            turn["reason"] if turn.get("reason") in _TURN_REASONS else "interrupted")
    if reason:
        return {"kind": "unresolved", "reason": reason,
                "presentation": {"stripe": "none" if reason == "source_unknown" else "attention", "reason_codes": [reason]}}
    return {"kind": "done", "presentation": {"stripe": "done", "reason_codes": ["work_settled"]}}


def reduce_status(snapshot: dict[str, Any], *, previous: dict | None = None) -> dict[str, Any]:
    """Reduce one qualified snapshot. Equal evidence is idempotent; revisions never regress.

    A previous value is the CAS base supplied by the persistence adapter, not a result
    another collector happened to publish while this collector was running.
    """
    identity = _identity(snapshot.get("identity"))
    generation = _generation(snapshot.get("generation"))
    prior = previous if previous and previous.get("status_version") == STATUS_VERSION else {}
    if prior.get("identity") == identity and generation < prior.get("generation", 0):
        raise ValueError("stale generation")
    coverage, revisions = _coverage(snapshot, identity, generation)
    failures = _failures(snapshot, prior, generation, coverage)
    value = {"status_version": STATUS_VERSION, "identity": identity, "generation": generation,
             "source_revisions": revisions, "coverage": coverage, "failures": failures,
             **_verdict(snapshot, coverage, generation, failures)}
    overdue = [item for item in snapshot.get('work') or ()
               if item.get('kind') == 'card' and item.get('state') == 'triage_overdue']
    # Operational attention never beats a user mute or a human ask (both decided in _verdict).
    if overdue and not snapshot.get('asks') and value.get('kind') != 'muted':
        value['presentation'] = {'stripe': 'stuck', 'reason_codes': list(dict.fromkeys([
            *value['presentation']['reason_codes'], 'triage_overdue']))}
        value.setdefault('on', deepcopy(overdue))
    old = {key: prior.get(key) for key in value}
    # An owner change is itself evidence even if the presentation is unchanged.
    revision = int(prior.get("revision", 0))
    value["revision"] = revision if value == old else revision + 1
    return value
