"""Lightweight shared text renderer for snapshot and live Kanban explanations."""
from __future__ import annotations

import time
from typing import Any


def _ago(since: Any, now: float) -> str:
    if not isinstance(since, (int, float)):
        return "-"
    seconds = max(0, int(now - since))
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def _value(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, dict):
        return ", ".join(f"{k}={v}" for k, v in value.items())
    return str(value)


def _measure(row: dict[str, Any]) -> str:
    if row.get("reading") is None and row.get("threshold") is None:
        return ""
    return f"{_value(row.get('reading'))}/{_value(row.get('threshold'))}"


def _name(row: dict[str, Any]) -> str:
    label = row.get("label") or row.get("governor")
    return f"{label} · {row['scope']}" if row.get("scope") else str(label)


def _print_why(answer: dict[str, Any]) -> None:
    now = time.time()
    if answer.get("error"):
        print(f"{answer['task_id']}: {answer['error']}")
        return
    first = answer.get("first_stop")
    head = f"{answer['task_id']} ({answer.get('status')}, board {answer.get('board')})"
    unknowns = [step for step in (answer.get("chain") or ()) if step.get("verdict") == "unknown"]
    if first is None and unknowns:
        note = f" ({answer['note']})" if answer.get("note") else ""
        print(f"{head}: unknown whether anything holds it ({len(unknowns)} rules not evaluated){note}")
    elif first is None:
        print(f"{head}: nothing holds it" + (f" ({answer['note']})" if answer.get("note") else ""))
    else:
        measure = f" {_measure(first)}" if _measure(first) else ""
        print(f"{head}: stopped by {_name(first)}{measure}, since {_ago(first.get('since'), now)}")
        tolerance = f"; tolerance {answer.get('freshness_bound_s'):g}s; recorded decision, not live" \
            if answer.get("source") == "snapshot" and answer.get("freshness_bound_s") is not None else ""
        print(f"  source: {first.get('source')}  ({answer['source']}, {first.get('age_s')}s old{tolerance})")
        if first.get("detail"):
            print(f"  detail: {_value(first['detail'])}")
    overlaps = answer.get("overlaps")
    if overlaps is None:
        print("  also held by: not evaluated yet (run with --live)")
    for other in overlaps or ():
        print(f"  also held by: {_name({**other, 'label': other.get('label') or _label(other)})} {_measure(other)}".rstrip())
    for other in answer.get("overlaps_unknown") or ():
        note = other.get("reason") or "unknown"
        print(f"  also unknown: {_name({**other, 'label': other.get('label') or _label(other)})} ({note})")
    if answer["source"] == "dry_run":
        for step in answer.get("chain") or ():
            print(f"    {step['verdict']:<8} {_name(step):<34} {_measure(step):<14} {_value(step.get('detail'))}".rstrip())
    elif answer.get("globals"):
        print("  board rules (this tick):")
        for step in answer["globals"]:
            note = "not reached" if step.get("reached") is False else _value(step.get("detail"))
            print(f"    {step['verdict']:<8} {_name(step):<34} {_measure(step):<14} {note}".rstrip())


def _label(entry: dict[str, Any]) -> str:
    from hermes_cli import kanban_governors as governors

    known = governors.governor(str(entry.get("governor")))
    return known.label if known else str(entry.get("governor"))
