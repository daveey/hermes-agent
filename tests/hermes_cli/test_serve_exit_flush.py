"""No agent import at boot; loaded gateways still receive the full exit flush."""
import signal
import sys
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize('initializing', [True, False])
def test_lazy_flush_only_calls_fully_initialized_gateway(monkeypatch, initializing):
    from hermes_cli import serve_exit_flush as bridge
    calls = []
    gateway = SimpleNamespace(__spec__=SimpleNamespace(_initializing=initializing),
                              _flush_sessions_before_exit=lambda: calls.append('flush'))
    with monkeypatch.context() as scoped:
        scoped.setitem(sys.modules, 'tui_gateway.server', gateway)
        bridge.dispatch(signal.SIGTERM, None, lambda *a: calls.append('previous'))
    assert calls == (['previous'] if initializing else ['flush', 'previous'])


def test_lazy_flush_respects_ignored_signal(monkeypatch):
    from hermes_cli import serve_exit_flush as bridge
    gateway = SimpleNamespace(__spec__=None,
                              _handle_exit_flush_signal=lambda *a: pytest.fail('ignored signal flushed'))
    with monkeypatch.context() as scoped:
        scoped.setitem(sys.modules, 'tui_gateway.server', gateway)
        bridge.dispatch(signal.SIGINT, None, signal.SIG_IGN)
