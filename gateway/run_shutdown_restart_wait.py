"""Restart-wait helpers of ``GatewayShutdownMixin``: naming the work a restart waits on, and cancelling a
core update's drain restart when that update gives up. [daveey]

hermes-gateway-launcher (the core-update execution gate) SIGUSR1s its gateway when hermes-sync publishes
``<HERMES_HOME>/core-update/intent``, and hermes-sync removes the intent when the gateway drain outlives
its wait. The deferred restart used to run anyway once the last work unit ended, relaunching the OLD code
(David-MBP 2026-10-29 15:29-15:47: a cron run held the restart 17 min, t_01a189db034f3b2b).
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import suppress
from pathlib import Path
from typing import Optional

logger = logging.getLogger("gateway.run")

# ``_await_active_work_before_restart`` result: the core update behind the restart gave up.
RESTART_CANCELLED = object()


class GatewayRestartWaitMixin:
    """Mixed into ``GatewayShutdownMixin``; methods use the runner's restart/drain state."""

    def _describe_active_work(self) -> list:
        """One dict per in-flight work unit the restart wait is holding for, so an observer
        (``hermes update``, ``hermes gateway status``) can name it instead of printing a bare count.

        ``kind`` ∈ ``chat`` (session turn), ``cron`` (job id + external worker pid when the run was
        handed to a restart-safe scope), ``api`` / ``deferred`` (count only — those sources expose
        no identity). Best-effort: a source that can't be read is omitted, never raises.
        """
        from gateway.run import _AGENT_PENDING_SENTINEL
        now = time.time()
        units: list = []
        for key, state in list(self._sessions_map().items()):
            agent = state.turn.agent
            if agent is None:
                continue
            unit: dict = {"kind": "chat", "session": key, "pid": os.getpid()}
            if state.turn.started_ts:
                unit["elapsed_s"] = round(now - state.turn.started_ts, 1)
            if agent is not _AGENT_PENDING_SENTINEL:
                unit["model"] = getattr(agent, "model", None)
                summary_fn = getattr(agent, "get_activity_summary", None)
                if callable(summary_fn):
                    with suppress(Exception):
                        summary = summary_fn()
                        unit["current_tool"] = summary.get("current_tool")
                        unit["idle_s"] = summary.get("seconds_since_activity")
            units.append(unit)
        with suppress(Exception):
            from cron.scheduler import get_running_job_details, get_wedged_job_ids
            wedged = get_wedged_job_ids()
            for job in get_running_job_details():
                units.append({"kind": "cron", "job_id": job["job_id"], "elapsed_s": job["elapsed_s"],
                              "pid": job["worker_pid"] or os.getpid(), "external": bool(job["worker_pid"]),
                              "wedged": job["job_id"] in wedged,
                              "restart_safe": bool(job.get("restart_safe"))})
        for kind, count in (("api", self._active_api_run_count()), ("deferred", self._active_deferred_agent_worker_count())):
            units.extend({"kind": kind, "pid": os.getpid()} for _ in range(count))
        return units

    def _core_update_intent_path(self) -> Optional[Path]:
        """``<HERMES_HOME>/core-update/intent`` when it exists now, else None. A restart requested
        while it exists is that update's drain request."""
        with suppress(Exception):
            home = os.environ.get("HERMES_HOME")
            if not home:
                from hermes_constants import get_hermes_home
                home = str(get_hermes_home())
            path = Path(home) / "core-update" / "intent"
            if path.exists():
                return path
        return None

    def _update_intent_withdrawn(self, intent: Optional[Path]) -> bool:
        """True (and logged) when the update intent behind this restart is gone: the update gave up,
        so restarting now would only relaunch the code already running. While a launcher-supervised
        gateway child is alive the updater cannot have applied (it needs LOCK_EX on exec.lock)."""
        if intent is None or intent.exists():
            return False
        logger.warning(
            "Restart cancelled: the core update that requested it gave up (%s removed) with %d active "
            "work unit(s) still running; staying up on the current code instead of restarting for an "
            "update that did not apply", intent, self._awaitable_work_count())
        return True

    async def _restart_wait_cancelled(self) -> bool:
        """Run the after-turn wait; True when the restart must be cancelled instead of run.

        Re-checks the intent on every exit path, not only inside the wait loop: it can vanish during
        the poll sleep while the last unit ends (loop exits on count == 0) or at the deadline, and a
        restart then would relaunch old code for an update that gave up. [daveey]
        """
        if await self._await_active_work_before_restart() is RESTART_CANCELLED:
            return True
        return self._update_intent_withdrawn(getattr(self, "_restart_update_intent", None))

    def _cancel_restart(self) -> None:
        """Undo ``request_restart`` before ``stop()``: accept new turns again, report ``running``, and
        let the next update's SIGUSR1 request a fresh restart. API runs keep their informational
        ``shutdown_requested_at`` stamp."""
        self._restart_requested = False
        self._restart_detached = False
        self._restart_via_service = False
        self._restart_update_intent = None
        self._draining = False
        self._restart_task_started = False
        self._scale_to_zero_status("running", "restart cancel: status mark failed")
