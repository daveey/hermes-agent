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
from typing import Any, Callable, Optional

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


def with_source_receipt(conn: sqlite3.Connection, task_id: str, do_update: Callable[[], Any], *,
                        run_id: Optional[int] = None, receipt: Any = None) -> Any:
    """Run ``do_update`` (the ONE status-leaving UPDATE of an ending) with the pending receipt alive for exactly
    that statement: INSERT immediately before it, unconditional DELETE immediately after it, whatever its rowcount
    and even if it raises. ``receipt`` defaults to the staged preflight receipt for the card's current run. With no
    receipt the DELETE still runs first, so a leftover row can never ride this ending."""
    run_id = _current_run_id(conn, task_id) if run_id is None else run_id
    receipt = staged_receipt(task_id, run_id) if receipt is None else receipt
    if receipt is None:
        purge(conn, task_id)
        return do_update()
    conn.execute(f"INSERT OR REPLACE INTO {TABLE} (task_id, run_id, receipt) VALUES (?, ?, ?)",
                 (task_id, run_id, _text(receipt)))
    try:
        return do_update()
    finally:
        purge(conn, task_id)


def purge(conn: sqlite3.Connection, task_id: str) -> None:
    conn.execute(f"DELETE FROM {TABLE} WHERE task_id = ?", (task_id,))
