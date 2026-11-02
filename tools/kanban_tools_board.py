"""Board contexts for Kanban tools, retaining dispatcher isolation. [daveey]"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Optional


class BoardPinnedError(ValueError):
    """The worker's explicit tool target disagrees with its pinned database."""

    code = "board_pinned"

    def __init__(self, requested: str, pinned: str):
        self.tool_error_fields = {"requested_board": requested, "pinned_board": pinned}
        super().__init__(
            f"Board {requested!r} refused: this worker is pinned to board {pinned!r} "
            "and cannot reach other boards. Omit board or use its pinned board. Nothing changed."
        )


def _check_explicit_worker_board(board: Optional[str]) -> None:
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_sync import board_slug_for_path

    pin = os.environ.get("HERMES_KANBAN_DB", "").strip()
    if board is None or not pin or not os.environ.get("HERMES_KANBAN_TASK"):
        return
    requested = kb._normalize_board_slug(board)
    if requested is None:
        return
    # Infer from the actual DB pin, not a potentially stale current-board hint.
    # Keep the lexical board name: resolving symlinks can erase boards/<slug>.
    pinned = board_slug_for_path(pin)
    if requested != pinned:
        raise BoardPinnedError(requested, pinned)


@contextmanager
def tool_board(board: Optional[str], *, quiet_close: bool = False):
    """Open the requested board, refusing silent worker-pin redirection."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    _check_explicit_worker_board(board)
    conn = kbc.connect(board=board)
    try:
        yield kb, conn
    finally:
        try:
            conn.close()
        except Exception:
            if not quiet_close:
                raise
