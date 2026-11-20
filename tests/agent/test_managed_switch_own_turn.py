"""The real turn facade may switch its own wire; external/copy-context workers may not."""
import contextvars
import threading

import pytest

from agent import nous_wire
from agent.agent_runtime_switch import TurnAdmittedDuringSwitch
from tests.agent import test_managed_switch_turn_custody as custody

managed = custody.managed
routing_home = custody.routing_home
streaming_route = custody.streaming_route
destinations = custody.destinations
live = custody.live


@pytest.mark.parametrize('managed_switch', [False, True])
def test_executing_turn_can_switch_but_copied_worker_cannot(live, monkeypatch, managed_switch):
    agent, _db = live
    calls = []

    def loop(current, *_args, **_kwargs):
        # build_turn_context sets this before turn_iteration_prep applies the pending wire.
        current._execution_thread_id = threading.get_ident()
        assert current._active_session_turn_lease_holder
        before = current._active_session_turn_lease_holder
        ctx, errors = contextvars.copy_context(), []

        def foreign():
            try:
                current.switch_model(custody.TARGET, 'anthropic')
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=lambda: ctx.run(foreign))
        worker.start()
        worker.join(30)
        assert not worker.is_alive()
        assert [type(exc) for exc in errors] == [TurnAdmittedDuringSwitch]
        if managed_switch:
            current.switch_model(custody.TARGET, 'anthropic')
            assert current.model == custody.TARGET
        else:
            current._nous_wire_pending = ('anthropic_messages', 'gmi')
            assert nous_wire.apply_pending_wire_switch(current) is True
            assert len(calls) == 1
        assert current._active_session_turn_lease_holder == before
        return {'final_response': 'ok', 'messages': []}

    monkeypatch.setattr('agent.conversation_loop.run_conversation', loop)
    if not managed_switch:
        monkeypatch.setattr('agent.model_routing_managed.managed_config', lambda: None)
        monkeypatch.setattr('agent.agent_runtime_helpers._switch_model_runtime',
                            lambda *args, **kwargs: calls.append(args[1:]))
    result = agent._run_conversation_scoped('fixture')
    assert result['final_response'] == 'ok'
    assert agent._active_session_turn_lease_holder is None


def test_stale_execution_thread_id_does_not_authorize_rpc_switch(live):
    agent, _db = live
    lease = custody.admit(agent)
    agent._execution_thread_id = threading.get_ident()
    try:
        with pytest.raises(TurnAdmittedDuringSwitch):
            agent.switch_model(custody.TARGET, 'anthropic')
    finally:
        lease.release()


@pytest.mark.parametrize('invalid', ['pid', 'agent', 'inactive', 'stopped', 'thread', 'holder'])
def test_own_turn_exemption_revalidates_custody(live, monkeypatch, invalid):
    from agent import origin_turn_custody as custody_scope
    agent, _db = live
    lease = custody.admit(agent)
    lease.turn_active = True
    agent._execution_thread_id = threading.get_ident()
    try:
        with custody_scope.scope(lease):
            assert custody_scope.owns_turn(agent)
            changes = {
                'pid': lambda: monkeypatch.setattr(custody_scope.os, 'getpid', lambda: -1),
                'agent': lambda: monkeypatch.setattr(lease, 'agent', object()),
                'inactive': lambda: monkeypatch.setattr(lease, 'turn_active', False),
                'stopped': lease.stop.set,
                'thread': lambda: monkeypatch.setattr(agent, '_execution_thread_id', -1),
                'holder': lambda: monkeypatch.setattr(agent, '_active_session_turn_lease_holder', 'successor'),
            }
            changes[invalid]()
            assert not custody_scope.owns_turn(agent)
    finally:
        monkeypatch.undo()
        lease.agent = agent
        agent._active_session_turn_lease_holder = lease.holder
        lease.release()
