"""Restart-wait helpers of ``GatewayShutdownMixin``: naming the work a restart waits on, and cancelling a
core update's drain restart when that update gives up. [daveey]

hermes-gateway-launcher (the core-update execution gate) SIGUSR1s its gateway when hermes-sync publishes
``<HERMES_HOME>/core-update/intent``, and hermes-sync removes the intent when the gateway drain outlives
its wait. The deferred restart used to run anyway once the last work unit ended, relaunching the OLD code
(David-MBP 2026-10-29 15:29-15:47: a cron run held the restart 17 min, t_01a189db034f3b2b).
"""

from __future__ import annotations

import json
import logging
import os
import time
from contextlib import suppress
from pathlib import Path
from typing import Optional

from gateway.session_runtime_workers import cron_turn_count, kanban_managed_count

# Only the update's post-grace Kanban wait: human turns retain their normal budget.
CORE_UPDATE_KANBAN_WAIT_S = 15.0
CORE_UPDATE_KANBAN_STOP_LEASH_S = 40.0  # 15 wait + 40 stop + 5 poll allowance = 60s admission
# Under an unfenced checkpoint intent that cuts cron (``_intent_is_checkpoint``) hermes-sync's instant drainer has already spent its own wait
# on this work and decided to apply over it: the cards are preserved by the checkpoint and the cron runs are cut.
# hermes-hub 2026-11-13: every instant swap sat the full 15 s here on a managed card that then restarted anyway
# (journal 21:06:18 -> 21:06:33). A short settle only; the bounds above stay the upper limit.
CORE_UPDATE_CHECKPOINT_KANBAN_WAIT_S = 2.0

logger = logging.getLogger("gateway.run")

# ``_await_active_work_before_restart`` result: the core update behind the restart gave up.
RESTART_CANCELLED = object()


