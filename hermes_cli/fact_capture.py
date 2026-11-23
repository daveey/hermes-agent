"""Guarded, bounded task-content capture for the projection owner. [daveey]

This is a transaction primitive, not a complete TaskFact adapter: hydrate must
select only declared content inputs covered by the task cursor. Operational and
external inputs need their own revisions. Large FULLs are explicitly refused
until the paged bootstrap/replay owner can handle them; never publish a prefix.
"""
from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Iterable, Mapping
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from hermes_cli import kanban_task_change as changes
from hermes_cli.fact_contract import freeze
from hermes_cli.kanban_generation import open_ro
from hermes_cli.kanban_lockguard import shm_identity


class CapturePending(RuntimeError):
    """A bounded page completed; reschedule without publication or cursor advance."""


class CaptureRefused(RuntimeError):
    """Discard this entire capture; the caller retains its last published cursor."""


class CaptureOverflow(CaptureRefused):
    """A full generation needs source-owned pagination."""


class CaptureBudgetExceeded(CaptureRefused):
    """A smaller page may satisfy this transaction's time/byte budget."""


@dataclass(frozen=True)
class CaptureBudget:
    max_keys: int = 2000
    max_seconds: float = 0.020
    max_bytes: int = 4 * 1024 * 1024

    def __post_init__(self):
        if not 1 <= self.max_keys <= 2000 or self.max_seconds <= 0 or self.max_bytes < 1:
            raise ValueError("invalid capture budget")


@dataclass(frozen=True)
class TaskCapture:
    before: changes.Cursor | None
    after: changes.Cursor | None
    full: bool
    ids: tuple[str, ...]
    records: tuple[Mapping, ...]
    observed_at: float
    transaction_seconds: float
    payload_bytes: int = 0
    replay_fence: changes.Cursor | None = None
    replay_position: tuple | None = None
    replay_done: bool = False


def _identity(path: Path):
    stat = path.stat()
    return stat.st_dev, stat.st_ino, shm_identity(path)


def payload_bytes(value):
    """Raw scalar payload bound, not a claim about Python RSS or encoded views."""
    if isinstance(value, Mapping):
        return 8 + sum(payload_bytes(k) + payload_bytes(v) for k, v in value.items())
    if isinstance(value, (tuple, list)):
        return sum(payload_bytes(item) for item in value)
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    if isinstance(value, bytes):
        return len(value)
    if value is None or isinstance(value, (bool, int, float)):
        return 8
    raise TypeError("capture hydrators must return raw SQLite scalars")


def capture_tasks(path: Path, cursor: changes.Cursor | None,
                  hydrate: Callable[..., Iterable[Mapping]], *,
                  budget: CaptureBudget = CaptureBudget(),
                  page_after: str | None = None,
                  replay: bool = False, replay_fence=None, replay_position=None) -> TaskCapture:
    """Capture cursor and raw inputs in one snapshot, closing before return.

    hydrate runs on batches of at most 500 ids; absence is represented by ids
    without a corresponding row. A FULL replaces source membership, whereas a
    delta's missing rows are tombstones. No cursor is installed here: only the
    owner may advance it after successful source+derived publication.
    """
    from hermes_state_guard import ensure_kanban_test_isolation

    path = Path(path).resolve()
    ensure_kanban_test_isolation(path)
    initial = path.stat()
    with closing(open_ro(path, timeout=0)) as conn:
        # SQLite refuses oversized rows before Python allocates their values.
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, budget.max_bytes)
        before = _identity(path)
        if before[:2] != (initial.st_dev, initial.st_ino):
            raise CaptureRefused("source generation changed while opening")
        started = time.monotonic()
        deadline = started + budget.max_seconds
        conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        conn.execute("BEGIN")
        try:
            from hermes_cli import fact_task_replay
            if replay:
                ids, after, replay_fence, replay_position = fact_task_replay.page(
                    conn, cursor, replay_position, replay_fence, budget.max_keys)
                full = False
            elif page_after is not None:
                after = fact_task_replay.coverage(conn, cursor)
                full = True
                ids = tuple(row[0] for row in conn.execute(
                    "SELECT id FROM tasks WHERE id > ? ORDER BY id LIMIT ?",
                    (page_after, budget.max_keys)))
            else:
                dirty, after = changes.changed_since(conn, cursor, limit=budget.max_keys)
                full = dirty is changes.FULL or len(dirty) > budget.max_keys
                if full:
                    ids = tuple(row[0] for row in conn.execute(
                        "SELECT id FROM tasks ORDER BY id LIMIT ?", (budget.max_keys + 1,)))
                else:
                    ids = tuple(sorted(dirty))
            if len(ids) > budget.max_keys:
                raise CaptureOverflow("keys budget exceeded; continuation required")
            records = []
            size = payload_bytes(ids)
            if size > budget.max_bytes:
                raise CaptureBudgetExceeded("capture byte budget exceeded")
            for offset in range(0, len(ids), 500):
                if time.monotonic() >= deadline:
                    raise CaptureBudgetExceeded("transaction time budget exceeded")
                for row in hydrate(conn, ids[offset:offset + 500]):
                    size += payload_bytes(row)
                    if size > budget.max_bytes:
                        raise CaptureBudgetExceeded("capture byte budget exceeded")
                    records.append(row)
            # Recheck after caller hydration, within the SAME snapshot. Ordinary
            # concurrent WAL commits cannot advance this cursor; replacement can.
            _dirty, verified = changes.changed_since(conn, after, limit=budget.max_keys)
            if verified != after or _identity(path) != before:
                raise CaptureRefused("source generation changed during capture")
            if time.monotonic() >= deadline:
                raise CaptureBudgetExceeded("transaction time budget exceeded")
            conn.execute("COMMIT")
            elapsed = time.monotonic() - started
        except sqlite3.OperationalError as exc:
            if getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_INTERRUPT:
                raise CaptureBudgetExceeded("transaction time budget exceeded") from exc
            raise
        finally:
            conn.set_progress_handler(None, 0)
            if conn.in_transaction:
                conn.execute("ROLLBACK")
    # Detach/freeze only after SQLite custody has ended. Hydrators return raw
    # scalars, not lazy generators or SQL handles stored in records.
    return TaskCapture(cursor, after, full, ids, tuple(freeze(row) for row in records),
                       time.time(), elapsed, size, replay_fence, replay_position,
                       replay and len(ids) < budget.max_keys)
