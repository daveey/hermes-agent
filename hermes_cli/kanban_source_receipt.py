"""PR-1 source receipt carrier: the receipt rides the status-leaving outbox op.

``staged`` carries a precomputed receipt across voluntary ending helpers. The
one-statement wrapper parks it in the local, never-synced pending table just
for the status UPDATE; the capture trigger binds it to OLD.current_run_id.
Cleanup is unconditional, including a CAS miss or exception. PR-2 supplies the
git preflight/publish producer; PR-1 deliberately leaves the protocol disabled.
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
# Preflight handoff carrier: (task_id, run_id) -> receipt computed before the write transaction (producer in PR-2).
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
    fenced = _consumable(conn, task_id, run_id, receipt)
    conn.execute(f"INSERT OR REPLACE INTO {TABLE} (task_id, run_id, receipt) VALUES (?, ?, ?)",
                 (task_id, run_id, _text(fenced)))
    try:
        out = do_update()
    finally:
        purge(conn, task_id)
    if fenced is not receipt and _left_running(conn, task_id):
        from hermes_cli import kanban_db as kb  # only an ending that actually happened records the refusal
        kb._append_event(conn, task_id, "workspace_source_publish_refused",
                         {"code": "artifact_selection_changed", "detail": "selection changed after the ending preflight",
                          "run_uid": fenced.get("run_uid")}, run_id=run_id)
    return out


def _left_running(conn: sqlite3.Connection, task_id: str) -> bool:
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return bool(row) and row[0] != "running"


def _consumable(conn: sqlite3.Connection, task_id: str, run_id: Optional[int], receipt: Any) -> Any:
    """Final fence, inside the ending's write transaction: a portable receipt whose artifact selection was
    replaced after the preflight (a later authenticated stage) rides as ``retained``, never portable."""
    try:
        obj = json.loads(receipt) if isinstance(receipt, str) else receipt
    except ValueError:
        return receipt  # malformed text: the Board already treats it as retained
    if not isinstance(obj, dict) or obj.get("state") not in ("published", "unchanged"):
        return receipt
    from hermes_cli import kanban_source_artifacts as artifacts
    if artifacts.receipt_selection_current(conn, task_id, run_id, obj):
        return receipt
    return {"run_uid": obj.get("run_uid"), "state": "retained", "manifest_revision": obj.get("manifest_revision"),
            "commit": None, "reason": "artifact_selection_changed"}


def purge(conn: sqlite3.Connection, task_id: str) -> None:
    conn.execute(f"DELETE FROM {TABLE} WHERE task_id = ?", (task_id,))
