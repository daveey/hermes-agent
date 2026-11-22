"""Canonical session-status snapshot over the existing outcome readers, and its fenced publication. [daveey]

``refresh_outcome`` stores one envelope under ``outcome_json["status"]`` (``hermes_state_session_status``):
qualified identity, request generation, per-source coverage, a monotonic revision and
``presentation.stripe``. It is computed from work evidence only: no summary judge, no prose heuristic
and no declaration can select it; those stay explanation on the surrounding outcome.

Every required source is read strictly here. A reader that raises is *unknown coverage* for this
snapshot (never an empty list), which the reducer turns into a non-green ``source_unknown`` revision;
the next successful refresh recovers. Publication compares the stored value and the newest user row
it was collected against, so a stale collector retries instead of overwriting its successor.

Known limits (stated, not hidden): a revision certifies one pass over these readers, not cross-source
atomicity; a process no longer inside the registry's retention window is not counted (not a claim it
succeeded); a turn that was never observed with ownership tokens keeps the transcript reading.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Optional

from hermes_state_session_status import _OPEN, STATUS_VERSION, reduce_status

logger = logging.getLogger(__name__)

CAS_ATTEMPTS = 3
_GENERATION_SCAN_ROWS = 200
_SINGULAR = {"cards": "card", "controls": "control", "subagents": "subagent", "processes": "process",
             "goals": "goal"}


def _so():
    import hermes_state_outcome

    return hermes_state_outcome


def _digest(items: Any) -> str:
    text = json.dumps(items, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _identity(db: Any, tip: str, ids: list[str]) -> dict[str, str]:
    home = Path(db.db_path).parent
    gateway = ""
    with contextlib.suppress(ImportError, OSError, ValueError):
        from hermes_cli.kanban_origin import gateway_names

        gateway = gateway_names()[0] or ""
    marks = ",".join("?" * len(ids))
    root = db._read_one(f"SELECT id FROM sessions WHERE id IN ({marks}) ORDER BY started_at ASC, id ASC LIMIT 1",
                        tuple(ids))
    return {"gateway": gateway or "local",
            "profile": home.name if home.parent.name == "profiles" else "default",
            "session_id": tip, "owner_epoch": f"lineage:{root['id'] if root is not None else tip}"}


def _generation(db: Any, tip: str) -> tuple[int, float]:
    """(id, timestamp) of the newest genuine user request. A provenance-validated informational notice or
    completion note is not a request; any other user row (typed, steered, folded or unproven) is."""
    so = _so()
    rows = db._read_all(
        "SELECT id, role, content, display_kind, display_metadata, timestamp FROM messages"
        " WHERE session_id = ? AND active = 1 AND role = 'user' ORDER BY id DESC LIMIT ?",
        (tip, _GENERATION_SCAN_ROWS))
    for row in map(dict, rows):
        if so._completion_note(row) is None:
            return int(row["id"]), float(row.get("timestamp") or 0)
    return 0, 0.0


def _user_fence(db: Any, tip: str) -> int:
    row = db._read_one("SELECT COALESCE(MAX(id), 0) AS top FROM messages WHERE session_id = ? AND role = 'user'",
                       (tip,))
    return int(row["top"]) if row is not None else 0


def _live_tokens(db: Any, tip: str) -> set[str]:
    """``_live_turn_tokens`` without its swallowed fallbacks: an unreadable lease or routing store raises."""
    from tui_gateway.turn_marker import UPDATE_HOLD_QUEUE_REASON, marker_writer_state, read_turn_marker

    so = _so()
    tokens = set()
    marker = read_turn_marker(Path(db.db_path).parent, tip)
    if marker and marker.get("reason") != UPDATE_HOLD_QUEUE_REASON and marker_writer_state(marker) == "alive":
        tokens.add(so.marker_identity(marker))
    owner = so._durable_turn_owner(db, tip)
    if owner is not None:
        tokens.add(f"lease:{owner['holder']}")
    tokens.update(so._gateway_turn_tokens(db, tip))
    return tokens


def _transcript_turn(tail: list[dict]) -> dict[str, str]:
    """Main's transcript reading of the owning turn, minus the summary judge and prose heuristic."""
    so = _so()
    if not tail:
        return {"state": "never_started"}
    lost = so._lost_request(tail)
    if lost:
        return {"state": "failed" if lost == "turn_failed" else "interrupted", "reason": lost}
    reason = so._tail_reason(tail)
    if reason:
        return {"state": "failed" if reason == "turn_failed" else "interrupted", "reason": reason}
    return {"state": "completed"}


