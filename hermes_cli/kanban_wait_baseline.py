"""Park-time baselines for generated card probes, not explicit user commands. [daveey]"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

DEPARTED = "awaited_card_departed"


def _latest(query, waiter):
    from hermes_cli.kanban_continuation import SCHEDULED_ENTRY_EVENTS
    kinds = (*SCHEDULED_ENTRY_EVENTS, "condition_met", DEPARTED)
    marks = ",".join("?" for _ in kinds)
    rows = query(f"SELECT kind, payload FROM task_events WHERE task_id=? AND kind IN ({marks}) "
                 "ORDER BY id DESC LIMIT 1", (waiter, *kinds))
    if not rows:
        return None
    kind, raw = rows[0]
    try:
        payload = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if kind in ("condition_met", DEPARTED):
        return None if kind == "condition_met" else (payload.get("ref"), None, payload.get("wake_check"))
    record = payload.get("awaiting") or {}
    if not isinstance(record, dict) or record.get("kind") != "card" or record.get("auto_card_probe") is not True:
        return None
    from hermes_cli.kanban_awaiting import card_probe
    cmd = card_probe(dict(record))
    if payload.get("wake_check") != cmd:
        return None
    target = record.get("ref")
    words = str(record.get("observed") or "").split()
    status = words[1] if len(words) >= 2 and words[0] == target else None
    return target, status if status in ("blocked", "review") else None, cmd


def park_status(waiter, db_path, target):
    """(applies, baseline); a newer condition_met leaves the existing handled-status rule in charge."""
    if not waiter or not db_path:
        return False, None
    from hermes_cli.kanban_wait_check import _query
    try:
        found = _latest(lambda sql, args: _query(Path(db_path), sql, args), waiter)
    except (ValueError, TypeError, AttributeError, OSError, sqlite3.Error):
        return False, None
    if found and found[0] == target:
        return True, found[1]
    return False, None


def record_departure(conn, waiter, cmd, output):
    """Retire the baseline on an observed nonterminal change, inside the generation-fenced result txn.

    A probe subprocess never writes the board. Persist once here so a later return to blocked/review wakes,
    including after a dispatcher restart or an intervening transport error.
    """
    found = _latest(lambda sql, args: conn.execute(sql, args).fetchall(), waiter)
    if not found or found[1] is None or found[2] != cmd:
        return
    words = output.split()
    if len(words) < 2 or words[0] != found[0] or words[1] not in ("todo", "ready", "running", "scheduled"):
        return
    from hermes_cli import kanban_db as kb
    kb._append_event(conn, waiter, DEPARTED, {"ref": found[0], "wake_check": cmd, "status": words[1]})
