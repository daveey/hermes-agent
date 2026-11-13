"""Cancellation-safe handoff of a managed subprocess acquired off the owner loop."""
import asyncio
import threading

# Attribute carrying the child's local-work custody on the Popen handle; ManagedWorker.close() consumes it.
WORK_ATTR = '_local_work_held'


async def acquire_process(spawn, *args, **kwargs):
    from agent import local_work_agent
    from gateway.session_managed_worker import ManagedWorker
    lock = threading.Lock()
    state = {'cancelled': False, 'process': None}
    # Reserved BEFORE the off-loop creation (the child may exist before the awaiting task resumes). Released
    # only by a failed spawn (no child) or by ManagedWorker.close() after verified exit + pipe/thread cleanup.
    held = local_work_agent.reserve('managed_child', 'gateway.session_worker_spawn.acquire_process')

    def create():
        try:
            process = spawn(*args, **kwargs)
        except BaseException:
            local_work_agent.release(held)  # spawn raised: no child was created
            raise
        if held is not None:
            try:
                setattr(process, WORK_ATTR, held)
            except Exception:  # health: allow BLE001 -- spawn succeeded; any failure to attach custody must latch unknown, never leak the child
                held.mark_unknown('managed-child-custody-unattachable')  # token stays: death unprovable
        with lock:
            cancelled = state['cancelled']
            if not cancelled:
                state['process'] = process
        if cancelled:
            ManagedWorker(process).close()
        return process

    try:
        pending = asyncio.create_task(asyncio.to_thread(create))
    except BaseException:
        local_work_agent.release(held)  # nothing was scheduled
        raise
    try:
        process = await asyncio.shield(pending)
        with lock:
            state['process'] = None
        return process
    except asyncio.CancelledError:
        with lock:
            state['cancelled'] = True
            process = state['process']
            state['process'] = None
        if process is not None:
            await asyncio.to_thread(ManagedWorker(process).close)
        # If creation still runs, its thread owns cleanup; consume an eventual spawn exception.
        pending.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
        raise
