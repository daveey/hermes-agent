"""Gateway worker wait and run-owned outcome custody on early abandonment."""
from __future__ import annotations

import asyncio
import threading


async def await_turn_worker(runner, worker, turn_ctx, interrupt_detected, interrupt_monitor):
    """Poll the executor, preserving this run's evidence even on timeout/cancellation."""
    from gateway.run import _abandon_timed_out_gateway_turn
    from gateway.turn_outcome import capture_run_outcome

    agent_holder = turn_ctx.agent_holder
    warning_fired = False
    try:
        while True:
            done, _ = await asyncio.wait({worker.executor_task}, timeout=5.0)
            if done:
                # Prefer the real result even if the watchdog fired in the same window.
                return worker.executor_task.result()
            if worker.agent_timeout is not None:
                if worker.timeout_fired.is_set():
                    break
                idle_secs = turn_ctx.awake_idle.measure(
                    runner._agent_activity_summary(agent_holder[0]).get("seconds_since_activity", 0.0))
                if not warning_fired and worker.agent_warning is not None and idle_secs >= worker.agent_warning:
                    warning_fired = True
                    await runner._run_agent_inactivity_warning(worker, turn_ctx.source, turn_ctx._status_thread_metadata)
                if idle_secs >= worker.agent_timeout:
                    threading.Thread(
                        target=_abandon_timed_out_gateway_turn,
                        kwargs={"agent_holder": agent_holder, **runner._reaper_kwargs(worker)},
                        name=f"gateway-turn-reaper-{worker.task_id[:12]}", daemon=True,
                    ).start()
                    break
            await runner._run_agent_backup_interrupt_check(turn_ctx, interrupt_detected, interrupt_monitor)
        return runner._run_agent_timeout_result(worker, turn_ctx)
    finally:
        # This invocation's own receipt (``begin_run_receipt``): only this worker's completion proves
        # its frozen receipt is ready; an abandoned worker names only a lease it holds now.
        if not worker.executor_task.done():
            capture_run_outcome(agent_holder[0], abandoned=True)
