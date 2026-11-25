"""Read a fresh dispatcher snapshot without initializing a board or importing dispatch.

The early CLI path is deliberately narrow. Fenced executions and nonstandard
selectors retain the canonical CLI's resolution and admission checks.
"""
from __future__ import annotations

import json
from itertools import pairwise
import math
import os
from pathlib import Path
import re
import sqlite3
import time

VERSION = 1
STALE_SECONDS = 180
SUFFIX = ".governors.json"
MAX_FRESHNESS_SECONDS = 600
GAP_HEADROOM_SECONDS = 60
RECENT_COMPLETIONS = 21  # twenty completed gaps; nearest-rank p95


def freshness_bound(doc: dict) -> float:
    """Legacy snapshots retain 180 s; malformed tolerances fail closed."""
    value = doc.get("freshness_bound_s", STALE_SECONDS)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        return 0.0
    return min(float(value), MAX_FRESHNESS_SECONDS)


def completion_policy(previous: dict, now: float, interval: float) -> dict:
    """Only the real post-dispatch recorder may append a completion timestamp."""
    raw = previous.get("tick_completions", [previous.get("tick_at")])
    times = [v for v in raw[-RECENT_COMPLETIONS:] if isinstance(v, (int, float))
             and not isinstance(v, bool) and math.isfinite(v) and 0 < v < now] if isinstance(raw, list) else []
    times = sorted(set(times + [now]))[-RECENT_COMPLETIONS:]
    gaps = sorted(b - a for a, b in pairwise(times))
    p95 = gaps[math.ceil(.95 * len(gaps)) - 1] if gaps else 0.0
    interval = interval if math.isfinite(interval) and interval > 0 else 60.0
    bound = min(MAX_FRESHNESS_SECONDS, max(3 * interval, p95 + GAP_HEADROOM_SECONDS))
    return {"tick_completions": times, "tick_gap_p95_s": p95, "dispatch_interval_s": interval,
            "freshness_bound_s": bound}


def read_answer(path: Path, task_id: str, *, now: float | None = None) -> dict | None:
    """Return saved evidence only; missing/stale/incompatible evidence falls back.

    One indexed, read-only card lookup prevents a finished or changed card from
    inheriting its old tick's hold. No schema initialization or integrity scans.
    """
    started = time.monotonic()
    try:
        doc = json.loads(path.with_name(path.name + SUFFIX).read_text(encoding="utf-8-sig"))
        if not isinstance(doc, dict) or doc.get("version") != VERSION:
            return None
        age = (time.time() if now is None else now) - float(doc.get("tick_at") or 0)
        if not 0 <= age <= freshness_bound(doc):
            return None
        saved = doc.get("cards", {}).get(task_id)
        if not isinstance(saved, dict) or not saved.get("label") or not saved.get("source"):
            return None
        from hermes_cli.kanban_lockguard import connect

        conn = connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1)
        try:
            row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        finally:
            conn.close()
        if row is None or row[0] != saved.get("status"):
            return None
        # Status I/O may have crossed expiry; never return the pre-I/O age.
        age = (time.time() if now is None else now) - float(doc.get("tick_at") or 0)
        if not 0 <= age <= freshness_bound(doc):
            return None
        recorded = {**saved, "task_id": task_id, "board": doc.get("board"), "tick_at": doc["tick_at"],
                    "age_s": round(age, 1), "freshness_bound_s": freshness_bound(doc),
                    "stale": False, "globals": doc.get("globals")}
        return answer(recorded, started=started)
    except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error):
        return None


def answer(recorded: dict, *, started: float) -> dict:
    """Shared rendering shape for cached and connected consumers."""
    first = {k: recorded.get(k) for k in ("governor", "label", "scope", "reading", "threshold", "since", "until",
                                         "detail", "source", "age_s", "freshness_bound_s")}
    first["verdict"] = "stop"
    return {"task_id": recorded["task_id"], "board": recorded.get("board"), "source": "snapshot",
            "age_s": recorded.get("age_s"), "freshness_bound_s": recorded.get("freshness_bound_s"),
            "status": recorded.get("status"), "first_stop": first, "overlaps": recorded.get("overlaps"),
            "overlaps_unknown": recorded.get("overlaps_unknown"), "globals": recorded.get("globals"),
            "chain": [first], "recorded": recorded, "elapsed_s": round(time.monotonic() - started, 3)}


def try_cli(argv: list[str]) -> bool:
    """Handle only unambiguous, unfenced snapshot invocations."""
    if argv[:2] != ["kanban", "why"] or len(argv) < 3:
        return False
    # Do not duplicate the canonical worker/delegation/profile board fences.
    from agent.delegation_context import explicit_board_intent_is_pinned

    if explicit_board_intent_is_pinned() or any(os.environ.get(k) for k in (
            "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD")):
        return False
    task_id, flags = argv[2], argv[3:]
    board = "default"
    if flags in ([], ["--json"]):
        pass
    elif (len(flags) == 2 and flags[0] == "--board") or (
            len(flags) == 3 and flags[0] == "--board" and flags[2] == "--json"):
        board = flags[1]
    elif len(flags) == 3 and flags[0] == "--json" and flags[1] == "--board":
        board = flags[2]
    else:
        return False
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", board):
        return False
    from hermes_constants import get_default_hermes_root

    root = Path(os.environ["HERMES_KANBAN_HOME"]).expanduser() if os.environ.get("HERMES_KANBAN_HOME") \
        else get_default_hermes_root()
    if "--board" not in flags and (root / "kanban" / "current").exists():
        return False  # canonical resolver handles current-board metadata and tombstones
    path = root / "kanban.db" if board == "default" else root / "kanban" / "boards" / board / "kanban.db"
    # sqlite-sidecar-open: ok — read_answer opens SQLite through lockguard; plain file read is unrelated JSON
    result = read_answer(path, task_id)
    if result is None or result.get("board") != board:
        return False
    if "--json" in flags:
        print(json.dumps(result, ensure_ascii=False, default=str))
    else:
        from hermes_cli.kanban_why_render import _print_why

        _print_why(result)
    return True
