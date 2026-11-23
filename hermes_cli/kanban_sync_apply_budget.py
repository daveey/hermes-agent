"""Short feed writer reservations and cooperative outbox service between them.

The atomic apply API remains available for repair callers. Feed callers commit
ordered prefixes, each with its cursor. No cursor is advanced past an uncommitted
row. SQLite VM deadlines abort oversized batches; retry halves them, and a single
oversized row fails closed rather than monopolizing the writer indefinitely.
"""
from __future__ import annotations

import contextlib
import contextvars
import sqlite3
import threading
import time
from itertools import pairwise
from typing import Any

MAX_ROWS = 32
MAX_TRANSACTION_S = 0.25
PUSH_INTERVAL_S = 5.0
WRITER_YIELD_S = 0.1
_LIMIT: contextvars.ContextVar[Any] = contextvars.ContextVar("sync_apply_limit", default=None)
_DEADLINE: contextvars.ContextVar[Any] = contextvars.ContextVar("sync_apply_deadline", default=None)
_SERVICE: contextvars.ContextVar[Any] = contextvars.ContextVar("sync_pull_service", default=None)
_YIELD = threading.Event()


class ApplyBudgetExceeded(sqlite3.OperationalError):
    """A rolled-back feed batch exceeded its writer budget."""


def check_budget():
    deadline = _DEADLINE.get()
    if deadline is not None and time.monotonic() >= deadline:
        raise ApplyBudgetExceeded("feed transaction exceeded writer budget")


@contextlib.contextmanager
def transaction_budget(conn):
    """Installed only after BEGIN, removed before rollback/COMMIT.

    This bounds SQLite VM work and checks Python-side work at transaction exit.
    OS scheduling, fsync and one SQLite opcode are not preemptible wall-time
    guarantees. No networking belongs inside the apply transaction.
    """
    limit = _LIMIT.get()
    if limit is None:
        yield
        return
    deadline = time.monotonic() + limit
    token = _DEADLINE.set(deadline)
    def expired():
        return time.monotonic() >= deadline
    conn.set_progress_handler(expired, 1000)
    try:
        yield
        if expired():
            raise ApplyBudgetExceeded("feed transaction exceeded writer budget")
    except sqlite3.OperationalError as exc:
        if expired() and "interrupted" in str(exc):
            raise ApplyBudgetExceeded("feed transaction exceeded SQLite VM budget") from exc
        raise
    finally:
        conn.set_progress_handler(None, 0)
        _DEADLINE.reset(token)


@contextlib.contextmanager
def push_service(callback):
    """A round supplies a non-transactional, existing-policy send opportunity."""
    token = _SERVICE.set([callback, time.monotonic()])
    try:
        yield
    finally:
        _SERVICE.reset(token)


def service_pushes():
    service = _SERVICE.get()
    if service is not None and time.monotonic() - service[1] >= PUSH_INTERVAL_S:
        service[0]()
        service[1] = time.monotonic()


def round_pushes(live, hub, slug, gateway, mode, result, evidence):
    from hermes_cli import kanban_sync_client as client
    def send():
        try:
            client._send(live, hub, slug, gateway, mode, result, after_pull=False, before_head=True, seen=evidence)
        except (client.HubError, OSError) as exc:
            client._log.info("kanban sync: %s interleaved push failed (%s)", slug, exc)
    return push_service(send)


def apply_page(conn, changes, *, new_cursor, **kwargs):
    if conn.in_transaction:
        raise RuntimeError("bounded feed apply requires its own writer reservations")
    from hermes_cli import kanban_containment_replica as containment
    # Establish conservative holds for every certified receipt before any prefix
    # can publish readiness. Releases remain held until their event is applied.
    containment.prepare_apply(conn, changes)
    return _apply_page(conn, changes, new_cursor=new_cursor, **kwargs)


def _apply_page(conn, changes, *, new_cursor, **kwargs):
    from hermes_cli import kanban_db_sync as sync
    if conn.in_transaction:
        raise RuntimeError("bounded feed apply requires its own writer reservations")
    # A feed is strictly ordered; snapshots/repair sequences use the atomic API.
    ordered = all(a.seq < b.seq for a, b in pairwise(changes))
    result = sync.ApplyResult(cursor=new_cursor)
    offset, size = 0, MAX_ROWS
    while offset < len(changes) or (not changes and offset == 0):
        service_pushes()
        batch = changes[offset:offset + size]
        if offset + len(batch) < len(changes) and (not ordered or not 0 < batch[-1].seq < new_cursor):
            raise ValueError("feed prefix requires ordered sequence cursors below page next")
        cursor = new_cursor if offset + len(batch) == len(changes) else batch[-1].seq
        token = _LIMIT.set(MAX_TRANSACTION_S)
        try:
            applied = sync.apply_remote_changes(conn, batch, new_cursor=cursor, **kwargs)
        except ApplyBudgetExceeded:
            if size == 1:
                raise
            size = max(1, size // 2)
            _YIELD.wait(WRITER_YIELD_S)
            continue
        finally:
            _LIMIT.reset(token)
        result.applied += applied.applied
        result.skipped += applied.skipped
        result.skipped_echo += applied.skipped_echo
        result.status_changed.update(applied.status_changed)
        offset += len(batch)
        if not batch:
            break
        # SQLite does not promise FIFO writer admission. Give an already-waiting
        # dispatcher a real scheduling window, not an immediate reacquisition.
        _YIELD.wait(WRITER_YIELD_S)
    service_pushes()
    return result
