"""Unsent local card results keep the pre-termination census UNKNOWN, never zero.

A finished run commits its completion locally and the Board learns of it through
``kanban_sync_outbox``. ``kanban_run_end_lease`` may release the Board lease while
that row is still pending, so an empty process/lease census says nothing about
whether the result reached the Board. Terminating the node then destroys it.

``local_results_state`` reads every account board read-only and answers
``clear`` only when every board was readable and none holds an outbox row (rows are
deleted on acknowledgement, so any row is unsent or unacknowledged). Any pending
row or any unreadable/missing board is not clear. This proves no remote custody of
workspace files or attachment bytes: that stays an INCOMPLETE, separately scoped item.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

CLEAR, PENDING, UNKNOWN = 'clear', 'pending', 'unknown'


@dataclass(frozen=True)
class ResultsState:
    state: str
    pending: int = 0
    reason: str = ''


def _outbox_rows(path: Path) -> int:
    """Outbox rows on one board; 0 when the board has no sync tables. Raises on any read failure."""
    conn = sqlite3.connect(f'{path.resolve(strict=True).as_uri()}?mode=ro', uri=True, timeout=2)
    try:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='kanban_sync_outbox'").fetchone() is None:
            return 0
        return int(conn.execute('SELECT COUNT(*) FROM kanban_sync_outbox').fetchone()[0])
    finally:
        conn.close()


def _sync_running(path: Path) -> int:
    """1 when a sync round holds this board's sync lock, else 0. Probe failure raises (UNKNOWN).

    Takes and immediately drops the same nonblocking flock a round holds, so a clear answer means no
    round is between reading outbox rows and acknowledging them.
    """
    from hermes_cli.kanban_sync_guard import SyncRoundBusy, board_sync_lock
    try:
        with board_sync_lock(path):
            return 0
    except SyncRoundBusy:
        return 1


def local_results_state(boards: list[Path] | None = None) -> ResultsState:
    """Fail closed: unreadable inventory or board is UNKNOWN, a pending row is PENDING."""
    try:
        if boards is None:
            from hermes_cli import worker_account_boards
            boards = worker_account_boards.paths()
        pending = 0
        running = 0
        for path in boards:
            pending += _outbox_rows(Path(path))
            running += _sync_running(Path(path))
    except (OSError, ValueError, sqlite3.Error) as exc:
        return ResultsState(UNKNOWN, reason=f'outbox unreadable: {type(exc).__name__}')
    if running and not pending:
        # A round that popped rows may be mid-acknowledgement; its lock is the only witness.
        return ResultsState(PENDING, running, 'kanban sync round in flight')
    if pending:
        return ResultsState(PENDING, pending, 'unsent local results in kanban_sync_outbox')
    return ResultsState(CLEAR)


def gate_count(count: int | None, boards: list[Path] | None = None) -> int | None:
    """Pass a census count through the gate: a zero without clear results becomes UNKNOWN (None)."""
    if count is None or count != 0:
        return count
    return 0 if local_results_state(boards).state == CLEAR else None
