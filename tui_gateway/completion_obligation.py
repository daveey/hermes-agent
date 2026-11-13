"""[daveey] Settling a Desktop chat's owed completion (``delivery_obligation`` with ``owner: tui``).

A Desktop-owned notify job's result receipt carries the owed notice (``tools/process_registry_obligations.py``)
from the moment its exit is recorded, so a Desktop backend that dies uncleanly (SIGKILL, OOM: no orderly exit
journal) does not take the only copy with it; the next Desktop backend re-arms it through the re-adopt sink.
This backend removes the block once the notice no longer needs it:

- delivered: the notice turn carrying it started (``start_synthesized_turn``'s commit; its crash marker then
  carries the prompt);
- intended suppression: the agent consumed the output (wait/log/kill; the registry settles those itself),
  background notifications are ``off`` for the chat, or the event formats to no text;
- custody transfer: the orderly-exit journal wrote it into the session's turn marker
  (``exit_job_receipts.journal_session_jobs``), or a core-update pause that stood killed the job and journaled
  the kill there (``update_pause.boundary_pause_hook``), so the next backend reports it from there, not twice.

A managed worker serving a Desktop chat's turn counts as a Desktop-owned backend too (a single-profile worker
inherits the backend's env, ``session_managed_worker._worker_env``). That is intended: the worker is a per-turn
interpreter whose completion queue nobody reads once its turn ends, so the obligation it stamps (its own pid)
is the notice's only custody; the Desktop keeper re-arms it once that worker is gone.

A tui obligation whose chat is never reopened (deleted or archived) is re-armed and wakes that chat on each
Desktop start until ``OBLIGATION_MAX_RECOVERIES`` / ``OBLIGATION_MAX_AGE_SECONDS`` drops it (bounded by design).

Every call is best effort: a failed settle keeps the obligation, and the worst case is one more delivery after
a crash, never a lost notice.
"""

from __future__ import annotations

import logging
from typing import Iterable

logger = logging.getLogger(__name__)


def _settle(pids: Iterable, how: str, *, owed_here_only: bool = False) -> None:
    try:
        from tools.process_registry import process_registry
        settle = process_registry.settle_tui_completion if owed_here_only else process_registry.settle_completion
        for pid in {p for p in pids if isinstance(p, str) and p}:
            settle(pid, how)
    except Exception:  # health: allow BLE001 -- a failed settle keeps the obligation (at least once)
        logger.warning("settling owed Desktop completions failed (%s)", how, exc_info=True)


def settle_started(offered: Iterable) -> None:
    """The notice turn carrying these completions (process ids) started: delivered into the session. Runs on every
    notice-turn start, so a job this backend never owed a notice (``_tui_owed``) costs no receipt read."""
    _settle(offered, "delivered: Desktop notice turn started", owed_here_only=True)


def settle_suppressed(evt: dict, how: str) -> bool:
    """A completion this chat deliberately does not turn into a notice. Returns True (the routing result)."""
    if isinstance(evt, dict) and evt.get("type", "completion") == "completion":
        _settle([evt.get("session_id")], how)
    return True


def settle_journaled(pids: Iterable) -> None:
    """The exit journal or an update pause wrote these jobs into the turn marker: the marker holds the notice."""
    _settle(pids, "journaled into the turn marker")
