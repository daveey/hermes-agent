"""Bound dispatcher SQLite lock waits without changing ordinary writer retry policy. [daveey]"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import logging
import sqlite3
import time

WRITE_WAIT_SECONDS = 1.0
REASON = "storage_write_contended"
_active: ContextVar[_Budget | None] = ContextVar("kanban_dispatch_write_budget", default=None)
_clock = time.monotonic


class DispatchWriteContended(sqlite3.OperationalError):
    """A dispatcher exhausted its SQLite lock-wait allowance; retry next pass."""


@dataclass
class _Budget:
    conn: sqlite3.Connection
    remaining: float
    timeout_ms: int
    contended: bool = False
    preclaim: bool = True

    def execute(self, sql):
        # Charge only boundary acquisition, not RPC/CPU or transaction-body time.
        remaining_ms = max(0, int(self.remaining * 1000))
        self.conn.execute(f"PRAGMA busy_timeout = {min(self.timeout_ms, remaining_ms)}")
        started = _clock()
        try:
            self.conn.execute(sql)
        except sqlite3.OperationalError as exc:
            from hermes_cli.kanban_db_connect import _is_busy_error
            if not _is_busy_error(exc):
                raise
            self.contended = True
            raise DispatchWriteContended(REASON) from exc
        finally:
            self.remaining = max(0, self.remaining - (_clock() - started))


def execute_boundary(conn, sql) -> bool:
    """True when a dispatch-scoped boundary was handled; ordinary writers are unchanged."""
    budget = _active.get()
    if budget is None or budget.conn is not conn or not budget.preclaim:
        return False
    budget.execute(sql)
    return True


def finish_preclaim(conn):
    """Stop cancellation before pickup can acquire execution/process custody."""
    budget = _active.get()
    if budget is None or budget.conn is not conn:
        return
    if budget.contended:
        raise DispatchWriteContended(REASON)  # a maintenance helper may have caught it
    budget.preclaim = False
    conn.execute(f"PRAGMA busy_timeout = {budget.timeout_ms}")


@contextmanager
def _budget(conn):
    previous = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    budget = _Budget(conn, WRITE_WAIT_SECONDS, previous)
    token = _active.set(budget)
    try:
        conn.execute(f"PRAGMA busy_timeout = {min(previous, int(WRITE_WAIT_SECONDS * 1000))}")
        yield budget
    finally:
        _active.reset(token)
        conn.execute(f"PRAGMA busy_timeout = {previous}")


def run(conn, work, *, board):
    """Record a typed pass skip outside the contended board database, never retry its body."""
    from hermes_cli.kanban_db_dispatch import DispatchResult
    with _budget(conn) as budget:
        try:
            result = work()
        except sqlite3.OperationalError as exc:
            from hermes_cli.kanban_db_connect import _is_busy_error
            if not budget.preclaim or (not isinstance(exc, DispatchWriteContended) and not _is_busy_error(exc)):
                raise
            budget.contended = True
            result = DispatchResult()
        if budget.contended:
            result.storage_skip_reason = REASON
            logging.getLogger(__name__).warning("kanban dispatch skipped: reason=%s board=%s", REASON, board)
    return result
