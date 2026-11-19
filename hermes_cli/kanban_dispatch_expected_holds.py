"""Diagnostic-only classification of intentional pacing and host-pressure waits. [daveey]"""
from __future__ import annotations

import dataclasses
import logging
from datetime import UTC, datetime

from hermes_cli.kanban_db_dispatch_clock import CLOCK_REASONS
from hermes_cli.kanban_db_dispatch_stuck import QUIET_COMPATIBLE_FIELDS

# Positive allowlist: a new outcome must not silently mask a failure. Quiet is handled separately.
_COMPATIBLE = (QUIET_COMPATIBLE_FIELDS - {"quiet_deferred", "quiet_deadlines"}) | {
    "llm_paced", "clock_held", "respawn_guarded", "claim_deferred", "skipped_ineligible"}


def _pacing_hold(row, now):
    """Validate a typed pacing skip; never mistake its clock_held refresh for a release."""
    if not isinstance(row, (tuple, list)) or len(row) != 4 or not isinstance(row[3], dict):
        return None
    tid, _, until, extra = row
    providers = extra.get("providers")
    if not isinstance(providers, list) or not providers or not all(isinstance(v, dict) for v in providers):
        return None
    # Only a pool at reserve holds a start (the governor's ``reserve_hold``): its wait is expected
    # while it names a real future reset. A hold with no reset stays stuck accounting (nothing frees it).
    timed = (all(v.get("state") == "held" for v in providers) and
             type(until) in (int, float) and now < until < 253402300799)
    if not timed:
        return None
    pools = ", ".join(f"{v.get('provider')} {v.get('state')}" for v in providers)
    return tid, (f"llm_pacing ({pools}); next reset {datetime.fromtimestamp(until, UTC).isoformat()}", until)


def expected_holds(results, now):
    """Return per-card (explanation, reset) and whether every local candidate is explained.

    Foreign label rows are not local candidates. Unknown outcomes, incomplete census and
    failures retain ordinary stuck accounting. This does not assert that running work progresses.
    """
    holds = {}
    covered_all = True
    for board, res in results:
        if res is None or res.skipped_locked or res.skipped_owned:
            continue
        local = dict(hold for row in res.llm_paced if (hold := _pacing_hold(row, now)))
        for tid, (reason, until) in res.clock_held.items():
            if reason in CLOCK_REASONS and now < until < 253402300799:
                local[tid] = (f"{reason}; until {datetime.fromtimestamp(until, UTC).isoformat()}", until)
        for tid, reason in res.respawn_guarded:
            if str(reason).startswith("start_gate:"):  # a plugin start gate (kanban_start_gate): asked every tick
                local[tid] = (f"{reason}; release when the plugin stops holding starts; recheck next tick", None)
        for tid, reason in res.claim_deferred:
            if reason == "worker_load_pressure":
                local[tid] = ("worker_load_pressure; release when host pressure clears; recheck next tick", None)
        holds.update({(board, tid): detail for tid, detail in local.items()})
        foreign = {tid for tid, reason in res.skipped_ineligible if reason == "labels_mismatch"}
        unexpected = (not dataclasses.is_dataclass(res) or any(
            getattr(res, field.name) for field in dataclasses.fields(res) if field.name not in _COMPATIBLE) or
            any(tid not in local for tid, _ in res.respawn_guarded) or
            any(reason != "worker_load_pressure" for _, reason in res.claim_deferred) or
            any(tid not in foreign for tid, _ in res.skipped_ineligible))
        if unexpected or len(set(local) | foreign) != res.lane_candidates:
            covered_all = False
    return holds, bool(holds) and covered_all


class ExpectedHoldMonitor:
    """One monitor per dispatcher tenure; bounded observation, not an admission-state writer.

    A wait with no reset is reported after 15 minutes; a timed wait after its first reset
    plus 15 minutes. Other cards spawning cannot clear a still-waiting card's age.
    """

    def __init__(self):
        self.last_report = None
        self.waits = {}

    def observe(self, results, now, emit):
        results = list(results)
        holds, covered = expected_holds(results, now)
        observed = {board for board, res in results if res is not None and
                    not res.skipped_locked and not res.skipped_owned}
        self.waits = {key: value for key, value in self.waits.items() if key in holds or key[0] not in observed}
        warnings = []
        for key, (detail, deadline) in holds.items():
            start, first_reset, warned = self.waits.get(key, (now, None, None))
            # Permit the first real reset, but never let rolling refreshes extend it forever.
            first_reset = first_reset if first_reset is not None else deadline
            due = max(start, first_reset or start) + 900
            if now >= due and (warned is None or now - warned >= 300):
                warnings.append(f"{key[0]}/{key[1]} still waiting after {int(now - start)}s; {detail}")
                warned = now
            self.waits[key] = (start, first_reset, warned)
        if warnings:
            emit(logging.WARNING, "kanban admission prolonged: " + "; ".join(warnings) +
                 ". No admission for these cards; running work may still be progressing. "
                 "Inspect pool turnover / host load and these cards' dispatch skips.")
        if holds and (self.last_report is None or now - self.last_report >= 300):
            emit(logging.INFO, "kanban dispatch held: " + "; ".join(
                f"{board}/{tid}: {detail}" for (board, tid), (detail, _) in sorted(holds.items())))
            self.last_report = now
        return covered
