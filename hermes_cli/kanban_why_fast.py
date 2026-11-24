"""Read a fresh dispatcher snapshot without initializing a board or importing dispatch.

The early CLI path is deliberately narrow. Fenced executions and nonstandard
selectors retain the canonical CLI's resolution and admission checks.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sqlite3
import time

VERSION = 1
STALE_SECONDS = 180
SUFFIX = ".governors.json"


def read_answer(path: Path, task_id: str, *, now: float | None = None) -> dict | None:
    """Return saved evidence only; missing/stale/incompatible evidence falls back.

    One indexed, read-only card lookup prevents a finished or changed card from
    inheriting its old tick's hold. No schema initialization or integrity scans.
    """
    started = time.monotonic()
    now = time.time() if now is None else now
    try:
        doc = json.loads(path.with_name(path.name + SUFFIX).read_text(encoding="utf-8-sig"))
        if not isinstance(doc, dict) or doc.get("version") != VERSION:
            return None
        age = now - float(doc.get("tick_at") or 0)
        if not 0 <= age <= STALE_SECONDS:
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
        recorded = {**saved, "task_id": task_id, "board": doc.get("board"), "tick_at": doc["tick_at"],
                    "age_s": round(age, 1), "stale": False, "globals": doc.get("globals")}
        return answer(recorded, started=started)
    except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error):
        return None


def answer(recorded: dict, *, started: float) -> dict:
    """Shared rendering shape for cached and connected consumers."""
    first = {k: recorded.get(k) for k in ("governor", "label", "scope", "reading", "threshold", "since", "until",
                                         "detail", "source", "age_s")}
    first["verdict"] = "stop"
    return {"task_id": recorded["task_id"], "board": recorded.get("board"), "source": "snapshot",
            "status": recorded.get("status"), "first_stop": first, "overlaps": recorded.get("overlaps"),
            "overlaps_unknown": recorded.get("overlaps_unknown"), "globals": recorded.get("globals"),
            "chain": [first], "recorded": recorded, "elapsed_s": round(time.monotonic() - started, 3)}


def try_cli(argv: list[str]) -> bool:
    """Handle only unambiguous, unfenced snapshot JSON invocations."""
    if argv[:2] != ["kanban", "why"] or len(argv) < 4 or "--json" not in argv:
        return False
    # Do not duplicate the canonical worker/delegation/profile board fences.
    from agent.delegation_context import explicit_board_intent_is_pinned

    if explicit_board_intent_is_pinned() or any(os.environ.get(k) for k in (
            "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD")):
        return False
    task_id, flags = argv[2], argv[3:]
    board = "default"
    if flags == ["--json"]:
        pass
    elif len(flags) == 3 and flags[0] == "--board" and flags[2] == "--json":
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
    if flags == ["--json"] and (root / "kanban" / "current").exists():
        return False  # canonical resolver handles current-board metadata and tombstones
    path = root / "kanban.db" if board == "default" else root / "kanban" / "boards" / board / "kanban.db"
    result = read_answer(path, task_id)
    if result is None or result.get("board") != board:
        return False
    print(json.dumps(result, ensure_ascii=False, default=str))
    return True
