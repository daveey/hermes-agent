"""A ``type: exec`` quick command that times out or is cancelled leaves nothing running.

Killing only the shell left ``sleep 120 & wait``'s sleep alive, and a cancelled await (gateway
shutdown on the WebSocket path) skipped cleanup entirely.
"""
import asyncio
import contextlib
from pathlib import Path

import psutil
import pytest

from gateway import run_inbound_hooks
from gateway.run_inbound_hooks import GatewayInboundHooksMixin
from tests.waiting import async_wait_until

_TREE = 'echo $$ > shell.pid; sleep 120 & echo $! > child.pid; wait'


def _alive(pid):
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _pid(path: Path):
    text = path.read_text().strip() if path.exists() else ''
    return int(text) if text else None


@pytest.mark.platforms("posix")
@pytest.mark.asyncio
async def test_timed_out_exec_command_kills_its_background_child(tmp_path, monkeypatch):
    monkeypatch.setattr(run_inbound_hooks, '_EXEC_QUICK_COMMAND_TIMEOUT', 2)
    reply = await GatewayInboundHooksMixin()._hm_run_exec_quick_command('probe', _TREE, cwd=str(tmp_path))
    assert reply == run_inbound_hooks.t('gateway.quick_command.timed_out')
    child = _pid(tmp_path / 'child.pid')
    assert child is not None
    await async_wait_until(lambda: not _alive(child), timeout=10, what='the backgrounded sleep to stop')


@pytest.mark.platforms("posix")
@pytest.mark.asyncio
async def test_cancelled_exec_command_kills_shell_and_child(tmp_path):
    task = asyncio.create_task(
        GatewayInboundHooksMixin()._hm_run_exec_quick_command('probe', _TREE, cwd=str(tmp_path)))
    child = await async_wait_until(lambda: _pid(tmp_path / 'child.pid'), timeout=10, what='the child pid')
    shell = _pid(tmp_path / 'shell.pid')
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await async_wait_until(lambda: not _alive(shell) and not _alive(child), timeout=10,
                           what='the shell and its child to stop')


@pytest.mark.platforms("posix")
@pytest.mark.asyncio
@pytest.mark.parametrize('cancel_twice', [False, True])
async def test_cancelled_exec_during_spawn_kills_child(tmp_path, monkeypatch, cancel_twice):
    # The shell can publish child.pid before asyncio finishes connecting its pipes.
    # Hold that ownership-transfer boundary, not the command's communicate await.
    create_shell = asyncio.create_subprocess_shell
    spawned = asyncio.Event()
    release = asyncio.Event()
    processes = []

    async def held_spawn(*args, **kwargs):
        proc = await create_shell(*args, **kwargs)
        processes.append(proc)
        spawned.set()
        await release.wait()
        return proc

    cleanup_started = asyncio.Event()
    cleanups = []
    stop_pending = run_inbound_hooks._stop_pending_exec_tree

    async def observed_cleanup(spawn):
        cleanups.append(asyncio.current_task())
        cleanup_started.set()
        await stop_pending(spawn)

    monkeypatch.setattr(run_inbound_hooks, '_stop_pending_exec_tree', observed_cleanup)
    monkeypatch.setattr(asyncio, 'create_subprocess_shell', held_spawn)
    task = asyncio.create_task(
        GatewayInboundHooksMixin()._hm_run_exec_quick_command('probe', _TREE, cwd=str(tmp_path)))
    try:
        await asyncio.wait_for(spawned.wait(), timeout=30)
        child = await async_wait_until(lambda: _pid(tmp_path / 'child.pid'), timeout=30)
        assert _alive(child)
        task.cancel()
        if cancel_twice:
            await asyncio.wait_for(cleanup_started.wait(), timeout=30)
            task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=30)
        await async_wait_until(lambda: not _alive(processes[0].pid) and not _alive(child),
                               timeout=10, what='the cancelled spawn tree to stop')
    finally:
        release.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await asyncio.gather(*cleanups, return_exceptions=True)
        for proc in processes:
            await run_inbound_hooks._stop_exec_tree(proc)


@pytest.mark.asyncio
async def test_cancelled_exec_spawn_failure_preserves_cancellation(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()

    async def failed_spawn(*args, **kwargs):
        started.set()
        await release.wait()
        raise OSError('spawn failed')

    monkeypatch.setattr(asyncio, 'create_subprocess_shell', failed_spawn)
    task = asyncio.create_task(GatewayInboundHooksMixin()._hm_run_exec_quick_command('probe', 'unused'))
    try:
        await asyncio.wait_for(started.wait(), timeout=30)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=30)
    finally:
        release.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
