"""Hold the always-on dashboard's application readiness until prewarms finish (bounded). [daveey]

Every hub core update retires the always-on ``serve`` (code skew) and its supervisor starts a new one. The new
process starts the kanban board, session-list annotation and project-tree prewarms on daemon threads and binds its
port at once. Desktop, which has been polling the dead port and failing fast, attaches on the first answer and fires
its whole refresh burst (sessions lists, project trees, session tasks, the board, ...) into a process whose caches are
all cold. Those requests wait on the prewarms' single-flight reads while ~60 threadpool threads share the GIL with
them, so ~4 s of warm-up CPU took 39 s of wall time on 2026-11-12 (``kanban board prewarm finished ...
elapsed_s=39.274``) and the Desktop's 6 s / 30 s request deadlines expired: a timeout burst on every core update.

uvicorn runs the lifespan startup before accepting app traffic. A headless bootstrap listener may already own
the socket, but returns HTTP503 (including health) until this gate completes. Desktop treats this as not ready and keeps
failing fast for up to 20 s (``ALWAYS_ON_RESTART_WAIT_MS`` in ``apps/desktop/electron/remote-lifecycle.ts``), then
attaches to a warm backend. The wait is capped (at most 6 s, and it ends by 17 s after process start, since boot
alone takes 11-18 s on the hub; see ``ALWAYS_ON_BIND_DEADLINE_S`` for how that fits the window) so a slow warm-up
degrades to today's behaviour (bind with prewarms still running) instead of pushing Desktop onto a second, colder
owned backend.

Only the always-on backend waits: a Desktop-owned ``serve --isolated`` is dialled with a 10 s ready probe and must
become ready promptly (GH-73083).
"""

from __future__ import annotations

import importlib
import logging
import threading
import time
from typing import Callable, Iterable, Optional

log = logging.getLogger(__name__)

# The wait is at most this long...
ALWAYS_ON_WARMUP_BUDGET_S = 6.0
# ...and ends this many seconds after process start, whichever comes first. Boot to bind on hermes-hub measured
# 11.4-18.4 s (median ~14 s) over 30 restarts on 2026-11-12, so a fixed 6 s on top of a slow boot would bind past
# Desktop's 20 s always-on attach window (``ALWAYS_ON_RESTART_WAIT_MS``). That window is not measured from the old
# process's exit: it starts in ``connectAlwaysOn``, after Desktop has noticed the drop and run its SSH preflight.
# Under core's systemd unit (``RestartSec=5``) a capped bind can land 5 + 17 = 22 s after the old process exits, so
# the fit relies on that detection + preflight time, not on slack inside this deadline.
ALWAYS_ON_BIND_DEADLINE_S = 17.0
# A process older than this at the gate is not a fresh boot (or its start time is wrong): use the plain budget.
_MAX_PLAUSIBLE_BOOT_S = 300.0

# Thread names the budget-spent log line translates (the annotation thread reports its current step).
_BOARD_THREAD = "kanban-board-prewarm"
_ANNOTATION_THREAD = "kanban-annotation-prewarm"


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


def process_age_s() -> Optional[float]:
    """Seconds since this process started, or None when unknown or implausible."""
    try:
        import psutil

        age = time.time() - psutil.Process().create_time()
    except Exception:  # health: allow BLE001 -- an unknown age falls back to the plain budget
        return None
    return age if 0 <= age <= _MAX_PLAUSIBLE_BOOT_S else None


def warmup_budget_s(age_s: Optional[float], budget_s: float = ALWAYS_ON_WARMUP_BUDGET_S,
                    deadline_s: float = ALWAYS_ON_BIND_DEADLINE_S) -> float:
    """The wait left: ``budget_s``, cut so the bind lands by ``deadline_s`` after process start (never negative)."""
    if age_s is None:
        return budget_s
    return max(0.0, min(budget_s, deadline_s - age_s))


def running_prewarm_names(threads: Iterable[threading.Thread]) -> list[str]:
    """Name each still-running prewarm (board / session annotation / project tree) for the budget-spent log."""
    names = []
    for thread in threads:
        if not thread.is_alive():
            continue
        if thread.name == _BOARD_THREAD:
            names.append("board")
        elif thread.name == _ANNOTATION_THREAD:
            try:
                from hermes_cli.kanban_annotation_prewarm import boot_phase

                names.append(boot_phase())
            except Exception:  # health: allow BLE001 -- the label is diagnostics only
                names.append("session annotation / project tree")
        else:
            names.append(thread.name)
    return names


def hold_bind_for_prewarm(budget_s: Optional[float] = None) -> Optional[tuple[float, int]]:
    """In the always-on backend only: wait (bounded) for the boot prewarms before the socket binds. Never raises.

    Without an explicit ``budget_s`` the wait is ``ALWAYS_ON_WARMUP_BUDGET_S``, cut to end by
    ``ALWAYS_ON_BIND_DEADLINE_S`` after process start."""
    try:
        from hermes_cli.always_on_dashboard import always_on_enabled

        if not always_on_enabled():
            return None
        threads = boot_prewarm_threads()
        age = process_age_s()
        budget = warmup_budget_s(age) if budget_s is None else budget_s
        elapsed, _ = join_within(threads, budget)
        age_note = "?" if age is None else f"{age:.1f}s"
        # Count and names from one is_alive pass, so a prewarm finishing after the join can't skew the line.
        names = running_prewarm_names(threads)
        running = len(names)
        if running:
            log.info("always-on dashboard: boot prewarm budget spent after %.2fs of %.2fs (process age %s at gate; "
                     "%d thread(s), %d still running: %s); binding", elapsed, budget, age_note, len(threads),
                     running, ", ".join(names))
        else:
            log.info("always-on dashboard: boot prewarm finished after %.2fs of %.2fs (process age %s at gate; "
                     "%d thread(s)); binding", elapsed, budget, age_note, len(threads))
        return elapsed, running
    except Exception:  # health: allow BLE001 -- the gate is an optimisation; binding must never depend on it
        log.warning("always-on dashboard: boot prewarm gate failed; binding now", exc_info=True)
        return None
