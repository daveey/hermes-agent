"""Re-adoption of orphaned background processes by the Desktop/TUI backend. [daveey]

Every Hermes process on a host writes the same ``processes.json``. When a Desktop
backend restarts, the background jobs its chats started live on as orphans (the
writer pid is gone). Only the messaging gateway used to call
``recover_from_checkpoint``, so nothing supervised a Desktop chat's
``notify_on_complete`` job again and its exit never reached the chat.

Ownership is read from the entry's routing fields: a messaging-gateway session key
is ``<ns>:<platform>:...`` (always has a colon) or the entry carries a watcher
platform. A Desktop/TUI/CLI session key is a bare state.db session id. The
messaging gateway adopts only its own (and legacy keyless) entries, and the TUI
backend adopts only bare-id entries, so the two never fight over one child and the
gateway's shutdown ``kill_all`` cannot kill a Desktop chat's job.

A re-adopted process has no pipe, so its exit status is never collected. When the
PID disappears, a ``completion`` with ``completion_reason="unknown"`` is queued
for the owning session ("exited (status unknown — re-adopted after restart)").
The Desktop host (tui_gateway ``_readopted_exit_deliver``) parks the event for the
chat's own poller and, when the chat is not open in this backend, resumes it in-process
(like the startup-resume sweep) so the exit runs a turn without the user opening it.
Only a Desktop-owned backend adopts: a ``hermes dashboard`` has no chats to deliver to.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

logger = logging.getLogger("tools.process_registry")

OWNER_GATEWAY = "gateway"
OWNER_TUI = "tui"
UNKNOWN_EXIT_REASON = "unknown"
UNKNOWN_EXIT_STATUS = "exited (status unknown — re-adopted after restart)"
# A sandbox job whose watcher could not be re-attached after a restart (sandbox gone, no handle). [daveey]
WATCHER_LOST_REASON = "watcher_lost"
WATCHER_LOST_STATUS = ("watcher lost at restart — status unknown (the sandbox could not be re-attached); "
                       "check the process yourself")
DETACHED_SWEEP_SECONDS = 30.0
_MAX_PARKED_EVENTS = 256


def checkpoint_entry_owner(entry: dict[str, Any]) -> str:
    """``gateway``, ``tui`` or ``""`` (no session key: legacy/unknown) for a checkpoint entry."""
    key = str(entry.get("session_key") or "")
    if entry.get("watcher_platform") or ":" in key:
        return OWNER_GATEWAY
    return OWNER_TUI if key else ""


def owner_accepts(owner: Optional[str], entry: dict[str, Any]) -> bool:
    """Whether a recovering process of kind *owner* may adopt *entry* (None = adopt any)."""
    if owner is None:
        return True
    kind = checkpoint_entry_owner(entry)
    if owner == OWNER_GATEWAY:
        return kind in (OWNER_GATEWAY, "")
    return kind == owner


class ProcessReadoptMixin:
    """Detached-PID sweep, unknown-exit completions and the parked-event box."""

    # Supplied by ProcessRegistry.
    _lock: Any
    _running: dict
    _finished: dict
    pending_watchers: list
    completion_queue: Any
    _refresh_detached_session: Callable[[Any], Any]

    _detached_sweep_thread: Optional[threading.Thread] = None
    _parked_events: Optional[list] = None
    # Desktop host delivery for an unknown exit (tui_gateway ``_readopted_exit_deliver``): park it for the owner
    # chat and wake that chat when it is not open. None = the bare completion queue (CLI/tests). [daveey]
    on_unknown_exit: Optional[Callable[[dict[str, Any]], Any]] = None

    # ----- unknown-exit completion -----

    def _unknown_exit_event(self, session) -> dict[str, Any]:
        lost = getattr(session, "completion_reason", "") == WATCHER_LOST_REASON
        return {
            "type": "completion",
            "session_id": session.id,
            "session_key": session.session_key,
            "task_id": session.task_id,
            "owner_task_id": session.owner_task_id,
            "command": session.command,
            "exit_code": None,
            "completion_reason": WATCHER_LOST_REASON if lost else UNKNOWN_EXIT_REASON,
            "termination_source": session.termination_source if lost else "readopted_after_restart",
            "output": "",
            "started_at": session.started_at,
            # The chat may not be open in this backend yet: park instead of dropping.
            "park_if_unowned": True,
        }

    def _deliver_readopted_event(self, event: dict[str, Any]) -> None:
        """Hand an event for a re-adopted job to the host's sink (Desktop: park + wake the chat), else the queue."""
        sink = self.on_unknown_exit
        if sink is None:
            self.completion_queue.put(event)
            return
        try:
            sink(event)
        except Exception:
            logger.warning("unknown-exit delivery hook failed; queueing %s instead", event.get("session_id"),
                           exc_info=True)
            self.completion_queue.put(event)

    def _prune_uncollected_detached(self, session):
        """Drop a recovered entry whose PID is gone, without inventing an exit.

        An owned systemd scope stays reachable so kill can still reap it. A
        scope-less entry is removed: poll/list must not report a collected exit.
        A gateway re-adoption with a completion watcher keeps the session as an
        ``unknown`` exit instead, so that watcher wakes the owning session. [daveey]
        """
        keep_for_watcher = session._lost_exit_via_watcher and session.notify_on_complete
        with session._lock:
            session.exited = True
            if keep_for_watcher:
                session.exit_code = None
                session.completion_reason = UNKNOWN_EXIT_REASON
                session.termination_source = "readopted_after_restart"
        with self._lock:
            was_running = self._running.pop(session.id, None) is not None
            if session.systemd_unit or keep_for_watcher:
                if keep_for_watcher and was_running:
                    session.exited_at = time.time()
                self._finished[session.id] = session
            else:
                self._finished.pop(session.id, None)
        self._write_checkpoint()
        if was_running and session._report_lost_exit and session.notify_on_complete:
            self._queue_unknown_exit(session)
        elif was_running and keep_for_watcher:
            logger.info("Re-adopted process %s (pid=%s) exited; status unknown — the gateway watcher wakes %s",
                        session.id, session.pid, session.session_key or "-")
        session._completion_event.set()
        return session if (session.systemd_unit or keep_for_watcher) else None

    def _put_completion(self, session, notification: dict[str, Any]) -> None:
        """Queue a completion; a re-attached job owned by a Desktop/TUI chat goes through the re-adopt
        sink instead (that chat may not be open in this backend). [daveey]"""
        if session._completion_via_readopt_sink:
            notification["park_if_unowned"] = True
            self._deliver_readopted_event(notification)
            return
        self.completion_queue.put(notification)

    def _queue_unknown_exit(self, session) -> None:
        from tools.process_registry import _redact_process_result
        event = self._unknown_exit_event(session)
        _redact_process_result(event)
        self._deliver_readopted_event(event)
        logger.info("Re-adopted process %s (pid=%s) %s — notifying %s", session.id, session.pid,
                    "watcher lost" if event["completion_reason"] == WATCHER_LOST_REASON
                    else "exited; status unknown", session.session_key)

    def _deliver_unwatched_exit(self, session, owner: Optional[str], reason: str, source: str) -> str:
        """Tell the owning session that a job's outcome was never observed (it exited while no host
        watched it, or its sandbox watcher could not be re-attached). Every owner type gets a wake:
        a Desktop/TUI chat through the re-adopt sink, a messaging-gateway session through a recovered
        process watcher (which reads this exited session), a CLI/managed worker through the queue.
        Returns how it was delivered (``sink`` | ``watcher`` | ``queue``), or ``""`` when the job asked
        for no notification. [daveey]"""
        with session._lock:
            session.exited = True
            session.exit_code = None
            session.completion_reason = reason
            session.termination_source = source
        session.exited_at = time.time()
        if not session.notify_on_complete:
            return ""
        if owner != OWNER_TUI and session.watcher_interval > 0:
            with self._lock:
                self._finished[session.id] = session
            session._completion_event.set()
            self.pending_watchers.append(self._recovered_watcher(session))
            return "watcher"
        if owner == OWNER_GATEWAY:
            return ""  # no watcher route recorded: the gateway has no consumer for a bare queue event
        self._queue_unknown_exit(session)
        return "sink" if owner == OWNER_TUI and self.on_unknown_exit is not None else "queue"

    @staticmethod
    def _recovered_watcher(session) -> dict[str, Any]:
        from tools.process_registry import _WATCHER_ROUTE_KEYS
        return {
            "session_id": session.id,
            "check_interval": session.watcher_interval,
            "session_key": session.session_key,
            **{key: getattr(session, f"watcher_{key}") for key in _WATCHER_ROUTE_KEYS},
            "notify_on_complete": session.notify_on_complete,
            "parent_session_id": session.parent_session_id,
        }

    # ----- parked events (owner chat not open in this backend) -----

    def park_unowned_event(self, event: dict[str, Any]) -> None:
        with self._lock:
            if self._parked_events is None:
                self._parked_events = []
            self._parked_events.append(event)
            del self._parked_events[:-_MAX_PARKED_EVENTS]
        from tui_gateway.notification_wait import wake
        wake(self.completion_queue)

    def has_parked_events(self) -> bool:
        """Empty is a lock-protected proof; a producer wakes queue waiters on publication."""
        with self._lock:
            return bool(self._parked_events)

    def take_parked_events(self, owns: Callable[[dict[str, Any]], bool]) -> list[dict[str, Any]]:
        """Remove and return the parked events *owns* claims. ``owns`` runs outside the lock."""
        with self._lock:
            parked = list(self._parked_events or ())
        if not parked:
            return []
        taken = [evt for evt in parked if _safe_owns(owns, evt)]
        if taken:
            with self._lock:
                ids = {id(evt) for evt in taken}
                self._parked_events = [evt for evt in (self._parked_events or ()) if id(evt) not in ids]
        return taken

    # ----- detached-PID sweep -----

    def sweep_detached_once(self) -> int:
        """Refresh every running detached host session; returns how many are still running."""
        with self._lock:
            sessions = [s for s in self._running.values() if s.detached and s.pid_scope == "host"]
        alive = 0
        for session in sessions:
            try:
                refreshed = self._refresh_detached_session(session)
            except Exception:
                logger.debug("Detached sweep failed for %s", session.id, exc_info=True)
                continue
            alive += bool(refreshed is not None and not refreshed.exited)
        return alive

    def start_detached_sweep(self, interval: float = DETACHED_SWEEP_SECONDS) -> bool:
        """Start the low-rate sweep thread (idempotent). It ends once no detached session is left."""
        with self._lock:
            thread = self._detached_sweep_thread
            if thread is not None and thread.is_alive():
                return False
            thread = threading.Thread(target=self._detached_sweep_loop, args=(interval,),
                                      name="process-detached-sweep", daemon=True)
            self._detached_sweep_thread = thread
        thread.start()
        return True

    def _detached_sweep_loop(self, interval: float) -> None:
        while True:
            time.sleep(interval)
            if not self.sweep_detached_once():
                with self._lock:
                    if not any(s.detached and not s.exited for s in self._running.values()):
                        self._detached_sweep_thread = None
                        return


