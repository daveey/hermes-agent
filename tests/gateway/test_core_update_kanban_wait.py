"""Only confirmed Kanban work gets a short core-update restart wait."""
import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gateway import run_shutdown_restart_wait as wait
from gateway.session_authorities import SessionAuthorities
from gateway.session_runtime_workers import kanban_managed_count
from tests.gateway.restart_test_helpers import make_restart_runner


def worker(*, kanban=True, closed=False):
    return SimpleNamespace(kanban_card=kanban, closed=SimpleNamespace(is_set=lambda: closed))


def test_count_confirmed_workers_across_profiles(tmp_path):
    runner = SimpleNamespace(session_authorities=SessionAuthorities(tmp_path))
    runner.session_authorities.add(tmp_path, SimpleNamespace(_managed_workers={
        'card': worker(), 'chat': worker(kanban=False), 'closed': worker(closed=True),
        'unclassified': SimpleNamespace(closed=SimpleNamespace(is_set=lambda: False))}))
    runner.session_authorities.add(tmp_path / 'profiles' / 'other',
                                   SimpleNamespace(_managed_workers={'card': worker()}))
    assert kanban_managed_count(runner) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('other', [None, 'chat', 'cron', 'api', 'mutation', 'unclassified'])
async def test_core_update_caps_only_kanban_wait(monkeypatch, tmp_path, other):
    runner, _ = make_restart_runner()
    intent = tmp_path / 'intent'
    intent.touch()
    runner._restart_update_intent = intent
    runner._restart_after_turn_timeout = 1800
    runner._active_work_count = lambda: 2 if other else 1
    runner._awaitable_work_count = runner._active_work_count
    runner._describe_active_work = list
    monkeypatch.setattr(wait, 'kanban_managed_count', lambda _: 1, raising=False)
    # Virtual monotonic time: no real 15s/1800s wait and no wall-clock dependence.
    clock = [0.0]
    loop = SimpleNamespace(time=lambda: clock[0])
    monkeypatch.setattr(asyncio, 'get_running_loop', lambda: loop)
    async def advance(seconds):
        clock[0] += seconds
    monkeypatch.setattr(asyncio, 'sleep', advance)
    assert await runner._await_active_work_before_restart() is False
    assert (15 <= clock[0] < 15.2) if other is None else (1800 <= clock[0] < 1800.2)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['ordinary', 'mixed_to_kanban', 'finished', 'cancelled'])
async def test_wait_transition_and_cancellation(monkeypatch, tmp_path, mode):
    runner, _ = make_restart_runner()
    intent = tmp_path / 'intent'
    intent.touch()
    runner._restart_update_intent = None if mode == 'ordinary' else intent
    runner._restart_after_turn_timeout = 60
    runner._describe_active_work = list
    clock = [0.0]
    def count():
        if mode == 'finished' and clock[0] >= 3:
            return 0
        return 2 if mode == 'mixed_to_kanban' and clock[0] < 5 else 1
    runner._active_work_count = count
    runner._awaitable_work_count = count
    monkeypatch.setattr(wait, 'kanban_managed_count', lambda _: 1, raising=False)
    monkeypatch.setattr(asyncio, 'get_running_loop', lambda: SimpleNamespace(time=lambda: clock[0]))
    async def advance(seconds):
        clock[0] += seconds
        if mode == 'cancelled' and clock[0] >= 3 and intent.exists():
            intent.unlink()
    monkeypatch.setattr(asyncio, 'sleep', advance)
    result = await runner._await_active_work_before_restart()
    if mode == 'ordinary':
        assert result is False and 60 <= clock[0] < 60.2
    elif mode == 'mixed_to_kanban':
        assert result is False and 20 <= clock[0] < 20.3
    elif mode == 'finished':
        assert result is True and clock[0] < 4
    else:
        assert result is wait.RESTART_CANCELLED and clock[0] < 4


@pytest.mark.asyncio
@pytest.mark.parametrize('cause,interrupted,expected', [
    ('Gateway restarting', True, True), ('Gateway shutting down', True, True),
    (None, True, False), ('Gateway restarting', False, False)])
async def test_cooperative_stop_booking_before_admission_settlement(monkeypatch, cause, interrupted, expected):
    from gateway import session_managed_worker as managed
    from gateway import session_kanban_restart as booking
    authority = SimpleNamespace(pending_results={}, _mutation_tasks=set())
    row = {'admission_id': 'a'}
    policy = SimpleNamespace(kanban_json='{}')
    async def execute(*_):
        authority.pending_results['a'] = {
            'result': {'interrupted': interrupted, 'interrupt_message': 'untrusted worker text'},
            'runtime_stop_cause': cause}
        return 'reply'
    monkeypatch.setattr(managed, 'execute_managed', execute)
    monkeypatch.setattr('gateway.session_policy.workspace_ready', lambda _: True)
    recorded = MagicMock()
    monkeypatch.setattr(booking, 'record_restart_interruption', recorded)
    assert await managed.admit_managed(authority, None, row, policy) == 'reply'
    assert recorded.call_count == int(expected)
