"""Pure bounded-window receipt metrics. Never used to admit, retry or declare progress.

Event order is producer order, not sorted wall time: sorting clocks would conceal
skew. ``spawned`` is a submitter, not the owner-bound executing interpreter.
Callers must pass a complete ordered task history (or report truncated coverage).
"""
from __future__ import annotations

import json
from itertools import pairwise
import math
import statistics

from hermes_cli.execution_observations import epoch

METRICS = ("ready_to_claim_s", "claim_to_process_start_s", "ready_to_process_start_s")


def _payload(event):
    raw = event.get("payload")
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw) if isinstance(raw, str) else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _ready(event, payload):
    # Creation is not readiness. Only explicit ready transitions qualify.
    if event.get("kind") in {"promoted", "promoted_manual", "unblocked", "triage_swept"}:
        return payload.get("status", "ready") == "ready"
    if event.get("kind") in {"released", "reclaimed", "owner_unavailable", "crashed", "rate_limited",
                              "timed_out", "stale", "reconciled", "owner_restart_lost",
                              "protocol_violation", "oom_killed", "signaled"}:
        return payload.get("retry_status") == "ready"
    return event.get("kind") in {"continued", "unblocked", "status", "continuation_woken"} and payload.get("status") == "ready"


def _delta(start, end):
    return end - start if epoch(start) and epoch(end) and end >= start else None


def _attempt(board, task_id, ready, claim=None):
    return {"board": board, "task_id": task_id,
            "run_id": claim.get("run_id") if claim else None,
            "ready_at": ready.get("created_at") if ready else None,
            "claim_at": claim.get("created_at") if claim else None,
            "ready_ref": ready.get("id") if ready else None,
            "claim_ref": claim.get("id") if claim else None,
            "claim_lock": _payload(claim).get("lock") if claim else None,
            "process_start_at": None, "process_ref": None, "process_pid": None,
            "last_progress": None}


def attempts(events, *, board, task_id, now):
    """One record per observed claim, plus a right-censored unclaimed ready interval.

    Binding must match the actual run and claim, with process birth evidence.
    Timestamp is the durable binding receipt time, NOT the OS fingerprint (which
    is not an epoch). This measures time-to-process-start-receipt, an upper bound
    on actual launch latency. Missing/skewed receipts produce null metrics.
    """
    rows, ready, current = [], None, None
    for event in events:
        payload, kind = _payload(event), event.get("kind")
        if _ready(event, payload):
            ready, current = event, None
        elif kind == "claimed" and event.get("run_id") is not None:
            # A Board lease event followed by local claim may share the run.
            # Only the local lock-bearing claim binds the interpreter receipt.
            if not payload.get("lock"):
                continue
            if current and current["run_id"] == event["run_id"]:
                continue
            current = _attempt(board, task_id, ready, event)
            rows.append(current)
            ready = None
        elif kind == "worker_bound" and current:
            if (event.get("run_id") == current["run_id"] and current["claim_lock"]
                    and payload.get("claim_lock") == current["claim_lock"]
                    and type(payload.get("pid")) is int and payload["pid"] > 0
                    and payload.get("started_at") not in (None, "", "unverified")
                    and current["process_ref"] is None):
                current.update(process_start_at=event.get("created_at"), process_ref=event.get("id"),
                               process_pid=payload["pid"])
        elif kind in {"dependency_wait", "blocked", "scheduled", "completed", "archived", "changes_requested",
                      "status", "auto_blocked", "gave_up", "block_loop_detected", "agent_block_escalated",
                      "supervisor_escalated", "continuation_exhausted", "unblocked", "imported"}:
            ready, current = None, None
    if ready is not None:
        rows.append(_attempt(board, task_id, ready))
    for row in rows:
        r, c, s = row["ready_at"], row["claim_at"], row["process_start_at"]
        unknown = []
        for label, value in (("ready", r), ("claim", c), ("process_start", s)):
            if not epoch(value):
                unknown.append("missing_" + label + "_receipt")
        skew = (not epoch(now) or any(epoch(v) and v > now for v in (r, c, s))
                or (epoch(r) and epoch(c) and c < r) or (epoch(c) and epoch(s) and s < c))
        if skew:
            unknown.append("clock_inconsistent")
        values = (_delta(r, c), _delta(c, s), _delta(r, s) if epoch(c) else None)
        row.update(zip(METRICS, (None, None, None) if skew else values))
        row.update(unknown=unknown, censored=s is None,
                   process_start_semantics="owner_bound_receipt_not_progress")
    return rows


def summarize(rows):
    """Retain raw samples; p50 needs two, p95 twenty (nearest-rank) samples."""
    out = {"attempt_count": len(rows), "censored_count": sum(bool(r["censored"]) for r in rows),
           "receipt_resolution_seconds": 1, "latency_semantics": "durable_receipt_delta_not_exact_launch"}
    for key in METRICS:
        values = sorted(r[key] for r in rows if r[key] is not None)
        out[key] = {"samples": values, "sample_count": len(values),
                    "p50": statistics.median(values) if len(values) >= 2 else None,
                    "p95": values[math.ceil(.95 * len(values)) - 1] if len(values) >= 20 else None}
    return out


def _valid_interval(row, left, right):
    at, until = row.get("observed_at"), row.get("valid_until")
    return (epoch(at) and epoch(until) and at <= left and until >= right and until <= at + 180
            and row.get("coverage_complete") is True
            and type(row.get("eligible_backlog")) is int and row["eligible_backlog"] >= 0
            and type(row.get("admissible_slots")) is int and row["admissible_slots"] >= 0
            and isinstance(row.get("restrictions"), list))


def idle_coverage(intervals):
    """Integrate aligned complete observations, unioning shared host slots.

    ``slot_scope`` identifies the actual shared slot pool, NOT the candidate or
    board. ``eligible_backlog`` is the deduplicated count for that whole pool,
    not a per-board count; disagreeing observations are unknown, never summed.
    Incomplete/conflicting evidence contributes unknown machine-seconds,
    never fabricated unknown slot-seconds. Each input is an explicitly observed
    interval; point snapshots must not be extended to fill observation gaps.
    """
    out = {"unexplained_idle_slot_seconds": 0, "unknown_seconds": 0,
           "explained_seconds": 0, "observed_seconds": 0, "invalid_intervals": 0}
    groups = {}
    for row in intervals:
        start, end = row.get("start"), row.get("end")
        if (not epoch(start) or not epoch(end) or end <= start
                or not row.get("machine") or not row.get("slot_scope")):
            out["invalid_intervals"] += 1
            continue
        groups.setdefault((row["machine"], row["slot_scope"]), []).append(row)
    for rows in groups.values():
        points = sorted({r[k] for r in rows for k in ("start", "end")})
        for left, right in pairwise(points):
            active = [r for r in rows if r["start"] <= left and r["end"] >= right]
            if not active:
                # The bounding observation window contains a telemetry gap.
                out["unknown_seconds"] += right - left
                continue
            width = right - left
            out["observed_seconds"] += width
            if (not all(_valid_interval(r, left, right) for r in active)
                    or len({r.get("admissible_slots") for r in active}) != 1
                    or len({r.get("eligible_backlog") for r in active}) != 1):
                out["unknown_seconds"] += width
            elif any(r["restrictions"] for r in active) or not any(r["eligible_backlog"] for r in active):
                out["explained_seconds"] += width
            else:
                out["unexplained_idle_slot_seconds"] += width * min(
                    active[0]["eligible_backlog"], active[0]["admissible_slots"])
    return out
