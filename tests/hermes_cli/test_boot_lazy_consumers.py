"""Background setup cannot force a gateway import; first WS imports off-loop."""
import builtins
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


def test_setup_broadcast_does_not_import_an_unloaded_gateway(monkeypatch):
    from hermes_cli import free_tier_bootstrap as setup
    original = builtins.__import__
    attempts = []

    def guarded(name, *args, **kwargs):
        if name == 'tui_gateway.server':
            attempts.append(name)
            raise AssertionError('unexpected gateway import')
        return original(name, *args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.delitem(sys.modules, 'tui_gateway.server', raising=False)
        scoped.setattr(builtins, '__import__', guarded)
        record = SimpleNamespace(as_payload=Mock(return_value={'ready': True}))
        setup._broadcast(record)
        assert attempts == []
        assert 'tui_gateway.server' not in sys.modules
        record.as_payload.assert_not_called()
        broadcast = Mock()
        scoped.setitem(sys.modules, 'tui_gateway.server', SimpleNamespace(_broadcast_global_event=broadcast))
        setup._broadcast(record)
        broadcast.assert_called_once_with(setup.SETUP_READY_EVENT, {'ready': True})


@pytest.mark.asyncio
async def test_ws_first_import_runs_off_the_event_loop_after_auth(monkeypatch):
    import importlib
    from hermes_cli import mcp_startup
    from hermes_cli.web_routers import chat_ws
    loop_thread = threading.get_ident()
    order = []

    async def allowed(*args, **kwargs):
        order.append('auth')
        return True

    async def handle(ws, **kwargs):
        order.append('handle')

    def load(name):
        assert name == 'tui_gateway.ws'
        assert threading.get_ident() != loop_thread
        assert order == ['auth']
        order.append('import')
        return SimpleNamespace(handle_ws=handle)

    monkeypatch.setattr(chat_ws, '_close_unless_sidecar_allowed', allowed)
    monkeypatch.setattr(mcp_startup, 'start_deferred_mcp_discovery_now', lambda: None)
    monkeypatch.setattr(importlib, 'import_module', load)
    ws = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    await chat_ws.gateway_ws(ws)
    assert order == ['auth', 'import', 'handle']
