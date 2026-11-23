"""Which model a listed card's newest run uses, for the Desktop running row. [daveey]

``run_model`` is display-only and bound to the card's newest run (``MAX(task_runs.id)``):
- concrete ``session_models`` win: ``kanban_run_models.stamp`` writes them into the run's terminal event
  (``completed``, ``worker_result``, ...), and the backfill CLI writes ``run_models_attested``;
- otherwise the run's newest ``run_route`` model: a concrete pin, or a class such as ``class:deep`` that the
  worker resolves at spawn (the board has no concrete model until the run ends).
A newest run with neither (just claimed, or routed with an unknown model) stamps nothing, never an older
run's model. A failed read stamps nothing.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional

from hermes_cli.kanban_run_models import TERMINAL_KINDS

_KINDS = ("run_route", "run_models_attested", *sorted(TERMINAL_KINDS))


def _payload(raw: Any) -> dict[str, Any]:
    try:
        payload = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _concrete(payload: dict[str, Any]) -> Optional[str]:
    models = payload.get("session_models")
    if not isinstance(models, list):
        return None
    names = [str(m).strip() for m in models if str(m or "").strip()]
    return ", ".join(dict.fromkeys(names)) or None


def add_run_models(conn: sqlite3.Connection, found: dict[str, dict[str, Any]]) -> None:
    """Stamp ``run_model`` on each listed card from its newest run's terminal/attestation/route events.

    One statement: SQLite joins each card's newest run (``MAX(task_runs.id)``) to that run's events through
    ``idx_events_run``. Reading every run's events of these kinds and dropping the older runs' in Python cost a
    result row each (2,228 rows for the hub's 157 listed cards), and in the Desktop backend every row step can wait
    out a GIL switch interval."""
    if not found:
        return
    ids = list(found)
    ph = ",".join("?" for _ in ids)
    try:
        got = conn.execute(
            f"SELECT e.task_id, e.kind, e.payload FROM (SELECT task_id, MAX(id) AS run_id FROM task_runs "
            f"WHERE task_id IN ({ph}) GROUP BY task_id) n JOIN task_events e "
            f"ON e.run_id = n.run_id AND e.task_id = n.task_id "
            f"WHERE e.kind IN ({','.join('?' for _ in _KINDS)}) ORDER BY e.id", [*ids, *_KINDS]).fetchall()
    except sqlite3.Error:
        return
    concrete: dict[str, str] = {}
    routed: dict[str, Optional[str]] = {}
    for task_id, kind, raw in got:  # the newest run's events only, oldest first
        payload = _payload(raw)
        if kind == "run_route":
            routed[task_id] = str(payload.get("model") or "").strip() or None  # an unknown route counts too
        elif model := _concrete(payload):
            concrete[task_id] = model
    for task_id in {**routed, **concrete}:
        model = concrete.get(task_id) or routed.get(task_id)
        if task_id in found and model:
            found[task_id]["run_model"] = model
