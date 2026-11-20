"""Shared turn and tool entry points hold real kernel drain leases."""
import pytest

from hermes_cli import worker_drain_fence as fence
from tests.tools import test_worker_drain_spawn as helpers

node = helpers.node
pytestmark = pytest.mark.platforms('linux')


@pytest.mark.parametrize('kind', ['turn', 'tool'])
def test_shared_entry_holds_lease_and_refuses_late_work(node, monkeypatch, kind):
    root, ident = node
    calls = []

    def body(*args, **kwargs):
        calls.append(fence.busy_leases(root))
        return {'final_response': 'ok'}

    if kind == 'turn':
        from agent.turn_facade import TurnFacadeMixin
        monkeypatch.setattr(TurnFacadeMixin, '_run_conversation_scoped', body)
        from agent import local_work_agent
        from contextlib import nullcontext
        monkeypatch.setattr(local_work_agent, 'scope', lambda *a: nullcontext())
        invoke = lambda: TurnFacadeMixin.run_conversation(object(), 'hello')
    else:
        from tools.registry import ToolRegistry
        registry = ToolRegistry()
        registry.register(name='drain_test', toolset='test', schema={'name': 'drain_test', 'parameters': {}},
                          handler=body)
        invoke = lambda: registry.dispatch('drain_test', {})
    invoke()
    assert calls == [1]
    assert fence.busy_leases(root) == 0
    helpers.latch(root, ident)
    with pytest.raises(fence.DrainRefused, match='draining'):
        invoke()
    assert calls == [1]
