"""``hermes kanban governors`` and ``hermes kanban why``: read what the dispatcher recorded. [daveey]

``governors`` prints each board's snapshot (``kanban_governors.snapshot``): per governor the reading, threshold,
since and the cards charged to it. ``why <card>`` answers from that snapshot (the first stop the last tick
actually made, in well under a second); when the last tick did not charge the card, or with ``--live``, it walks
the rules itself (``kanban_governors_explain.explain``, read-only) and prints the whole chain.
"""
from __future__ import annotations

import argparse
import time
from typing import Any, Optional

from hermes_cli import kanban_governors as governors


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


def board_slugs(args: argparse.Namespace) -> list[Optional[str]]:
    if not getattr(args, "all_boards", False):
        return [getattr(args, "board", None)]
    from hermes_cli import kanban_db as kb

    return [meta.get("slug") or kb.DEFAULT_BOARD for meta in kb.list_boards(include_archived=False)]


def read_boards(args: argparse.Namespace) -> list[dict[str, Any]]:
    from hermes_cli import kanban_db_connect as kbc

    out = []
    for slug in board_slugs(args):
        try:
            with kbc.connect_closing(board=slug) as conn:
                out.append(governors.snapshot(conn))
        except Exception as exc:  # health: allow BLE001 -- one unreadable board must not hide the others
            out.append({"version": governors.VERSION, "board": slug, "available": False, "stale": True,
                        "error": f"{type(exc).__name__}: {exc}", "governors": [], "cards": {}})
    return out


def _print_board(snap: dict[str, Any], now: float) -> None:
    if not snap.get("available"):
        why = snap.get("error") or "no dispatcher tick has recorded governors on this board yet"
        print(f"board {snap.get('board')}: no data ({why})")
        return
    stale = "  STALE: no tick for that long, treat as unknown" if snap.get("stale") else ""
    print(f"board {snap['board']}: {snap['idle_total']} idle card(s), tick {snap['age_s']}s ago{stale}")
    for step in snap.get("globals") or ():
        if step.get("verdict") != "pass":
            note = "not reached this tick" if step.get("reached") is False else step.get("reason") or ""
            print(f"  board rule {step['verdict']:<8} {_name(step):<26} {_measure(step):<14} {note}".rstrip())
    for row in snap["governors"]:
        if not row.get("active") and row.get("reading") is None:
            continue
        state = f"{row['cards']} card(s) since {_ago(row.get('since'), now)}" if row.get("active") else "ok"
        ids = " ".join(row.get("card_ids") or [])
        print(f"  {_name(row):<34} {row.get('kind', ''):<9} {_measure(row):<14} {state}  {ids}".rstrip())


def governors_command(args: argparse.Namespace) -> int:
    from hermes_cli.kanban_output import _print_json

    boards = read_boards(args)
    if getattr(args, "json", False):
        _print_json({"version": governors.VERSION, "generated_at": time.time(), "registry": governors.registry(),
                     "boards": boards})
        return 0
    now = time.time()
    for snap in boards:
        _print_board(snap, now)
    return 0


def why(conn: Any, task_id: str, *, live: bool = False) -> dict[str, Any]:
    """The recorded first stop when the last tick charged the card (``source: snapshot``), else the dry run."""
    started = time.monotonic()
    recorded = None if live else governors.card(conn, task_id)
    if recorded is None or recorded.get("stale"):
        from hermes_cli.kanban_governors_explain import explain

        return explain(conn, task_id)
    from hermes_cli.kanban_why_fast import answer

    return answer(recorded, started=started)


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
        print(f"  source: {first.get('source')}  ({answer['source']}, {first.get('age_s')}s old)")
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
    known = governors.governor(str(entry.get("governor")))
    return known.label if known else str(entry.get("governor"))


def why_command(args: argparse.Namespace) -> int:
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli.kanban_output import _print_json

    with kbc.connect_closing(board=getattr(args, "board", None)) as conn:
        answer = why(conn, args.task_id, live=getattr(args, "live", False))
    if getattr(args, "json", False):
        _print_json(answer)
    else:
        _print_why(answer)
    return 1 if answer.get("error") else 0