def _turn(tail: list[dict], live: set[str], concluding: frozenset[str], trigger: str, evidence: dict,
          generation: int) -> tuple[dict, dict]:
    """The owning turn for this generation, and the token evidence to keep.

    A turn observed with ownership tokens settles only on a ``turn_end`` naming one of them; the newest
    admission decides (a new genuine request, or a live turn sharing no token, replaces the older one). A
    generation never observed with tokens keeps the transcript reading unchanged."""
    same = evidence.get("generation") == generation
    admitted = set(evidence.get("admitted") or ()) if same else set()
    settled = set(evidence.get("settled") or ()) if same else set()
    if live:
        admitted, settled = (admitted | live, settled) if admitted & live else (set(live), set())
        turn = {"state": "running"}
    else:
        if trigger == "turn_end" and concluding and (not admitted or admitted & concluding):
            admitted |= concluding
            settled |= concluding
        turn = ({"state": "unsettled", "reason": "turn_unsettled"} if admitted and not settled & admitted
                else _transcript_turn(tail))
    kept = {"generation": generation, "admitted": sorted(admitted), "settled": sorted(settled)} if admitted else {}
    return {**turn, "generation": generation}, kept


def _open_state(state: Any) -> str:
    return state if state in _OPEN else "waiting"


def _control_items(sid: str) -> tuple[list[dict], list[dict]]:
    """Registered providers, strictly: a raising provider is unknown controls, never 'no open ask'."""
    so = _so()
    decisions, waits = [], []
    for _name, fn in list(so._providers.items()):
        result = fn(sid)
        if not isinstance(result, dict):
            continue
        decisions += [i for i in map(so._decision_item, result.get("decision_items") or ()) if i]
        waits += [i for i in map(so._wait_item, result.get("waiting_on") or ()) if i]
    return decisions, waits


def _process_items(db: Any, tip: str) -> list[dict]:
    """Live and retained processes of this chat from the registry's strict qualified inventory."""
    from tools.process_registry import process_registry

    with _so()._owner_scope(db):
        rows = process_registry.list_sessions(include_retained=True, strict=True, owner_session_id=tip)
    items = []
    for row in rows:
        if row.get("status") != "exited":
            state = "running"
        else:
            state = "succeeded" if row.get("exit_code") == 0 else "failed"
        items.append({"kind": "process", "id": f"process:{row['session_id']}", "state": state,
                      "at": float(row.get("exited_at") or 0)})
    return items


def _goal_items(db: Any, ids: list[str]) -> list[dict]:
    """The exact owned ``goal:<sid>`` records; a malformed record raises (unknown), it is not 'no goal'."""
    items = []
    for sid in ids:
        row = db._read_one("SELECT value FROM state_meta WHERE key = ?", (f"goal:{sid}",))
        if row is None or row["value"] in (None, ""):
            continue
        data = json.loads(row["value"])
        if not isinstance(data, dict):
            raise ValueError("malformed goal record")
        if data.get("status") == "active" and str(data.get("goal") or "").strip():
            items.append({"kind": "goal", "id": f"goal:{sid}", "state": "running"})
    return items


def _subagent_items(db: Any, ids: list[str], since: float) -> tuple[list[dict], list[dict]]:
    """(running + lost during this generation, lost before it)."""
    so = _so()
    every = so._lost_subagent_items(db, ids, None)
    current = {item["id"] for item in (so._lost_subagent_items(db, ids, since) if since else every)}

    def tagged(items: list[dict]) -> list[dict]:
        return [{"id": f"subagent:{item['id']}", "state": item["state"]} for item in items]

    return (tagged(so._subagent_items(db, ids)) + tagged([i for i in every if i["id"] in current]),
            tagged([i for i in every if i["id"] not in current]))


