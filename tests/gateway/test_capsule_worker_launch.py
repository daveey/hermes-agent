"""Actual owner spawn chooses only its own bootstrap-first capsule command."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway import session_managed_worker as managed


@pytest.mark.asyncio
@pytest.mark.parametrize('required', [True, False])
async def test_registered_worker_launch_shape(monkeypatch, required):
    authority = SimpleNamespace(profile_id='fixture', _managed_spawning={})
    monkeypatch.setattr(managed, '_mark_spawning', lambda *a: authority._managed_spawning)
    monkeypatch.setattr(managed, '_worker_env', lambda a: {'HERMES_HOME': '/fixture/profile'})
    import asyncio
    import threading
    calls = []
    def preflight(policy):
        calls.append(threading.current_thread() is threading.main_thread())
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            pytest.fail('capsule preflight ran on the event loop')
        return required
    monkeypatch.setattr('gateway.session_kanban_capsule.check_capsule_transport', preflight)
    monkeypatch.setattr('gateway.session_managed_worker_log.worker_log_path', lambda profile: None)
    seen = []
    class StopBeforeSpawn(Exception):
        pass
    async def acquire(spawn, argv, **kwargs):
        seen.append((argv, kwargs))
        raise StopBeforeSpawn
    monkeypatch.setattr('gateway.session_worker_spawn.acquire_process', acquire)
    with pytest.raises(StopBeforeSpawn):
        await managed._spawn_registered_worker(authority, SimpleNamespace(session_id='s'),
                                               {'generation': 1}, object())
    [(argv, kwargs)] = seen
    assert calls == [False]
    root = Path(managed.__file__).resolve().parents[1]
    if required:
        from hermes_cli._launch_template import render
        assert argv == [sys.executable, '-I', '-c', render(str(root), 'agent.managed_worker', 'main')]
    else:
        assert argv == [sys.executable, '-m', 'agent.managed_worker']
    assert kwargs['cwd'] == root
    assert kwargs['env']['HERMES_HOME'] == '/fixture/profile'
    assert authority._managed_spawning == {}
