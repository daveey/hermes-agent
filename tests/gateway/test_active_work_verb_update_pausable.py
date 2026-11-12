"""The ``active-work`` verb says, on every unit, whether a core-update pause request parks it.

``update_pausable`` comes from the gateway update-pause hook itself (``_describe_active_work``): true
only while that hook is installed on a chat turn's agent, which happens only for an eligible bound
scope. Units with no such notion (api, delegation, managed, cron) are published ``False`` explicitly,
so a consumer never mistakes a missing field on an older core for a parkable unit.
"""
import os
from types import SimpleNamespace

from gateway.run import GatewayRunner
from gateway.session_update_pause_hook import bind_scope, installed


def _runner(agents):
    runner = object.__new__(GatewayRunner)
    runner._running_agents = {}
    runner.adapters = {}
    runner.session_authorities = []
    sessions = {key: SimpleNamespace(turn=SimpleNamespace(agent=agent, started_ts=None))
                for key, agent in agents.items()}
    runner._sessions_map = lambda: sessions
    runner._active_api_run_count = lambda: 1
    runner._active_deferred_agent_worker_count = lambda: 0
    return runner


def test_active_work_verb_reports_update_pausable_on_every_unit(tmp_path, monkeypatch):
    from gateway.run_active_work_verb import active_work_verb
    from tools import async_delegation
    monkeypatch.setattr("gateway.session_update_pause_hook.start_mid_tool_cancel", lambda agent, root: lambda: None)
    monkeypatch.setattr(async_delegation, 'list_async_delegations', lambda: [
        {'status': 'running', 'delegation_id': 'd1', 'goal': 'g', 'dispatched_at': 0}])
    pausable, plain = SimpleNamespace(), SimpleNamespace()
    runner = _runner({'k:pausable': pausable, 'k:plain': plain})
    runner._describe_parked_work = list
    runner._active_work_count = lambda: 4
    loop = SimpleNamespace(call_soon_threadsafe=lambda fn: fn())
    with bind_scope(tmp_path, eligible=True), installed(pausable):
        answer = active_work_verb(runner, loop)({})
    assert answer['pid'] == os.getpid()
    got = {(u['kind'], u.get('session') or u.get('delegation_id') or ''): u['update_pausable']
           for u in answer['active_work']}
    assert got == {('chat', 'k:pausable'): True, ('chat', 'k:plain'): False,
                   ('api', ''): False, ('delegation', 'd1'): False}
    assert all(type(u['update_pausable']) is bool for u in answer['active_work'])
