"""Coalesce concurrent synchronous HTTP reads before threadpool admission."""
import asyncio
import inspect
from functools import partial, wraps
from weakref import WeakKeyDictionary
from typing import get_type_hints

from anyio.to_thread import run_sync
from hermes_constants import get_hermes_home
from hermes_cli.web_read_stages import ReadTrace


def _freeze(value):
    if isinstance(value, dict):
        return (type(value), frozenset((_freeze(k), _freeze(v)) for k, v in value.items()))
    if isinstance(value, (list, tuple)):
        return (type(value), tuple(map(_freeze, value)))
    if isinstance(value, (set, frozenset)):
        return (type(value), frozenset(map(_freeze, value)))
    return (type(value), value)


def coalesced_read(func, *, thread_runner=run_sync, admission_key=None, max_concurrent=1,
                   overflow_to_runner=False):
    """Return an async, in-flight-only wrapper around a synchronous read.

    Identical home + bound arguments share one in-flight result. Arguments must
    be hashable or built-in containers of hashable values and must not be
    mutated during the read. A custom async thread_runner must await the
    supplied zero-argument callable through a context-preserving threadpool,
    not abandon running work.

    Admission: by default one worker per decorated function/event loop.
    ``admission_key(bound_arguments)`` splits admission into groups (e.g. one
    per kanban board): one read per group at a time, at most ``max_concurrent``
    groups at once, so a slow read of one resource does not queue reads of
    another while a burst still holds a bounded number of workers. [daveey]
    ``overflow_to_runner`` limits coalescing bookkeeping, not the downstream
    runner's admission: excess distinct keys enter that runner immediately,
    retaining its original queue deadline instead of waiting for a shared slot.
    """
    if max_concurrent < 1:
        raise ValueError("max_concurrent must be >= 1")
    if admission_key is None and max_concurrent != 1:
        raise ValueError("max_concurrent needs an admission_key")
    signature = inspect.signature(func)
    hints = get_type_hints(func, include_extras=True)
    signature = signature.replace(
        parameters=[p.replace(annotation=hints.get(p.name, p.annotation))
                    for p in signature.parameters.values()],
        return_annotation=hints.get("return", signature.return_annotation),
    )
    states = WeakKeyDictionary()

    @wraps(func)
    async def wrapped(*args, **kwargs):
        loop = asyncio.get_running_loop()
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        home = str(get_hermes_home())
        key = (home, _freeze(bound.arguments))
        # Group before the read so a bad key fails this caller, not a shared task.
        group = None if admission_key is None else (home, _freeze(admission_key(bound.arguments)))
        pending, admission, groups = states.setdefault(loop, ({}, asyncio.Semaphore(max_concurrent), {}))
        entry = pending.get(key)
        if entry is None:
            trace = ReadTrace()
            if overflow_to_runner and len(pending) >= max_concurrent:
                # Do not postpone the lane's queue clock behind another semaphore.
                trace.join()
                return await trace.run(thread_runner, partial(func, *args, **kwargs))
            group_slot = None if group is None else groups.setdefault(group, asyncio.Semaphore(1))

            async def execute():
                try:
                    # Group first: a read queued behind its own group holds no shared slot.
                    if group_slot is None:
                        trace.emit("read_admission_wait")
                        async with admission:
                            return await trace.run(thread_runner, partial(func, *args, **kwargs))
                    trace.emit("read_group_wait")
                    async with group_slot:
                        trace.emit("read_admission_wait")
                        async with admission:
                            return await trace.run(thread_runner, partial(func, *args, **kwargs))
                finally:
                    pending.pop(key, None)
                    if not pending:
                        states.pop(loop, None)
            task = loop.create_task(execute())
            pending[key] = (task, trace)
            # Observe failures even if every HTTP caller has disconnected.
            task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        else:
            task, trace = entry
        trace.join()
        # A disconnected caller must not release admission for a live worker.
        return await asyncio.shield(task)

    wrapped.__signature__ = signature
    wrapped.__annotations__ = hints
    return wrapped
