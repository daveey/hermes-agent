"""Spike R1 (t_01a1d0bdfa1955d5): the source receipt rides the outbox op that moves a card out of ``running``.

The receipt is computed before the ending's write transaction (the preflight; here a stub carrier, ``staged``)
and parked in the local, never-synced ``kanban_source_receipt_pending`` row. The tasks UPDATE capture trigger
(``kanban_db_sync._update_trigger``) reads it into the same outbox op as the status change, only when that UPDATE
leaves ``running`` for the run the row names.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import sqlite3
from typing import Any, Optional

TABLE = "kanban_source_receipt_pending"
DDL = (f"CREATE TABLE IF NOT EXISTS {TABLE} (task_id TEXT PRIMARY KEY, run_id INTEGER NOT NULL, "
       "receipt TEXT NOT NULL CHECK (json_valid(receipt)))")
# Stub preflight carrier: (task_id, run_id) -> receipt computed before the write transaction.
_STAGED: contextvars.ContextVar[dict] = contextvars.ContextVar("kanban_source_receipt_staged", default={})


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(DDL)


def trigger_pair() -> str:
    """The ``(k, v)`` row the tasks UPDATE capture trigger adds to its ``UNION ALL``."""
    return (f"SELECT 'source_receipt' AS k, p.receipt AS v FROM {TABLE} AS p WHERE p.task_id = NEW.id "
            "AND p.run_id = OLD.current_run_id AND OLD.status = 'running' AND NEW.status IS NOT 'running'")


@contextlib.contextmanager
def staged(task_id: str, run_id: int, receipt: Any):
    token = _STAGED.set({**_STAGED.get(), (task_id, int(run_id)): receipt})
    try:
        yield
    finally:
        _STAGED.reset(token)


def staged_receipt(task_id: str, run_id: Optional[int]) -> Any:
    return None if run_id is None else _STAGED.get().get((task_id, int(run_id)))


def _current_run_id(conn: sqlite3.Connection, task_id: str) -> Optional[int]:
    row = conn.execute("SELECT current_run_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return row[0] if row else None


def _text(receipt: Any) -> str:
    return receipt if isinstance(receipt, str) else json.dumps(receipt, sort_keys=True, separators=(",", ":"))


def stage_pending(conn: sqlite3.Connection, task_id: str) -> None:
    """Plan v4 §2.1 as written: the first statement of the ending's write transaction."""
    run_id = _current_run_id(conn, task_id)
    receipt = staged_receipt(task_id, run_id)
    if receipt is not None:
        conn.execute(f"INSERT OR REPLACE INTO {TABLE} (task_id, run_id, receipt) VALUES (?, ?, ?)",
                     (task_id, run_id, _text(receipt)))
