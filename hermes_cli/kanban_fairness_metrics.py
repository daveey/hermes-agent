"""Ready-wait cohorts and acceptance gates from one queue-fairness capture. [daveey]

Pure: records in, verdict out. An *episode* is one stay of one card in ``ready``. Every time in it is an
interval, because the capture sees a change when the database applies it, not when it happened:

* the start is the Board's ``ready_since`` when the row carries a believable one, else the sighting that
  showed it ready;
* a claim ends it at the origin's ``claimed`` event second when one was journaled, else at the sighting of the
  ``running`` row; a card first sighted ``running`` with a new ``ready_since`` was ready and claimed between
  two sightings, and still counts;
* a card that leaves ``ready`` any other way (blocked, archived, parked) has only a lower bound, unless the
  capture was armed with ``unclaimed_exit = "elapsed_ready"`` (it then waited exactly as long as it sat
  there); one still ready at the close always has only a lower bound.

``clock_error_s`` widens every wait by the assumed disagreement between the clocks involved, and
``apply_lag_s`` is how long a change may take to reach the captured database (a replica's pull lag; 0 for a
board's own file): a sighting at ``t`` proves the change happened in ``t - lag .. t``. A gate passes
only on its upper bound, fails only on its lower bound, and is INDETERMINATE in between, with an empty
cohort, or when the capture has a coverage gap. Nothing is excluded from a cohort to make a gate pass.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, NamedTuple, Optional

from hermes_cli.kanban_fairness_schema import (
    FAIL,
    INDETERMINATE,
    MAX_WAIT_S,
    OWNER_SOURCES,
    P0_MEDIAN_S,
    PASS,
    URGENT_WAIT_S,
    VERSION,
)


@dataclass
class Episode:
    task_id: str
    priority: int
    key: Optional[str]
    start_lo: float
    start_hi: float
    ready_since: Optional[float]
    last_seen: float
    opening: bool = False
    end: str = "open"  # open | claimed | left:<status> | requeued
    end_lo: float = 0.0  # meaningful once ``end`` is not "open"
    end_hi: float = 0.0
    priorities: list = field(default_factory=list)

    def wait(self, clock: Clock) -> tuple[float, Optional[float]]:
        """``(lower, upper)`` seconds in ready; upper is None while the card is still ready at the close."""
        if self.end == "open":
            return max(0.0, clock.close_s - clock.lag - self.start_hi - clock.err), None
        lo = max(0.0, self.end_lo - self.start_hi - clock.err)
        if self.end != "claimed" and not clock.unclaimed_elapsed:
            return lo, None
        return lo, max(0.0, self.end_hi - self.start_lo + clock.err)

    def elapsed_hi(self, clock: Clock) -> float:
        """The longest this episode can have been ready inside the window."""
        return max(0.0, (clock.close_s if self.end == "open" else self.end_hi) - self.start_lo + clock.err)

    def changed_priority(self) -> bool:
        return len(set(self.priorities)) > 1


class Clock(NamedTuple):
    close_s: float
    err: float
    lag: float
    unclaimed_elapsed: bool = False


def episodes(records: Iterable[dict], *, armed_s: float, err: float, lag: float = 0.0) -> list[Episode]:
    """Every ready episode the records show, oldest first; ``claimed`` events tighten claim times."""
    seen: dict[str, tuple[str, float]] = {}  # task -> (status, wall of the latest sighting)
    counted: dict[str, Optional[float]] = {}  # task -> ready_since of its latest episode
    live: dict[str, Episode] = {}
    done: list[Episode] = []
    claims: dict[str, list[int]] = {}
    for rec in records:
        if "payload" in rec:
            if rec["kind"] not in ("opening", "row_change"):
                continue
            p = rec["payload"]
            rec = {"task_id": p["task_id"], "wall_ms": rec["wall_ns"] / 1_000_000,
                   "kind": p["journal_kind"], "op": p["op"], "fields": p["fields"]}
        tid, wall, fields = rec["task_id"], rec["wall_ms"] / 1000, rec["fields"]
        if rec["kind"] == "event":
            if fields["kind"] == "claimed":
                claims.setdefault(tid, []).append(fields["created_at"])
            continue
        status = "deleted" if rec["op"] == "delete" else fields["status"]
        ep = live.get(tid)
        since = fields.get("ready_since")
        since = None if since is None else since / 1000
        if status != "ready":
            if ep is not None:
                ep.end = "claimed" if status == "running" else f"left:{status}"
                ep.end_lo, ep.end_hi = max(ep.start_lo, wall - lag), wall
                done.append(live.pop(tid))
            elif (status == "running" and rec["kind"] != "opening" and since is not None and since <= wall + err
                  and since != counted.get(tid, since if tid in seen else None)):
                # Ready and claimed between two sightings (a replica's pull): the row still says since when.
                done.append(Episode(tid, fields.get("priority") or 0, fields.get("idempotency_key"), since,
                                    since, since, wall, end="claimed", end_lo=max(since, wall - lag),
                                    end_hi=wall, priorities=[fields.get("priority") or 0]))
            counted[tid] = since
            seen[tid] = (status, wall)
            continue
        if ep is not None and since is not None and ep.ready_since is not None and since > ep.ready_since:
            # The card left ready and came back between two sightings: the first stay ended unseen.
            ep.end, ep.end_lo, ep.end_hi = "requeued", max(ep.start_lo, ep.last_seen - lag), since
            done.append(live.pop(tid))
            ep = None
        if ep is None:
            before = seen.get(tid)
            floor = wall - lag
            # A ready_since older than a sighting of the card out of ready belongs to an earlier stay.
            stale = since is None or since > wall + err or (
                before is not None and since < before[1] - lag - err)
            if stale and rec["kind"] == "opening":  # ready before the window, since when is not on the row
                floor = float(fields.get("created_at") or 0)
            start = (floor, wall) if stale or since is None else (since, since)
            ep = live[tid] = Episode(tid, fields.get("priority") or 0, fields.get("idempotency_key"), *start,
                                     None if stale else since, wall, opening=rec["kind"] == "opening")
        ep.last_seen = wall
        counted[tid] = ep.ready_since
        ep.priorities.append(fields.get("priority") or 0)
        seen[tid] = (status, wall)
    out = done + list(live.values())
    for ep in out:
        if ep.end != "claimed":
            continue
        stamps = [c for c in claims.get(ep.task_id, ()) if ep.start_lo - err - 1 <= c <= ep.end_hi + err]
        if stamps:
            ep.end_lo = max(ep.start_lo, float(min(stamps)))
            ep.end_hi = max(ep.end_lo, min(ep.end_hi, min(stamps) + 1.0))
    return sorted(out, key=lambda e: (e.start_lo, e.task_id))


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def _counts(cohort: list[Episode]) -> dict:
    out = {"episodes": len(cohort), "opening_backlog": sum(e.opening for e in cohort), "claimed": 0, "open": 0,
           "left_unclaimed": 0, "requeued": 0, "priority_changed": sum(e.changed_priority() for e in cohort)}
    for ep in cohort:
        name = ep.end if ep.end in ("claimed", "open", "requeued") else "left_unclaimed"
        out[name] += 1
    return out


def _median_gate(cohort: list[Episode], clock: Clock) -> dict:
    out = {"threshold_s": P0_MEDIAN_S, **_counts(cohort), "median_lo_s": None, "median_hi_s": None}
    if not cohort:
        return {**out, "status": INDETERMINATE, "reason": "no stored-priority-0 ready episode in the window"}
    waits = [ep.wait(clock) for ep in cohort]
    lo = _median([w[0] for w in waits])
    hi = _median([float("inf") if w[1] is None else w[1] for w in waits])
    out.update(median_lo_s=round(lo, 3), median_hi_s=None if hi == float("inf") else round(hi, 3))
    if hi <= P0_MEDIAN_S:
        return {**out, "status": PASS}
    if lo > P0_MEDIAN_S:
        return {**out, "status": FAIL}
    return {**out, "status": INDETERMINATE, "reason": "the median's bounds straddle the threshold"}


def _max_gate(eps: list[Episode], clock: Clock) -> dict:
    """No card waits past ``MAX_WAIT_S`` while claims happen: an overdue card is a breach only when some
    claim was witnessed during its overtime (its own late claim counts)."""
    claim_times = sorted(ep.end_hi for ep in eps if ep.end == "claimed")
    out = {"threshold_s": MAX_WAIT_S, "episodes": len(eps), "claims_witnessed": len(claim_times),
           "raw_max_lo_s": None, "raw_max_hi_s": None, "breaches": [], "possible": []}
    if eps:
        out["raw_max_lo_s"] = round(max(ep.wait(clock)[0] for ep in eps), 3)
        out["raw_max_hi_s"] = round(max(ep.elapsed_hi(clock) for ep in eps), 3)
    for ep in eps:
        lo = ep.wait(clock)[0]
        until = clock.close_s if ep.end == "open" else ep.end_hi
        if lo > MAX_WAIT_S and any(ep.start_hi + MAX_WAIT_S + clock.err < t <= until for t in claim_times):
            out["breaches"].append({"task_id": ep.task_id, "wait_lo_s": round(lo, 3), "end": ep.end})
        elif ep.elapsed_hi(clock) > MAX_WAIT_S and any(
                ep.start_lo + MAX_WAIT_S - clock.err < t <= until for t in claim_times):
            out["possible"].append({"task_id": ep.task_id, "end": ep.end,
                                    "wait_hi_s": round(ep.elapsed_hi(clock), 3)})
    if out["breaches"]:
        return {**out, "status": FAIL}
    if not claim_times:
        return {**out, "status": INDETERMINATE, "reason": "no claim was witnessed in the window"}
    if out["possible"]:
        return {**out, "status": INDETERMINATE, "reason": "an episode's bounds straddle the threshold"}
    return {**out, "status": PASS}


def _urgent_gate(cohort: list[Episode], clock: Clock, empty: str) -> dict:
    out = {"threshold_s": URGENT_WAIT_S, **_counts(cohort), "worst_lo_s": None, "worst_hi_s": None, "late": [],
           "undecided": []}
    if not cohort:
        return {**out, "status": INDETERMINATE, "reason": empty}
    out["worst_lo_s"] = round(max(ep.wait(clock)[0] for ep in cohort), 3)
    out["worst_hi_s"] = round(max(ep.elapsed_hi(clock) for ep in cohort), 3)
    for ep in cohort:
        lo, hi = ep.wait(clock)
        row = {"task_id": ep.task_id, "end": ep.end, "wait_lo_s": round(lo, 3),
               "wait_hi_s": None if hi is None else round(hi, 3)}
        if lo > URGENT_WAIT_S:
            out["late"].append(row)
        elif hi is None or hi > URGENT_WAIT_S:
            out["undecided"].append(row)
    if out["late"]:
        return {**out, "status": FAIL}
    if out["undecided"]:
        return {**out, "status": INDETERMINATE,
                "reason": "an episode has no upper bound (still ready, or left unclaimed) or straddles the threshold"}
    return {**out, "status": PASS}


def _owner_coverage(owners: Optional[list[dict]], armed_s: float, close_s: float) -> Optional[str]:
    """Why the owner journal cannot vouch for the whole window, or None when it does."""
    if owners is None:
        return "no owner_authority.jsonl: land-turn owner history was not journaled for this window"
    for source in OWNER_SOURCES:
        spans = sorted((o["from_s"], o["to_s"], o["complete"]) for o in owners
                       if o["kind"] == "coverage" and o["source"] == source
                       and o["to_s"] > armed_s and o["from_s"] < close_s)
        reached = armed_s
        for start, end, complete in spans:
            if not complete:
                return f"{source} owner history has a recorded break inside the window"
            if start > reached:
                break
            reached = max(reached, end)
        if reached < close_s:
            return f"{source} owner history does not cover the whole window"
    return None


def _owner_episodes(eps: list[Episode], owners: Optional[list[dict]], close_s: float) -> list[Episode]:
    held: dict[str, list[tuple[float, float]]] = {}
    for o in owners or ():
        if o["kind"] == "interval":
            held.setdefault(o["task_id"], []).append(
                (o["start_s"], float("inf") if o["end_s"] is None else o["end_s"]))
    out = []
    for ep in eps:
        until = close_s if ep.end == "open" else ep.end_hi
        if any(start < until and end > ep.start_lo for start, end in held.get(ep.task_id, ())):
            out.append(ep)
    return out


def verdict(capture: dict, *, alarm_prefixes: tuple, min_window_s: float = 0) -> dict:
    """The live report for a loaded capture (:func:`kanban_fairness_store.load`)."""
    manifest = capture["manifest"]
    gaps = list(capture["gaps"])
    window = manifest.get("window") or {}
    # The metric kernel uses a local row view; persisted input remains the strict envelope.
    records = [
        {"seq": rec["payload"]["journal_seq"], "kind": rec["payload"]["journal_kind"],
         "wall_ms": rec["wall_ns"] / 1_000_000, "task_id": rec["payload"]["task_id"],
         "op": rec["payload"]["op"], "fields": rec["payload"]["fields"]}
        if "payload" in rec else rec
        for rec in capture["records"] if "payload" not in rec or rec["kind"] in ("opening", "row_change")
    ]
    armed_ms = window.get("armed_ms") or (records[0]["wall_ms"] if records else 0)
    close_ms = window.get("sealed_ms") or (records[-1]["wall_ms"] if records else armed_ms)
    armed_s, close_s = armed_ms / 1000, close_ms / 1000
    assumed = manifest.get("assumptions") or {}
    err, lag = float(assumed.get("clock_error_s", 2.0)), float(assumed.get("apply_lag_s", 0.0))
    # Never narrow writer-attested uncertainty to a smaller report assumption.
    err = max([err, *(rec["clock_error_ns"] / 1_000_000_000 for rec in capture["records"]
                     if rec.get("clock_error_ns") is not None)])
    unclaimed = assumed.get("unclaimed_exit", "unknown")
    clock = Clock(close_s, err, lag, unclaimed == "elapsed_ready")
    if close_s - armed_s < min_window_s:
        gaps.append({"code": "window_short",
                     "detail": f"window is {close_s - armed_s:.0f} s, the report needs {min_window_s:.0f} s"})
    eps = episodes(records, armed_s=armed_s, err=err, lag=lag)
    p0 = [ep for ep in eps if ep.priority == 0]
    alarms = [ep for ep in eps if isinstance(ep.key, str) and ep.key.startswith(tuple(alarm_prefixes))]
    owner_gap = _owner_coverage(capture["owners"], armed_s, close_s)
    gates = {
        "p0_median_wait": _median_gate(p0, clock),
        "max_wait_while_claims": _max_gate(eps, clock),
        "alarm_wait": _urgent_gate(alarms, clock, "no alarm card was ready in the window"),
        "owner_wait": _urgent_gate(_owner_episodes(eps, capture["owners"], close_s), clock,
                                   "no land-turn owner card was ready in the window"),
    }
    if owner_gap and gates["owner_wait"]["status"] != FAIL:
        gates["owner_wait"].update(status=INDETERMINATE, reason=owner_gap)
    if gaps:  # a proven failure stands on incomplete evidence; a pass does not
        for gate in gates.values():
            if gate["status"] == PASS:
                gate.update(status=INDETERMINATE, reason="capture coverage is incomplete")
    states = {gate["status"] for gate in gates.values()}
    overall = FAIL if FAIL in states else PASS if states == {PASS} else INDETERMINATE
    return {
        "version": VERSION, "mode": "live", "capture_id": manifest.get("capture_id"),
        "board": manifest.get("board"),
        "window": {"armed_ms": armed_ms, "close_ms": close_ms, "seconds": round(close_s - armed_s, 3),
                   "min_seconds": min_window_s},
        "assumptions": {"clock_error_s": err, "apply_lag_s": lag, "unclaimed_exit": unclaimed,
                        "alarm_prefixes": list(alarm_prefixes)},
        "coverage": {"complete": not gaps, "gaps": gaps},
        "cohorts": {"all": _counts(eps), "p0": _counts(p0), "alarm": _counts(alarms)},
        "gates": gates, "overall": overall, "policy": manifest.get("policy") or {},
    }
