"""Conservative closeout cues never execute arbitrary waiting-source probes."""
import contextvars
import threading

import pytest

from hermes_cli import kanban_annotation_host as host
from hermes_cli import kanban_session_activity as ksa
from hermes_cli import plugins
from hermes_cli.plugins import PluginManager


@pytest.fixture
def world(monkeypatch):
    scope = contextvars.ContextVar('closeout_test_scope', default=('home', 'default'))
    monkeypatch.setattr(ksa, '_origin_scope', scope.get)
    monkeypatch.setattr(host, 'enabled', lambda: True)
    monkeypatch.setattr(ksa, '_waiting_sources', {})
    monkeypatch.setattr(host, '_declared', {})
    monkeypatch.setattr(plugins, '_resolve_hook_callback_timeout', lambda: 0)
    calls = []
    monkeypatch.setattr(host, 'kick', calls.append)
    return scope, calls


def forbidden():
    pytest.fail('hook observer executed a waiting source or input probe')


def source(*, declared=True):
    ksa.register_waiting_source('closeout-test', forbidden, forbidden if declared else None)


def run(callback, hook='post_llm_call'):
    manager = PluginManager()
    manager._hooks[hook] = [callback]
    return manager.invoke_hook(hook, session_id='s')


@pytest.mark.parametrize('declared', [True, False])
def test_callback_runs_without_any_source_or_input_execution(world, declared):
    _, calls = world
    source(declared=declared)
    def callback(**_):
        assert calls == []  # no acknowledgement before the actual commit
        return 'answer'
    assert run(callback) == ['answer']
    assert calls == [('home', 'default')]


def _no_waiting() -> dict:
    return {}


def test_ordinary_callback_also_cues_conservatively(world):
    _, calls = world
    ksa.register_waiting_source('unchanged', _no_waiting, lambda: 1)
    run(lambda **_: None)
    assert calls == [('home', 'default')]


@pytest.mark.parametrize('hook', ['pre_llm_call', 'post_tool_call'])
def test_other_hooks_never_sample_or_kick(world, hook):
    _, calls = world
    source()
    run(lambda **_: None, hook)
    assert calls == []


def test_no_sources_and_disabled_do_not_kick(world, monkeypatch):
    _, calls = world
    run(lambda **_: None)
    monkeypatch.setattr(host, 'enabled', lambda: False)
    source()
    run(lambda **_: None)
    assert calls == []


@pytest.mark.parametrize('error', [ValueError, SystemExit])
def test_callback_exception_is_preserved_and_still_cues(world, error):
    _, calls = world
    source()
    original = error('after commit')
    def callback(**_):
        raise original
    with pytest.raises(error) as raised:
        PluginManager._invoke_observed_callback('post_llm_call', callback, {})
    assert raised.value is original
    assert calls == [('home', 'default')]


def test_timeout_cues_only_when_physical_callback_finishes_in_original_scope(world, monkeypatch):
    scope, calls = world
    source()
    entered, release, cued = threading.Event(), threading.Event(), threading.Event()
    def kick(value):
        calls.append(value)
        cued.set()
    monkeypatch.setattr(host, 'kick', kick)
    def callback(**_):
        entered.set()
        assert release.wait(5)
    token = scope.set(('home', 'other'))
    manager = PluginManager()
    try:
        from hermes_cli.plugins_dispatch import _HOOK_SKIPPED
        assert manager._run_hook_callback_bounded('post_llm_call', callback, {'session_id': 's'}, 0.03) is _HOOK_SKIPPED
    finally:
        scope.reset(token)
    try:
        assert entered.is_set() and calls == []
        # A suppressed call did not execute and must not emit a cue.
        assert manager._run_hook_callback_bounded('post_llm_call', callback, {'session_id': 's'}, 0.03) is _HOOK_SKIPPED
        assert calls == []
    finally:
        release.set()
    assert cued.wait(5)
    assert calls == [('home', 'other')]


@pytest.mark.parametrize('boundary', ['scope', 'kick'])
@pytest.mark.parametrize('error', [OSError, SystemExit])
def test_observer_failures_preserve_callback_result(world, monkeypatch, boundary, error):
    source()
    def broken(*_):
        raise error('unavailable')
    monkeypatch.setattr(ksa if boundary == 'scope' else host,
                        '_origin_scope' if boundary == 'scope' else 'kick', broken)
    assert run(lambda **_: 'still ran') == ['still ran']


@pytest.mark.parametrize('remove', [False, True])
def test_registration_change_during_callback_still_cues(world, remove):
    _, calls = world
    if remove:
        source()
    def callback(**_):
        if remove:
            ksa.register_waiting_source('closeout-test', None)
        else:
            source()
    run(callback)
    assert calls == [('home', 'default')]


def test_split_module_callback_is_not_excluded(world):
    _, calls = world
    source()
    def callback(**_):
        return 'changed via helper'
    callback.__module__ = 'different_plugin_helper'
    assert run(callback) == ['changed via helper']
    assert calls == [('home', 'default')]