class GatewayRestartWaitMixin:
    """Mixed into ``GatewayShutdownMixin``; methods use the runner's restart/drain state."""

    def _wedged_agent_count(self) -> int:
        """Work units the restart wait may skip: chat agents idle past ``agent.gateway_timeout`` and
        cron runs older than the scheduler's stale-inflight allowance (#115469).

        API work has no activity clock and pending sentinels are brand-new, so neither counts.
        """
        return self._wedged_chat_agent_count() + self._wedged_cron_job_count()

    def _kanban_restart_wait_state(self, intent, now, since):
        """15 monotonic seconds with ONLY confirmed cards left, only for an update's restart.

        Unknown claims/mutations/managed human turns keep their original wait. Reset if other
        work appears. Withdrawing the intent is checked by the caller before this cap.

        An intent marked ``"cron_cut": true`` (hermes-sync applied past its cron bound,
        t_01a18dbde9f8b2f0) also puts awaitable cron runs under the cap: the update was decided over
        them, and waiting up to restart_after_turn_timeout for them would land it past the drain's target.
        Each cut run is logged with its job id and start time; its next tick reruns it.
        """
        kanban = kanban_managed_count(self) if intent is not None else 0
        cron = self._cut_cron_count(intent)
        if kanban + cron <= 0 or self._awaitable_work_count() != kanban + cron:
            return None, False
        since = now if since is None else since
        wait_s = CORE_UPDATE_CHECKPOINT_KANBAN_WAIT_S if self._intent_is_checkpoint(intent) else CORE_UPDATE_KANBAN_WAIT_S
        expired = now - since >= wait_s
        if expired:
            # The after-turn grace is spent. Do not start another full graceful drain in stop().
            self._core_update_kanban_cap_spent = True
            logger.warning(
                "Core update %s wait capped at %.0fs with %d managed card(s) and %d cron run(s); "
                "proceeding to restart drain (interrupted attempts requeued uncounted)",
                "Kanban/cron-only" if cron else "Kanban-only", wait_s, kanban, cron,
            )
            if cron:
                self._log_cut_cron_runs()
        return since, expired

    def _core_update_kanban_snapshot_stop_leash(self) -> None:
        """Fix, once at stop() entry, the short stop-relative leash for a spent Kanban update grace.

        A fixed number (callers already subtract their own stop elapsed), snapshotted so work or
        intent changing mid-stop cannot flip the budget after the watchdog was armed. Human or
        unknown work, a withdrawn intent or an ordinary restart keep the normal budgets.
        """
        self._stop_kanban_leash_s = None
        intent = getattr(self, "_restart_update_intent", None)
        if not getattr(self, "_core_update_kanban_cap_spent", False) or not self._restart_requested:
            return
        if intent is None or not intent.exists():
            return
        # Under a "cron_cut" intent the cron runs were cut with the cards (t_01a18dbde9f8b2f0).
        cron = self._cut_cron_count(intent)
        if self._awaitable_work_count() > kanban_managed_count(self) + cron:
            return
        self._stop_kanban_leash_s = CORE_UPDATE_KANBAN_STOP_LEASH_S

    def _cut_cron_count(self, intent: Optional[Path]) -> int:
        """Awaitable work a ``cron_cut`` intent cuts: the awaitable cron runs plus the agent turns
        those runs execute on this owner (``cron_turn_count``), which the total counts again as
        turns; 0 without such an intent."""
        if not self._intent_cuts_cron(intent):
            return 0
        return self._restart_wait_cron_counts()["awaitable"] + cron_turn_count(self)

    def _intent_cuts_cron(self, intent: Optional[Path]) -> bool:
        """The update intent says cron runs in flight are cut, not awaited (``"cron_cut": true``). Read per
        intent (re-read each poll: tiny file); a missing or unreadable intent never cuts."""
        if intent is None:
            return False
        with suppress(Exception):
            data = json.loads(Path(intent).read_text(encoding="utf-8-sig"))
            return isinstance(data, dict) and data.get("cron_cut") is True
        return False

    def _intent_is_checkpoint(self, intent: Optional[Path]) -> bool:
        """The update intent is an unfenced instant checkpoint that cuts cron: the drainer already waited and
        applies over the work left. Re-read each poll like ``_intent_cuts_cron``; missing or unreadable is False,
        which keeps the full wait."""
        if intent is None:
            return False
        with suppress(Exception):
            data = json.loads(Path(intent).read_text(encoding="utf-8-sig"))
            # The same shape gateway.restart.checkpoint_cron_grace_spent requires: an unfenced instant checkpoint
            # that also cuts cron. A fenced (Mac) intent or a checkpoint without the cron cut keeps the full wait.
            return (isinstance(data, dict) and data.get("kind") == "unfenced"
                    and data.get("checkpoint") is True and data.get("cron_cut") is True)
        return False

    def _log_cut_cron_runs(self) -> None:
        """One line per cron run the update's restart cuts: job id and start time."""
        with suppress(Exception):
            from cron.scheduler import get_running_job_details
            now = time.time()
            for job in get_running_job_details():
                if job.get("restart_safe"):
                    continue  # a scoped worker outlives the restart; it is not cut
                elapsed = job.get("elapsed_s")
                started = (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - elapsed))
                           if isinstance(elapsed, (int, float)) else "unknown")
                logger.warning("Core update restart cutting cron run %s (started %s, %ss ago); its next tick "
                               "reruns it", job.get("job_id"), started,
                               round(elapsed) if isinstance(elapsed, (int, float)) else "?")

    def _restart_cap_state_text(self) -> str:
        """Log suffix naming whether this restart carries a core-update intent (the Kanban-only cap)."""
        if getattr(self, "_restart_update_intent", None) is not None:
            return f"core-update intent present: Kanban-only cap {CORE_UPDATE_KANBAN_WAIT_S:.0f}s armed"
        return "no core-update intent: ordinary restart, Kanban-only cap not armed"

    def _describe_active_work(self) -> list:
        """One dict per in-flight work unit the restart wait is holding for, so an observer
        (``hermes update``, ``hermes gateway status``) can name it instead of printing a bare count.

        ``kind`` ∈ ``chat`` (session turn), ``cron`` (job id + external worker pid when the run was
        handed to a restart-safe scope), ``managed`` (session + worker pid), ``api`` / ``deferred``
        (count only). Best-effort: a source that can't be read is omitted, never raises.
        """
        from gateway.run import _AGENT_PENDING_SENTINEL
        now = time.time()
        units: list = []
        for key, state in list(self._sessions_map().items()):
            agent = state.turn.agent
            if agent is None:
                continue
            unit: dict = {"kind": "chat", "session": key, "pid": os.getpid(), "update_pausable": False}
            if state.turn.started_ts:
                unit["elapsed_s"] = round(now - state.turn.started_ts, 1)
            if agent is not _AGENT_PENDING_SENTINEL:
                unit["model"] = getattr(agent, "model", None)
                # [daveey] A turn under the core-update pause hook parks on a durable checkpoint when an update
                # requests a pause, then auto-continues: the updater pauses it instead of waiting for a gap.
                from gateway.session_update_pause_hook import update_pausable
                unit["update_pausable"] = update_pausable(agent)
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
        from gateway.session_runtime_workers import uncounted_runtime_work_units
        units.extend(uncounted_runtime_work_units(self, os.getpid()))
        for kind, count in (("api", self._active_api_run_count()),
                            ("deferred", self._active_deferred_agent_worker_count())):
            units.extend({"kind": kind, "pid": os.getpid()} for _ in range(count))
        return units

    def _describe_parked_work(self) -> list:
        """Sessions parked on an update checkpoint: unfinished and resumed after the update, never
        counted by ``_active_work_count`` and never listed in ``_describe_active_work``."""
        from gateway.session_runtime_workers import parked_work_units
        with suppress(Exception):
            return parked_work_units(self)
        return []

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
        self._core_update_kanban_cap_spent = False
        self._stop_kanban_leash_s = None
        from gateway import exit_deadline
        exit_deadline.set_exit_target(None)  # the update's exit promise went with it
        self._draining = False
        self._restart_task_started = False
        self._scale_to_zero_status("running", "restart cancel: status mark failed")
