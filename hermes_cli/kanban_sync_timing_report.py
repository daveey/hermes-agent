"""Read a ``kanban-sync-timing.jsonl`` capture (``hermes_cli.kanban_sync_timing``) into neutral measurements.

``python -m hermes_cli.kanban_sync_timing_report <file>`` prints one JSON summary per capture. It applies no
threshold and no baseline: it reports validity (any lost record, sequence gap, diagnostic error, identity change,
missing stop marker, incomplete stop reason, schema/event/allowlist violation, malformed record, or a start without a
boot-recorded code identity or with a moved checkout invalidates the capture; records before the first ``start`` are
reported as their own invalid capture, never dropped) and measured distributions only:

- per decoded change served by its own board's successful service: notification-to-service latency, and how much
  of that interval overlapped measured report spans and diagnostic flush spans; cohorts with and without report
  overlap, plus the overlap residual (latency minus the measured overlapping report time, the same interval's own
  measurement, not another route's median); a capture with no overlapping change has coverage ``none``;
- failed/raised services, changes still unserviced at the stop marker (censored, never dropped);
- due-report overlap and no-due-report-overlap cohorts separately from the compatibility total-report cohorts;
  due means a recorded collect or send component, including failed/raised reports, never the result string or a
  duration threshold. Overlap uses the full due report interval, not just its components; residuals subtract only
  that interval's own measured overlap, not a counterfactual or a latency acceptance verdict;
- full report durations (due and not-due cohorts), their components and send outcomes;
- the diagnostic's own flush and hook overhead.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from hermes_cli.kanban_sync_timing import ENUMS, SCHEMA, _allowed

COMPLETE_REASONS = frozenset({"expired", "bound", "disabled"})
_NS_PER_MS = 1_000_000


def load(path: Path) -> list[dict]:
    out = []
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            rec = None
        out.append(rec if isinstance(rec, dict) else {"ev": "_bad"})
    return out


def captures(records: Iterable[dict]) -> list[list[dict]]:
    """Split at each ``start`` record. Records before the first start (orphans, junk, a capture whose start is
    missing) are kept as their own leading capture, which :func:`validate` rejects as ``no_start_marker``."""
    caps: list[list[dict]] = []
    for rec in records:
        if rec.get("ev") == "start" or not caps:
            caps.append([])
        caps[-1].append(rec)
    return caps


def validate(cap: list[dict]) -> list[str]:
    """Reasons this capture cannot serve as evidence; empty means complete and lossless."""
    problems = []
    start = cap[0] if cap else {}
    stop = cap[-1] if cap and cap[-1].get("ev") == "stop" else None
    if start.get("ev") != "start":
        problems.append("no_start_marker")
    if stop is None:
        problems.append("no_stop_marker")
    if any(r.get("ev") == "_bad" for r in cap):
        problems.append("unparseable_line")
    if [r.get("seq") for r in cap] != list(range(len(cap))):
        problems.append("sequence_gap")
    if start.get("schema") != SCHEMA:
        problems.append("schema_mismatch")
    real = [r for r in cap if r.get("ev") != "_bad"]
    if any(r.get("ev") not in ENUMS["ev"] for r in real):
        problems.append("unknown_event")
    if any(not _allowed(k, v) for r in real for k, v in r.items()):
        problems.append("field_not_allowed")
    if start.get("code_src") != "boot" or "code" not in start or start.get("disk_moved") is True:
        problems.append("identity_unverified")
    if stop is not None:
        if stop.get("lost"):
            problems.append("lost_records")
        if stop.get("errors"):
            problems.append("diagnostic_errors")
        if stop.get("reason") not in COMPLETE_REASONS:
            problems.append(f"incomplete_{stop.get('reason')}")
        if (stop.get("run"), stop.get("pid")) != (start.get("run"), start.get("pid")):
            problems.append("identity_changed")
        if stop.get("valid") is not True and not problems:
            problems.append("marked_invalid")
    return problems


def _dist_ms(values_ns: list[int]) -> dict:
    if not values_ns:
        return {"n": 0}
    ms = sorted(v / _NS_PER_MS for v in values_ns)

    def rank(p: int) -> float:  # nearest-rank percentile
        return ms[max(0, math.ceil(len(ms) * p / 100) - 1)]
    return {"n": len(ms), "min": ms[0], "p50": rank(50), "p90": rank(90), "max": ms[-1]}


def _overlap(a0: int, a1: int, spans: list[tuple[int, int]]) -> int:
    return sum(max(0, min(a1, s1) - max(a0, s0)) for s0, s1 in spans)


def _served(cap: list[dict], report_spans, flush_spans, due_report_spans=()) -> list[dict]:
    changes = {r["seq"]: r for r in cap if r.get("ev") == "chg"}
    done: set[int] = set()
    served = []
    for svc in cap:
        if svc.get("ev") != "svc" or svc.get("out") != "ok" or "c_first" not in svc:
            continue
        for seq in range(svc["c_first"], svc["c_last"] + 1):
            chg = changes.get(seq)
            if chg is None or chg["b"] != svc["b"] or seq in done:
                continue
            done.add(seq)
            served.append({"seq": seq, "b": svc["b"], "latency_ns": svc["t1"] - chg["t"],
                           "to_start_ns": svc["t0"] - chg["t"], "failed_attempts": svc.get("fails", 0),
                           "report_overlap_ns": _overlap(chg["t"], svc["t1"], report_spans),
                           "due_report_overlap_ns": _overlap(chg["t"], svc["t1"], due_report_spans),
                           "flush_overlap_ns": _overlap(chg["t"], svc["t1"], flush_spans)})
    return served


def _is_due(rep: dict) -> bool:
    return "collect_t0" in rep or "send_t0" in rep


def _reports(reps: list[dict]) -> dict:
    due = [r for r in reps if _is_due(r)]
    parts = {name: _dist_ms([r[f"{name}_t1"] - r[f"{name}_t0"] for r in due if f"{name}_t0" in r])
             for name in ("collect", "limit", "cpu", "adv", "send")}
    return {"due": _dist_ms([r["t1"] - r["t0"] for r in due]),
            "not_due": _dist_ms([r["t1"] - r["t0"] for r in reps if r not in due]),
            "components": parts,
            "send_outcomes": dict(Counter(f"{r['send_out']}:{r.get('send_st', '')}" for r in due if "send_out" in r)),
            "cpu_window_ms": sorted({r["cpu_win_ms"] for r in due if "cpu_win_ms" in r})}


def summarize(cap: list[dict]) -> dict[str, Any]:
    problems = validate(cap)
    start, stop = cap[0], (cap[-1] if cap[-1].get("ev") == "stop" else {})
    try:
        measured = _measure(cap, stop)
    except (KeyError, TypeError, ValueError, ArithmeticError):  # a record missing or mistyping a needed field
        problems, measured = problems + ["malformed_record"], {}
    return {
        "valid": not problems, "invalid": problems,
        "identity": {k: start.get(k) for k in ("run", "pid", "code", "code_src", "disk_moved")},
        "stop_reason": stop.get("reason"), "records": len(cap), "lost": stop.get("lost"),
        **measured,
    }


def _measure(cap: list[dict], stop: dict) -> dict[str, Any]:
    start = cap[0]
    by = {ev: [r for r in cap if r.get("ev") == ev] for ev in ("rep", "flush", "svc", "pend", "chg", "wait")}
    report_spans = [(r["t0"], r["t1"]) for r in by["rep"]]
    due_report_spans = [(r["t0"], r["t1"]) for r in by["rep"] if _is_due(r)]
    flush_spans = [(r["t0"], r["t1"]) for r in by["flush"]]
    served = _served(cap, report_spans, flush_spans, due_report_spans)
    overlap = [s for s in served if s["report_overlap_ns"] > 0]
    plain = [s for s in served if s["report_overlap_ns"] == 0]
    due_overlap = [s for s in served if s["due_report_overlap_ns"] > 0]
    no_due_overlap = [s for s in served if s["due_report_overlap_ns"] == 0]
    local = [r["t1"] - r["l_t"] for r in by["svc"] if r.get("out") == "ok" and r.get("loop") and "l_t" in r]
    return {
        "duration_ms": (stop["t"] - start["t"]) / _NS_PER_MS if "t" in stop else None,
        "changes": len(by["chg"]), "served": served,
        "coverage": "overlap" if overlap else "none",
        "due_report_coverage": "overlap" if due_overlap else "none",
        "cohorts": {"overlap": _dist_ms([s["latency_ns"] for s in overlap]),
                    "non_overlap": _dist_ms([s["latency_ns"] for s in plain]),
                    "overlap_residual_ms": _dist_ms([s["latency_ns"] - s["report_overlap_ns"] for s in overlap]),
                    "due_report_overlap": _dist_ms([s["latency_ns"] for s in due_overlap]),
                    "no_due_report_overlap": _dist_ms([s["latency_ns"] for s in no_due_overlap]),
                    "due_report_overlap_residual_ms": _dist_ms(
                        [s["latency_ns"] - s["due_report_overlap_ns"] for s in due_overlap]),
                    "all": _dist_ms([s["latency_ns"] for s in served])},
        "local_to_service": _dist_ms(local),
        "service_failures": sum(r.get("out") != "ok" for r in by["svc"]),
        # Changes still pending at stop, and separately the boards beyond the stop's PEND_MAX records (their change
        # count is unknown): different units, never summed.
        "unserviced": sum(r.get("c_n", 0) for r in by["pend"]),
        "unserviced_boards_truncated": stop.get("pend_more") or 0,
        "reports": _reports(by["rep"]),
        "waits": dict(Counter(r.get("why") for r in by["wait"])),
        "overhead": {"flushes": len(by["flush"]), "flush": _dist_ms([e - s for s, e in flush_spans]),
                     "flush_ns_total": stop.get("flush_ns"), "hook_ns_total": stop.get("ovh_ns")},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("path", type=Path)
    args = parser.parse_args(argv)
    summaries = [summarize(cap) for cap in captures(load(args.path))]
    json.dump(summaries, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0 if summaries and all(s["valid"] for s in summaries) else 1


if __name__ == "__main__":
    raise SystemExit(main())
