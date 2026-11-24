"""Dormant landing must not opt a legacy host into native f2 scheduling."""
import threading

import pytest

from gateway.kanban_notification_schedule import BoundActions
from tui_gateway import notification_gateway_sources as sources


def test_environment_requires_explicit_opt_in(monkeypatch):
    monkeypatch.delenv('HERMES_FACTS_GATEWAY', raising=False)
    assert not sources.enabled()
    monkeypatch.setenv('HERMES_FACTS_GATEWAY', '1')
    assert sources.enabled()
    monkeypatch.setenv('HERMES_FACTS_GATEWAY', '0')
    assert not sources.enabled()


def test_registered_default_is_off():
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    assert DEFAULT_CONFIG['display']['gateway_notification_f'] is False


@pytest.mark.parametrize('state', [{}, {'display': {}}, {'display': {'gateway_notification_f': False}}])
def test_missing_policy_cannot_authorize_native_work(monkeypatch, tmp_path, state):
    from hermes_cli import config, fact_source_watch
    from tests.tui_gateway.test_gateway_native_source_lifecycle import Watch
    from tui_gateway.notification_gateway_policy import Policy

    monkeypatch.setattr(config, 'get_config_path', lambda: tmp_path / 'config.yaml')
    monkeypatch.setattr(config, 'load_config_readonly', lambda: state)
    monkeypatch.setattr(fact_source_watch, 'NativeSourceWatch', Watch)
    policy = Policy(threading.Event())
    try:
        policy.step(0, reconcile=True)
        assert not policy.enabled
        assert policy.retry_at == float('inf')
    finally:
        policy.close()


def test_default_gateway_uses_legacy_without_starting_source_owner(monkeypatch):
    from tui_gateway import notification_loaded_f

    monkeypatch.delenv('HERMES_FACTS_GATEWAY', raising=False)

    def forbidden(*args):
        pytest.fail('dormant gateway started native owner')

    monkeypatch.setattr(notification_loaded_f, 'register_gateway', forbidden)
    gate = BoundActions(lambda: None, native=True)
    try:
        assert gate.legacy
        assert gate.due(0, legacy_due=True)
        assert not gate.due(0, legacy_due=False)
        assert gate.native_action is None
    finally:
        gate.close()


def test_default_unloaded_keeps_legacy_cadence(monkeypatch):
    from tui_gateway import kanban_unloaded_schedule as unloaded, notification_loaded_f

    monkeypatch.delenv('HERMES_FACTS_GATEWAY', raising=False)
    now = [0]
    monkeypatch.setattr(unloaded.time, 'monotonic', lambda: now[0])

    def forbidden(*args):
        pytest.fail('dormant unloaded executor started native owner')

    monkeypatch.setattr(notification_loaded_f, 'register_gateway', forbidden)
    schedule = unloaded.UnloadedSchedule(lambda: None, 30, native=True)
    calls = []
    try:
        schedule.run('owner', lambda: calls.append(now[0]))
        now[0] = 1
        schedule.run('owner', lambda: calls.append(now[0]))
        now[0] = 30
        schedule.run('owner', lambda: calls.append(now[0]))
        assert calls == [0, 30]
    finally:
        schedule.close()