def canonical_status(db: Any, tip: str, stored: Optional[dict], *, trigger: str,
                     concluding: frozenset[str] = frozenset()) -> dict[str, Any]:
    """Collect one qualified snapshot for ``tip`` and reduce it against the stored envelope."""
    so = _so()
    prior = stored.get("status") if isinstance(stored, dict) else None
    prior = prior if isinstance(prior, dict) and prior.get("status_version") == STATUS_VERSION else None
    ids = so._lineage_ids(db, tip)
    identity = _identity(db, tip, ids)
    same_owner = prior is not None and prior.get("identity") == identity
    generation, since = _generation(db, tip)
    if same_owner:  # a rewound transcript never regresses the generation it already published
        generation = max(generation, int(prior.get("generation") or 0))
    errors: dict[str, str] = {}

    def read(name: str, fn: Callable[[], Any], empty: Any) -> Any:
        try:
            return fn()
        except Exception as exc:  # health: allow BLE001 -- any reader failure is unknown coverage, never empty
            logger.warning("session status: %s unreadable for %s", name, tip, exc_info=True)
            errors[name] = f"{_SINGULAR.get(name, name)}_read_failed:{type(exc).__name__}"
            return empty

    row = so._row(db, tip) or {}
    tail = so._without_finished_notes(so._tail(db, tip))
    live = read("turn", lambda: _live_tokens(db, tip) - concluding, set())
    kept = (prior.get("turn_evidence") or {}) if same_owner else {}
    turn, evidence = _turn(tail, live, concluding, trigger, kept, generation)
    card_asks, card_waits = read("cards", lambda: so._card_items(ids), ([], []))
    control_asks, control_waits = read("controls", lambda: _control_items(tip), ([], []))
    subagents, lost_before = read("subagents", lambda: _subagent_items(db, ids, since), ([], []))
    processes = read("processes", lambda: _process_items(db, tip), [])
    goals = read("goals", lambda: _goal_items(db, ids), [])
    # Work that failed before this generation's request is retained failure, cleared only by this
    # generation's own successful completion; it is not this generation's linked work.
    earlier = [p for p in processes if p["state"] == "failed" and 0 < p["at"] < since]
    work = ([{"kind": "card", "id": f"card:{w['id']}", "state": _open_state(w.get("state"))} for w in card_waits]
            + [{"kind": w["kind"], "id": f"control:{w['id']}", "state": _open_state(w.get("state"))}
               for w in control_waits]
            + [{"kind": "subagent", "id": s["id"], "state": s["state"]} for s in subagents]
            + [{"kind": "process", "id": p["id"], "state": p["state"]} for p in processes if p not in earlier]
            + goals)
    asks = control_asks + card_asks
    readings = {"cards": [card_asks, card_waits], "controls": [control_asks, control_waits],
                "turn": [turn, sorted(live)], "subagents": [subagents, lost_before],
                "processes": processes, "goals": goals}
    sources = {}
    for name, items in readings.items():
        revision = _digest(items)
        sources[name] = {"identity": identity, "generation": generation, "revision": revision,
                         "current_revision": revision,
                         **({"coverage": "unknown", "reason": errors[name]} if name in errors
                            else {"coverage": "complete"})}
    base = deepcopy(prior) if prior is not None else {"status_version": STATUS_VERSION}
    known = {failure.get("id") for failure in base.get("failures") or ()}
    base["failures"] = list(base.get("failures") or ()) + [
        {"id": item["id"], "generation": max(0, generation - 1), "state": item["state"]}
        for item in [*earlier, *lost_before] if item["id"] not in known]
    status = reduce_status({"identity": identity, "generation": generation, "started": bool(tail), "turn": turn,
                            "sources": sources, "work": work, "asks": asks, "muted": bool(row.get("muted"))},
                           previous=base)
    # The revision is counted against what was actually published, not the failure-seeded reducer base.
    semantic = {key: value for key, value in status.items() if key != "revision"}
    unchanged = prior is not None and all(prior.get(key) == value for key, value in semantic.items())
    status["revision"] = int((prior or {}).get("revision") or 0) + (0 if unchanged else 1)
    if evidence:
        status["turn_evidence"] = evidence
    return status


def _publish(db: Any, tip: str, raw: Any, fence: int, outcome: dict) -> bool:
    """Write only if the stored outcome and the newest user row are still the ones collected against."""
    payload = json.dumps(outcome, ensure_ascii=False, separators=(",", ":"))
    return db._write_rowcount(
        "UPDATE sessions SET outcome_json = ?, outcome_at = ? WHERE id = ? AND outcome_json IS ?"
        " AND (SELECT COALESCE(MAX(id), 0) FROM messages WHERE session_id = ? AND role = 'user') = ?",
        (payload, time.time(), tip, raw, tip, fence)) > 0


def refresh(db: Any, sid: str, trigger: str, concluding: frozenset[str]) -> dict[str, Any]:
    """``refresh_outcome``'s body: derive the explanation, reduce the canonical status, publish fenced."""
    so = _so()
    tip = so._tip(db, sid)
    for _attempt in range(CAS_ATTEMPTS):
        tip = so._tip(db, sid)
        row = so._row(db, tip)
        derived = so._derive(db, tip, concluding=concluding)
        if row is None:
            return derived
        raw, fence = row.get("outcome_json"), _user_fence(db, tip)
        stored = so._parse(raw)
        derived = {**derived, "status": canonical_status(db, tip, stored, trigger=trigger, concluding=concluding)}
        if so._same(stored, derived):
            # Unchanged is only an answer while it is still what is stored for the same newest request.
            if (so._row(db, tip) or {}).get("outcome_json") == raw and _user_fence(db, tip) == fence:
                return stored
            continue
        if _publish(db, tip, raw, fence, derived):
            logger.debug("session outcome %s -> %s/%s (%s)", tip, derived.get("kind"),
                         derived["status"].get("kind"), trigger)
            return derived
    # Every attempt met a successor's write or a newer request: theirs stands, and their own refresh follows.
    logger.warning("session outcome refresh (%s) for %s lost %d publication races; successor kept",
                   trigger, tip, CAS_ATTEMPTS)
    return so._parse((so._row(db, tip) or {}).get("outcome_json")) or derived
