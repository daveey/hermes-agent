"""Fail-closed, replica-order-independent non-human containment state.

This reducer does not authorize a release. Its caller must authenticate each
release before providing it; generic lifecycle events never satisfy a hold.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from hermes_cli.kanban_containment_policy import ContainmentPolicyError, identity, policy


@dataclass(frozen=True)
class Hold:
    """Unreleased episodes, or malformed history requiring scoped repair."""

    episodes: tuple[str, ...] = ()
    unresolved: bool = False


def _uid(value: Any) -> bool:
    try:
        parsed = uuid.UUID(value) if isinstance(value, str) else None
    except ValueError:
        return False
    return parsed is not None and parsed.version == 7 and str(parsed) == value


def _payload(event: Mapping[str, Any]) -> dict | None:
    raw = event.get("payload")
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return None
    if not isinstance(value, dict):
        return None
    # Append-owned transport evidence is not part of the frozen predicate. It
    # must remain an object; unknown application keys still fail strict parsing.
    value = dict(value)
    for key in ("_recovery_history_v1", "_wake_episode_v1"):
        if key in value:
            if not isinstance(value.pop(key), dict):
                return None
    return value


def _stop(event: Mapping[str, Any]) -> dict | None:
    data = _payload(event)
    if not _uid(event.get("uid")) or data is None or data.keys() != {
        "version", "target", "policy", "producer_baseline",
    }:
        return None
    if type(data["version"]) is not int or data["version"] != 1:
        return None
    try:
        target = identity(data["target"])
        spec = policy(data["policy"], target=target)
    except ContainmentPolicyError:
        return None
    if target["task_id"] != event.get("task_id"):
        return None
    baseline = data["producer_baseline"]
    if baseline is not None and ("producer" not in spec or not isinstance(baseline, dict)
                                or baseline.keys() != {"event_uid", "run_uid"}
                                or not _uid(baseline["run_uid"])
                                or (baseline["event_uid"] is not None and not _uid(baseline["event_uid"]))):
        return None
    return data


def fingerprint(payload: dict) -> str:
    """Bind a release to the exact frozen predicate, not a mutable task row."""
    data = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _evidence(data: Any, stop: dict) -> bool:
    if not isinstance(data, dict) or data.get("kind") not in ("authority", "producer"):
        return False
    kind = data["kind"]
    keys = {"kind", kind, "run_uid"} | ({"event_uid"} if kind == "producer" else set())
    if data.keys() != keys or not _uid(data["run_uid"]):
        return False
    try:
        actor = identity(data[kind])
    except ContainmentPolicyError:
        return False
    expected = stop["policy"].get(kind)
    if expected is None or any(actor[k] != expected[k] for k in ("board", "task_id")):
        return False
    if kind == "producer":
        baseline = stop["producer_baseline"]
        if not _uid(data["event_uid"]) or (baseline is not None and (
            data["event_uid"] == baseline["event_uid"] or data["run_uid"] == baseline["run_uid"]
        )):
            return False
    return True


def _release(event: Mapping[str, Any], stop: Mapping[str, Any]) -> bool:
    data = _payload(event)
    if not _uid(event.get("uid")) or event.get("task_id") != stop.get("task_id") or data is None:
        return False
    if data.keys() != {"version", "episode", "containment_sha256", "resume_status", "evidence"}:
        return False
    if type(data["version"]) is not int or data["version"] != 1 or data["episode"] != stop["uid"]:
        return False
    payload = _stop(stop)
    return (payload is not None and data["containment_sha256"] == fingerprint(payload)
            and data["resume_status"] == "ready" and _evidence(data["evidence"], payload))


def reduce_events(
    events: Iterable[Mapping[str, Any]], *,
    verify_release: Callable[[Mapping[str, Any], Mapping[str, Any]], bool] | None = None,
) -> Hold | None:
    """Reduce an episode set, never latest-event-wins.

    ``verify_release`` must verify authenticated canonical provenance and the
    episode's authority/new-producer proof, not just the receipt's shape. No
    verifier (including an offline replica) means no releases. A raised verifier
    error propagates so callers cannot mistake an unknown result for permission.
    """
    events = list(events)
    stops: dict[str, Mapping[str, Any]] = {}
    releases = []
    unresolved = False
    for event in events:
        if event.get("kind") == "containment_released":
            releases.append(event)
        if event.get("kind") != "contained":
            continue
        data = _stop(event)
        if data is None:
            unresolved = True
            continue
        uid = event["uid"]
        if uid in stops and _stop(stops[uid]) != data:
            unresolved = True
        stops[uid] = event
    active = set(stops)
    if verify_release is not None:
        from hermes_cli.kanban_containment_terminal import closure
        closed = {uid for uid, stop in stops.items() if any(
            event.get("kind") == "containment_closed" and closure(event, stop)
            and verify_release(event, stop) is True for event in events)}
        for receipt in releases:
            data = _payload(receipt)
            episode = data.get("episode") if data else None
            if not isinstance(episode, str) or episode not in stops:
                continue
            stop = stops[episode]
            if episode not in closed and _release(receipt, stop) and verify_release(receipt, stop) is True:
                active.discard(episode)
    return Hold(tuple(sorted(active)), unresolved) if active or unresolved else None
