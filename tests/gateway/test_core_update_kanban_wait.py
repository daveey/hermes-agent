"""Only confirmed Kanban work gets a short core-update restart wait."""
import asyncio
import json
import os
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


def test_default_gateway_drain_bound_is_75_seconds():
    from gateway.shutdown_watchdog import resolve_shutdown_watchdog_delay
    runner, _ = make_restart_runner()
    # Same core path on hub and Mac; launchd may shorten, never extend, this watchdog.
    assert wait.CORE_UPDATE_KANBAN_WAIT_S + resolve_shutdown_watchdog_delay(runner._restart_drain_timeout) == 75.0


def test_real_managed_counts_reach_the_short_wait(tmp_path):
    from gateway.session_managed_worker import ManagedWorker
    runner, _ = make_restart_runner()
    card = ManagedWorker(SimpleNamespace())
    card.kanban_card = True
    runner.session_authority = SimpleNamespace(_managed_workers={'s': card}, sessions={})
    assert runner._active_work_count() == runner._awaitable_work_count() == 1
    since, expired = runner._kanban_restart_wait_state(tmp_path / 'intent', 0.0, None)
    assert (since, expired) == (0.0, False)
    assert runner._kanban_restart_wait_state(tmp_path / 'intent', 15.0, since) == (0.0, True)
    # A real managed human worker is not a card, even though it has the same process class.
    human = ManagedWorker(SimpleNamespace())
    runner.session_authority._managed_workers['human'] = human
    assert runner._awaitable_work_count() == 2
    assert runner._kanban_restart_wait_state(tmp_path / 'intent', 16.0, since) == (None, False)


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
    assert await managed.admit_managed(authority, SimpleNamespace(session_id='s'), row, policy) == 'reply'
    assert recorded.call_count == int(expected)
    if expected:
        # [daveey] A clean restart/shutdown Stop is a continuation checkpoint (session_kanban_continue).
        assert recorded.call_args.kwargs['checkpoint'] == {'session_id': 's', 'request_id': None,
                                                           'admission_id': 'a', 'cause': cause}


@pytest.mark.asyncio
@pytest.mark.parametrize('has_intent', [True, False])
async def test_restart_line_states_whether_the_kanban_cap_is_armed(monkeypatch, tmp_path, caplog, has_intent):
    """The hub's missing cap (14:38Z) could not be read from the log: the line showed only cap=1800s."""
    runner, _ = make_restart_runner()
    intent = tmp_path / 'intent'
    intent.write_text('{"pid": 1, "kind": "unfenced"}\n')  # exactly what hermes-sync's unfenced path writes
    runner._restart_update_intent = intent if has_intent else None
    runner._restart_after_turn_timeout = 30
    runner._active_work_count = runner._awaitable_work_count = lambda: 1
    runner._describe_active_work = list
    monkeypatch.setattr(wait, 'kanban_managed_count', lambda _: 1, raising=False)
    clock = [0.0]
    monkeypatch.setattr(asyncio, 'get_running_loop', lambda: SimpleNamespace(time=lambda: clock[0]))
    async def advance(seconds):
        clock[0] += seconds
    monkeypatch.setattr(asyncio, 'sleep', advance)
    with caplog.at_level('INFO', logger='gateway.run'):
        await runner._await_active_work_before_restart()
    line = next(r.getMessage() for r in caplog.records if 'Restart requested with' in r.getMessage())
    if has_intent:
        assert 'core-update intent present: Kanban-only cap 15s armed' in line
    else:
        assert 'no core-update intent: ordinary restart' in line


def test_unfenced_intent_is_not_a_launcher_fence(tmp_path):
    """hermes-sync's unfenced intent (hub) arms the gateway cap but parks no launcher, so `hermes update`
    must keep its own gateway-restart verification."""
    from hermes_cli.update_gateway_fence import ancestor_fence_owner
    home = tmp_path / 'h'
    (home / 'core-update').mkdir(parents=True)
    path = home / 'core-update' / 'intent'
    path.write_text(json.dumps({'pid': os.getppid(), 'created_at': 0, 'kind': 'unfenced'}) + '\n')
    assert ancestor_fence_owner([home]) is None
    path.write_text(json.dumps({'pid': os.getppid(), 'created_at': 0}) + '\n')
    assert ancestor_fence_owner([home]) == os.getppid()
