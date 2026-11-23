"""Failed board teardown blocks admission but drains healthy siblings. [daveey]"""
from types import SimpleNamespace
import threading

import pytest

from hermes_cli.fact_contract import Unavailable
from plugins.kanban.dashboard import board_fact_views
from hermes_cli.fact_board_startup import BoardRuntime


def test_failed_shutdown_keeps_provider_closed_and_retries_custody(monkeypatch):
    runtime = object.__new__(BoardRuntime)
    runtime.home = 'fixture'
    runtime.api = SimpleNamespace(board_fact_views=board_fact_views)
    runtime.current = 'default'
    runtime._closed = runtime._drained = False
    runtime._lock = threading.RLock()
    runtime._previous = None
    calls = []
    def fail():
        calls.append('failed')
        raise TimeoutError('busy')
    runtime.views = {'default': SimpleNamespace(close=fail)}
    runtime.owners = {'default': SimpleNamespace(close=lambda: calls.append('healthy'))}
    installed = (runtime, runtime.provider)
    monkeypatch.setattr(board_fact_views, '_installed', installed)
    with pytest.raises(ExceptionGroup, match='board shutdown incomplete'):
        runtime.close()
    assert calls == ['failed', 'healthy']
    assert board_fact_views._installed == installed
    assert isinstance(runtime.provider(None), Unavailable)
    runtime.views['default'].close = lambda: calls.append('recovered')
    runtime.close()
    assert calls == ['failed', 'healthy', 'recovered', 'healthy']
    assert board_fact_views._installed is None
    runtime.close()
    assert len(calls) == 4
