"""Native observation hooks are inert unless a trusted entry loaded ``observer_runtime``. [daveey]

Regression: the merged observer hooks imported ``observer_runtime`` unconditionally, so every ordinary
managed worker died with ``ModuleNotFoundError`` before reaching the model.
"""
import importlib
import sys
import types

import pytest


def test_core_hook_modules_import_without_observer(monkeypatch):
    monkeypatch.delitem(sys.modules, 'observer_runtime', raising=False)
    monkeypatch.delitem(sys.modules, 'observer_codex', raising=False)
    for name in ('agent.chat_completion_helpers', 'agent.tool_executor', 'agent.codex_runtime',
                 'agent.turn_api_call', 'agent.client_lifecycle', 'agent.session_persistence'):
        importlib.import_module(name)
    assert 'observer_runtime' not in sys.modules


def test_unobserved_hooks_pass_through(monkeypatch):
    from agent import observation_seam as seam
    monkeypatch.delitem(sys.modules, 'observer_runtime', raising=False)
    assert seam.sdk_request(lambda a, b: (a, b))(1, b=2) == (1, 2)
    assert seam.terminal_commit(lambda *a: 'ok')('agent', [], None) == 'ok'
    assert seam.unsupported_summary(lambda: 'summary')() == 'summary'
    assert seam.active() is False
    seam.persisted(object(), True)
    seam.terminal(object(), 'terminal', 'c1', 'r', 'o', False)
    assert seam.finish_oneshot(3) == 3
    created = []
    client = types.SimpleNamespace(responses=types.SimpleNamespace(create=lambda **kw: created.append(kw) or 'stream'))
    assert seam.open_codex_stream(object(), client, {'stream': True}) == 'stream'
    assert created == [{'stream': True}]


def test_observed_launch_delegates_to_loaded_observer(monkeypatch):
    from agent import observation_seam as seam
    calls = []
    fake = types.SimpleNamespace()

    def sdk_request(fn):
        def wrapped(*a, **k):
            calls.append('sdk')
            return fn(*a, **k)
        return wrapped
    fake.sdk_request = sdk_request
    fake.unsupported_summary = lambda fn: (lambda *a, **k: (_ for _ in ()).throw(PermissionError('unsupported')))
    fake.active = lambda: True
    fake.persisted = lambda agent, ok: calls.append(('persisted', ok))
    fake.finish_oneshot = lambda rc: 7
    monkeypatch.setitem(sys.modules, 'observer_runtime', fake)
    # Decorated at import time with no observer present: still delegates once one is loaded.
    hooked = seam.sdk_request(lambda x: x * 2)
    assert hooked(4) == 8 and calls == ['sdk']
    with pytest.raises(PermissionError):
        seam.unsupported_summary(lambda: 'x')()
    assert seam.active() is True
    seam.persisted(None, False)
    assert calls[-1] == ('persisted', False)
    assert seam.finish_oneshot(0) == 7
