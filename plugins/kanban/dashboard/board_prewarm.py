"""Boot-time read of the active, default and blocked boards, so Desktop's first cold-start poll finds
a snapshot (or joins this read) instead of paying connect + first-init integrity probe + the
full read under the burst's GIL contention. The probe still runs in ``kbc.connect`` before
anything is written; only the thread it runs on changes."""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional

from agent.memory_provider import spawn_context_thread
from hermes_cli import kanban_db

log = logging.getLogger(__name__)


def _warm_names(api):
    yield kanban_db.get_current_board()
    yield kanban_db.DEFAULT_BOARD
    # Discover only after the selected board has warmed. A failed directory
    # scan must not discard the guaranteed active/default warm-up.
    try:
        for board in kanban_db.list_boards():
            if api._board_counts(board["slug"]).get("blocked", 0):
                yield board["slug"]
    except Exception:
        log.warning("kanban board prewarm discovery failed", exc_info=True)


def prewarm_boards(api) -> None:
    """``api`` is the kanban plugin_api module (key and read come from the same code as GET /board)."""
    from plugins.kanban.dashboard.board_snapshot import file_sig, prewarm

    # Desktop's blocked-task loader reads these boards too, not only the selected
    # board. Counts are query-only: discovery must not initialize every store.
    seen = set()
    for name in _warm_names(api):
        started = time.monotonic()
        try:
            resolved = api._resolve_board(name)
            path = str(kanban_db.kanban_db_path(board=resolved))
            key = (path, None, False, None, None, int(api._BOARD_DONE_LIMIT))
            if key in seen:
                continue
            seen.add(key)
            prewarm(lambda r=resolved: api._get_board_uncached(None, False, r, None, None, api._BOARD_DONE_LIMIT),
                    key, file_sig(path))
            log.info("kanban board prewarm finished board=%s elapsed_s=%.3f", name, time.monotonic() - started)
        except Exception:
            log.warning("kanban board prewarm failed for %r", name, exc_info=True)


def start_board_prewarm(api) -> Optional[threading.Thread]:
    """Run ``prewarm_boards`` on a daemon thread (not under the test harness)."""
    if os.environ.get("HERMES_TEST_ISOLATION"):
        return None
    thread = spawn_context_thread(lambda: prewarm_boards(api), name="kanban-board-prewarm")
    thread.start()
    return thread
