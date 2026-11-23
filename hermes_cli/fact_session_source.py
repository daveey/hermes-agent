"""Guarded session capture with listing AND message/counter clocks. [daveey]"""
from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import hermes_state_session_change as listing
import hermes_state_session_content_change as content
from hermes_cli.fact_capture import (
    CaptureBudget, CaptureBudgetExceeded, CaptureOverflow, CapturePending,
    CaptureRefused, _identity, payload_bytes,
)
from hermes_cli.fact_bootstrap import PagedBootstrap
from hermes_cli.fact_contract import freeze
from hermes_cli.fact_source_watch import NativeSourceWatch


@dataclass(frozen=True)
class SessionCursor:
    listing: listing.Cursor
    content: content.Cursor


@dataclass(frozen=True)
class SessionCapture:
    before: SessionCursor | None
    after: SessionCursor | None
    full: bool
    ids: tuple[str, ...]
    records: tuple
    observed_at: float
    transaction_seconds: float
    payload_bytes: int
    replay_fence: SessionCursor | None = None
    replay_position: tuple | None = None
    replay_done: bool = False


def _changed(conn, cursor, budget):
    a, left = listing.changed_since(conn, cursor.listing if cursor else None, limit=budget.max_keys)
    b, right = content.changed_since(conn, cursor.content if cursor else None, limit=budget.max_keys)
    after = SessionCursor(left, right) if left is not None and right is not None else None
    if a is listing.FULL or b is content.FULL:
        return listing.FULL, after
    ids = a | b
    return (listing.FULL if len(ids) > budget.max_keys else ids), after


def capture_sessions(path, cursor, hydrate, *, budget=CaptureBudget(), page_after=None,
                     replay=False, replay_fence=None, replay_position=None):
    """No transcript preload: the host selects bounded projection inputs by id."""
    from hermes_state import _connect_tracked_db
    from hermes_state_holders import read_only_db_uri

    path = Path(path).resolve()
    initial = path.stat()
    with closing(_connect_tracked_db(read_only_db_uri(path), tracking_path=path,
                                     uri=True, isolation_level=None, timeout=0)) as conn:
        conn.execute("PRAGMA query_only=ON")
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, budget.max_bytes)
        before = _identity(path)
        if before[:2] != (initial.st_dev, initial.st_ino):
            raise CaptureRefused("source generation changed while opening")
        started = time.monotonic()
        deadline = started + budget.max_seconds
        conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        conn.execute("BEGIN")
        try:
            from hermes_cli import fact_session_replay
            replay_done = False
            if replay:
                ids, after, replay_fence, replay_position, replay_done = fact_session_replay.page(
                    conn, cursor, replay_position, replay_fence, budget.max_keys)
                full = False
            elif page_after is not None:
                after = fact_session_replay.coverage(conn, cursor)
                full = True
                ids = tuple(row[0] for row in conn.execute(
                    "SELECT id FROM sessions WHERE id>? ORDER BY id LIMIT ?", (page_after, budget.max_keys)))
            else:
                dirty, after = _changed(conn, cursor, budget)
                full = dirty is listing.FULL
                ids = (tuple(row[0] for row in conn.execute("SELECT id FROM sessions ORDER BY id LIMIT ?",
                                                          (budget.max_keys + 1,)))
                       if full else tuple(sorted(dirty)))
            if len(ids) > budget.max_keys:
                raise CaptureOverflow("keys budget exceeded; continuation required")
            size = payload_bytes(ids)
            if size > budget.max_bytes:
                raise CaptureBudgetExceeded("capture byte budget exceeded")
            records = []
            for offset in range(0, len(ids), 500):
                if time.monotonic() >= deadline:
                    raise CaptureBudgetExceeded("transaction time budget exceeded")
                for row in hydrate(conn, ids[offset:offset + 500]):
                    size += payload_bytes(row)
                    if size > budget.max_bytes:
                        raise CaptureBudgetExceeded("capture byte budget exceeded")
                    records.append(row)
            _dirty, verified = _changed(conn, after, budget)
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
    return SessionCapture(cursor, after, full, ids, tuple(freeze(row) for row in records),
                          time.time(), elapsed, size, replay_fence, replay_position, replay_done)


class SessionContentSource:
    def __init__(self, source, path, hydrate):
        self.source = source
        self.path = Path(path).absolute()
        self._hydrate = hydrate
        self._watch = None
        self._bootstrap = None

    def watch(self, wake):
        if self._watch is not None:
            raise RuntimeError("source already registered")
        self._watch = NativeSourceWatch((self.path,), wake)
        return self._watch

    def capture(self, cursor, budget):
        if self._watch is not None:
            self._watch.check()
        if (self._bootstrap is not None and self._bootstrap.ready is not None
                and cursor == self._bootstrap.ready.after):
            self._bootstrap = None
        if self._bootstrap is not None:
            result = self._bootstrap.capture(cursor, budget)
        else:
            try:
                result = capture_sessions(self.path, cursor, self._hydrate, budget=budget)
            except (CaptureOverflow, CaptureBudgetExceeded):
                self._bootstrap = PagedBootstrap(self.path, self._hydrate, capture=capture_sessions)
                raise CapturePending("session bootstrap required") from None
        if self._watch is not None:
            self._watch.check()
        return result
