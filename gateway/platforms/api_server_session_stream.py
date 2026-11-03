"""Disconnect handling for ``POST /api/sessions/{session_id}/chat/stream``.

The route runs its turn as a task over an executor-backed ``_run_agent``; when the SSE socket
dies (or the handler is cancelled) the turn is drained here so the run record keeps its outcome
for ``GET /v1/runs/{run_id}``.
"""
import asyncio
from contextlib import suppress
from typing import Any


async def drain_task_on_disconnect(
    active_run_agents: dict[str, Any], run_id: str, task: asyncio.Task, agent_ref: list, *,
    interrupt_message: str, shield_wait: bool,
) -> None:
    """Preserve live run control refs until the executor-backed turn actually exits.

    Only a turn whose agent was never built is cancelled. Once the agent exists the turn is
    interrupted and awaited, including after its worker already unregistered it from
    ``active_run_agents`` on the way out (before the loop sees the result): cancelling there
    discards a finished reply and records the run as cancelled."""
    agent = active_run_agents.get(run_id) or agent_ref[0]
    if agent is None:
        if not task.done():
            task.cancel()
            with suppress(Exception):
                await task
        return
    with suppress(Exception):
        agent.interrupt(interrupt_message)
    if not task.done():
        with suppress(Exception):
            await (asyncio.shield(task) if shield_wait else task)