CHECKPOINT_KEEPER_SECONDS = 30.0
_keeper_lock = threading.Lock()
_keeper_thread: Optional[threading.Thread] = None


def start_checkpoint_keeper(registry=None, interval: float = CHECKPOINT_KEEPER_SECONDS) -> bool:
    """Desktop backend: every *interval* s, put back any live entry another writer dropped from
    processes.json (``reassert_checkpoint``). Idempotent; one file read per tick. [daveey]"""
    global _keeper_thread
    if registry is None:
        from tools.process_registry import process_registry as registry

    def _loop() -> None:
        while True:
            time.sleep(interval)
            try:
                registry.reassert_checkpoint()
            except Exception:
                logger.debug("checkpoint keeper tick failed", exc_info=True)

    with _keeper_lock:
        if _keeper_thread is not None and _keeper_thread.is_alive():
            return False
        _keeper_thread = threading.Thread(target=_loop, name="process-checkpoint-keeper", daemon=True)
        _keeper_thread.start()
    return True


def _safe_owns(owns: Callable[[dict[str, Any]], bool], evt: dict[str, Any]) -> bool:
    try:
        return bool(owns(evt))
    except Exception:
        return False


def recover_tui_orphaned_processes(registry=None, *, sweep_interval: float = DETACHED_SWEEP_SECONDS) -> int:
    """Desktop/TUI backend startup: adopt this host's orphaned Desktop-chat processes and
    supervise them until they exit. Never raises."""
    # Only the backend Desktop spawned hosts chats. A launchd ``hermes dashboard`` runs the same web_server
    # lifespan and, booting first, won the checkpoint lock: the exit then landed in a process with no chat and
    # no poller and was lost (18:31 PDT 2026-10-14, session 20261014_142820_ff330a). [daveey]
    from hermes_cli.process_identity import is_desktop_owned_backend
    if not is_desktop_owned_backend():
        logger.debug("not a Desktop-owned backend: leaving orphaned chat processes for the Desktop backend")
        return 0
    if registry is None:
        from tools.process_registry import process_registry as registry
    try:
        recovered = registry.recover_from_checkpoint(owner=OWNER_TUI)
    except Exception:
        logger.warning("TUI process checkpoint recovery failed", exc_info=True)
        return 0
    if recovered:
        logger.info("Re-adopted %d orphaned background process(es) after restart", recovered)
        registry.start_detached_sweep(sweep_interval)
    return recovered


def start_tui_orphan_recovery(registry=None) -> threading.Thread:
    """Run :func:`recover_tui_orphaned_processes` off the startup path (it probes PIDs and
    takes the checkpoint file lock)."""
    thread = threading.Thread(target=recover_tui_orphaned_processes, args=(registry,),
                              name="process-orphan-readopt", daemon=True)
    thread.start()
    return thread
