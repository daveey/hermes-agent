"""Which model a listed card's newest run uses, for the Desktop running row. [daveey]

``run_model`` is display-only, read from the card's own events for its newest run:
- ``run_models_attested`` (the concrete session models, recorded once the run ends) wins;
- otherwise the newest ``run_route`` names the routed model: a concrete pin, or a class such as
  ``class:deep`` that the worker resolves at spawn (the board has no concrete model until attestation).
A failed read or a run with no route event stamps nothing.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional

_KINDS = ("run_route", "run_models_attested")


def _model(kind: str, raw: Any) -> Optional[str]:
    try:
        payload = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if kind == "run_models_attested":
        models = [str(m).strip() for m in payload.get("session_models") or [] if str(m or "").strip()]
        return ", ".join(dict.fromkeys(models)) or None
    model = str(payload.get("model") or "").strip()
    return model or None


def add_run_models(conn: sqlite3.Connection, found: dict[str, dict[str, Any]]) -> None:
    """Stamp ``run_model`` on each listed card from its newest run's route/attestation events."""
    if not found:
        return
    ids = list(found)
    try:
        got = conn.execute(
            f"SELECT task_id, run_id, kind, payload FROM task_events WHERE task_id IN ({','.join('?' for _ in ids)}) "
            f"AND run_id IS NOT NULL AND kind IN ({','.join('?' for _ in _KINDS)}) ORDER BY id",
            [*ids, *_KINDS]).fetchall()
    except sqlite3.Error:
        return
    best: dict[str, tuple[int, bool, str]] = {}  # task -> (run_id, attested, model)
    for task_id, run_id, kind, payload in got:
        model = _model(kind, payload)
        if model is None:
            continue
        attested = kind == "run_models_attested"
        prev = best.get(task_id)
        if prev is None or run_id > prev[0] or (run_id == prev[0] and (attested or not prev[1])):
            best[task_id] = (run_id, attested, model)
    for task_id, (_, _, model) in best.items():
        if task_id in found:
            found[task_id]["run_model"] = model
