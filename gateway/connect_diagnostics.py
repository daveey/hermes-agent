"""Bounded, credential-free await-chain snapshots for platform connect deadlines."""
import asyncio
import types
from pathlib import PurePath


def task_wait_chain(task: object) -> list[str]:
    """Describe native coroutine/generator locations only, never repr, source or locals.

    Task.get_stack() exposes only the outer suspended frame. Following cr_await/gi_yieldfrom
    identifies DNS, TLS, HTTP, and ready waits nested inside the Discord client task.
    """
    if not isinstance(task, asyncio.Task):
        return []
    current = task.get_coro()
    frames = []
    for _ in range(24):
        if isinstance(current, types.CoroutineType):
            frame, current = current.cr_frame, current.cr_await
        elif isinstance(current, types.GeneratorType):
            frame, current = current.gi_frame, current.gi_yieldfrom
        else:
            break
        if frame is not None:
            code = frame.f_code
            frames.append(f"{PurePath(code.co_filename).name}:{frame.f_lineno}:{code.co_qualname}")
    return frames


def connect_waits(adapter: object, task: asyncio.Task) -> dict[str, list[str]]:
    """The Discord bot is a separate task: the connect waiter alone only shows Event.wait."""
    waits = {"connect": task_wait_chain(task)}
    bot = getattr(adapter, "_bot_task", None)
    if isinstance(bot, asyncio.Task):
        waits["bot"] = task_wait_chain(bot)
    return waits
