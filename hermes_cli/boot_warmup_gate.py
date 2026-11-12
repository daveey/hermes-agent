"""Hold the always-on dashboard's socket until its boot prewarms finish (bounded). [daveey]

Every hub core update retires the always-on ``serve`` (code skew) and its supervisor starts a new one. The new
process starts the kanban board, session-list annotation and project-tree prewarms on daemon threads and binds its
port at once. Desktop, which has been polling the dead port and failing fast, attaches on the first answer and fires
its whole refresh burst (sessions lists, project trees, session tasks, the board, ...) into a process whose caches are
all cold. Those requests wait on the prewarms' single-flight reads while ~60 threadpool threads share the GIL with
them, so ~4 s of warm-up CPU took 39 s of wall time on 2026-11-12 (``kanban board prewarm finished ...
elapsed_s=39.274``) and the Desktop's 6 s / 30 s request deadlines expired: a timeout burst on every core update.

uvicorn runs the lifespan startup before it binds, so waiting here keeps the port closed while the prewarms run
uncontended. Desktop's always-on attach treats a closed port as "waiting for its supervisor restart" and keeps
failing fast for up to 20 s (``ALWAYS_ON_RESTART_WAIT_MS`` in ``apps/desktop/electron/remote-lifecycle.ts``), then
attaches to a warm backend. The wait is bounded well inside that window so a slow warm-up degrades to today's
behaviour (bind with prewarms still running) instead of pushing Desktop onto a second, colder owned backend.

Only the always-on backend waits: a Desktop-owned ``serve --isolated`` is dialled with a 10 s ready probe and must
bind at once (GH-73083).
"""

from __future__ import annotations

import importlib
import logging
import threading
import time
from typing import Callable, Iterable, Optional

log = logging.getLogger(__name__)

# Boot to bind is ~10-12 s on hermes-hub; with Desktop's 20 s restart wait this leaves several seconds of slack.
ALWAYS_ON_WARMUP_BUDGET_S = 6.0


def join_within(threads: Iterable[Optional[threading.Thread]], budget_s: float, *,
                clock: Callable[[], float] = time.monotonic) -> tuple[float, int]:
    """Join each thread inside one shared ``budget_s``; return (elapsed seconds, threads still running)."""
    started = clock()
    pending = [t for t in threads if t is not None]
    for thread in pending:
        remaining = budget_s - (clock() - started)
        if remaining <= 0:
            break
        thread.join(remaining)
    return clock() - started, sum(1 for t in pending if t.is_alive())


def boot_prewarm_threads() -> list[threading.Thread]:
    """The boot prewarm threads this process started (annotation + project tree, kanban boards)."""
    threads: list[threading.Thread] = []
    for module, attr in (("hermes_cli.kanban_annotation_prewarm", "boot_thread"),
                         ("plugins.kanban.dashboard.board_prewarm", "boot_thread")):
        try:
            thread = getattr(importlib.import_module(module), attr)()
        except Exception:  # health: allow BLE001 -- a missing prewarm is simply not waited for
            log.debug("boot prewarm %s unavailable", module, exc_info=True)
            continue
        if thread is not None:
            threads.append(thread)
    return threads


def hold_bind_for_prewarm(budget_s: Optional[float] = None) -> Optional[tuple[float, int]]:
    """In the always-on backend only: wait (bounded) for the boot prewarms before the socket binds. Never raises."""
    try:
        from hermes_cli.always_on_dashboard import always_on_enabled

        if not always_on_enabled():
            return None
        threads = boot_prewarm_threads()
        elapsed, running = join_within(threads, ALWAYS_ON_WARMUP_BUDGET_S if budget_s is None else budget_s)
        log.info("always-on dashboard: boot prewarm %s after %.2fs (%d thread(s), %d still running); binding",
                 "finished" if not running else "budget spent", elapsed, len(threads), running)
        return elapsed, running
    except Exception:  # health: allow BLE001 -- the gate is an optimisation; binding must never depend on it
        log.warning("always-on dashboard: boot prewarm gate failed; binding now", exc_info=True)
        return None
