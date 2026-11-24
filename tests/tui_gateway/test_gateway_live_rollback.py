"""A live f2 rollback is configuration publication, never remote env injection."""
import pytest

import builtins
import sqlite3
import threading

from gateway.kanban_notification_schedule import BoundActions, PassReport
from tui_gateway import notification_gateway_sources as sources, notification_loaded_f as loaded


def test_config_rollback_reenable_preserves_custody_and_quiet_gate(monkeypatch, tmp_path):
    from hermes_cli import config, fact_source_watch
    from tests.tui_gateway.test_gateway_native_source_lifecycle import Watch

    state = {'display': {'gateway_notification_f': True}}
    monkeypatch.setattr(config, 'load_config_readonly', lambda: state)
    monkeypatch.setattr(config, 'get_config_path', lambda: tmp_path / 'config.yaml')
    monkeypatch.setattr(fact_source_watch, 'NativeSourceWatch', Watch)
    monkeypatch.setattr(sources, 'inventory', lambda: ((tmp_path / 'board.db',), ()))
    monkeypatch.setenv('HERMES_FACTS_GATEWAY', '1')
    scheduler = loaded.Scheduler(clock=lambda: 0)
    monkeypatch.setattr(loaded, '_owner', scheduler)
    gate = BoundActions(lambda: None, native=True)
    try:
        assert not gate.due(0, legacy_due=True)
        scheduler.step()
        assert gate.due(0, legacy_due=False)
        gate.settled(PassReport(pending=True, deadline=4), 0, 5)
        owner = scheduler.gateway_sources
        source_watch = owner.watch
        state['display']['gateway_notification_f'] = False
        owner.policy.changed()
        owner.step(1, reconcile=False)
        assert source_watch.closed
        assert owner.watch is None
        assert gate.legacy
        assert not gate.due(1, legacy_due=True)  # refusal floor/custody unchanged
        assert gate.due(4, legacy_due=False)
        gate.settled(PassReport(), 4, 5)
        assert gate.due(5, legacy_due=True)
        assert not gate.due(5, legacy_due=False)
        assert owner.policy.watch is not None  # native control survives rollback
        state['display']['gateway_notification_f'] = True
        owner.policy.changed()
        owner.step(6, reconcile=False)
        assert owner.ready
        assert owner.watch is not source_watch
        assert gate.due(6, legacy_due=False)
        gate.settled(PassReport(), 6, 5)

        def forbidden(*args, **kwargs):
            raise AssertionError('quiet admission did source I/O')

        with monkeypatch.context() as patch:
            patch.setattr(builtins, 'open', forbidden)
            patch.setattr(sqlite3, 'connect', forbidden)
            patch.setattr(config, 'load_config_readonly', forbidden)
            assert not gate.due(7, legacy_due=True)
        control = owner.policy.watch
        gate.close()
        owner.step(8, reconcile=False)
        assert control.closed
    finally:
        gate.close()
        scheduler.close()


def test_config_only_hint_does_not_admit_collection(monkeypatch, tmp_path):
    from hermes_cli import config, fact_source_watch
    from tests.tui_gateway.test_gateway_native_source_lifecycle import Watch

    reads = []
    monkeypatch.setattr(config, 'get_config_path', lambda: tmp_path / 'config.yaml')
    monkeypatch.setattr(config, 'load_config_readonly', lambda: reads.append(1) or {'display': {'gateway_notification_f': True}})
    monkeypatch.setattr(fact_source_watch, 'NativeSourceWatch', Watch)
    monkeypatch.setattr(sources, 'inventory', lambda: ((), ()))
    owner = sources.Sources(threading.Event())
    action = owner.add(lambda: None)
    try:
        owner.step(0, reconcile=True)
        assert action.due(legacy_due=False)
        owner.policy.changed()
        owner.step(1, reconcile=False)
        assert len(reads) == 2
        assert not action.due(legacy_due=False)
        owner.step(30, reconcile=True)  # one existing census repairs missed config hints
        assert len(reads) == 3
    finally:
        owner.close()


def test_real_canonical_config_write_is_a_native_live_rollback(monkeypatch, tmp_path):
    from hermes_cli import config

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(config, 'get_config_path', lambda: tmp_path / 'config.yaml')
    monkeypatch.setattr(sources, 'inventory', lambda: ((tmp_path / 'board.db',), ()))
    initial = config.load_config()
    initial.setdefault('display', {})['gateway_notification_f'] = True
    config.save_config(initial)
    owner = sources.Sources(threading.Event())
    action = owner.add(lambda: None)
    try:
        owner.step(0, reconcile=True)
        assert owner.ready
        assert owner.policy.enabled
        assert action.due(legacy_due=False)
        owner.event.clear()
        updated = config.load_config()
        updated['display']['gateway_notification_f'] = False
        config.save_config(updated)
        assert owner.event.wait(5)  # actual native commit hint, no process injection or polling
        owner.step(1, reconcile=False)
        assert not owner.policy.enabled
        assert owner.watch is None
        assert not action.due(legacy_due=False)
    finally:
        owner.close()


def test_policy_failure_quiet_retry_and_unsupported_census(monkeypatch, tmp_path):
    from hermes_cli import config, fact_source_watch
    from tui_gateway.notification_gateway_policy import Policy
    from tests.tui_gateway.test_gateway_native_source_lifecycle import Watch

    monkeypatch.setattr(config, 'get_config_path', lambda: tmp_path / 'config.yaml')
    monkeypatch.setattr(fact_source_watch, 'NativeSourceWatch', Watch)
    fail = [True]
    reads = []

    def capture():
        reads.append(1)
        if fail[0]:
            raise ValueError('config unavailable')
        return {'display': {'gateway_notification_f': True}}

    monkeypatch.setattr(config, 'load_config_readonly', capture)
    policy = Policy(threading.Event())
    try:
        assert policy.step(0, reconcile=False)
        assert not policy.enabled
        assert policy.retry_at == 1
        assert not policy.step(0.5, reconcile=False)
        assert reads == [1]
        fail[0] = False
        assert policy.step(1, reconcile=False)
        assert policy.enabled
        assert policy.retry_at == float('inf')
        old = policy.watch
        policy.close()
        assert old.closed
        policy.close()

        def unsupported(*args, **kwargs):
            raise NotImplementedError

        monkeypatch.setattr(fact_source_watch, 'NativeSourceWatch', unsupported)
        assert not policy.step(2, reconcile=False)
        assert policy.enabled
        assert policy.watch is None
        assert not policy.step(3, reconcile=False)
        assert reads == [1, 1, 1]
        assert not policy.step(30, reconcile=True)
        assert reads == [1, 1, 1, 1]
    finally:
        policy.close()


def test_cold_and_repeated_off_need_no_data_watch(monkeypatch, tmp_path):
    from hermes_cli import config, fact_source_watch
    from tests.tui_gateway.test_gateway_native_source_lifecycle import Watch

    monkeypatch.setattr(config, 'get_config_path', lambda: tmp_path / 'config.yaml')
    monkeypatch.setattr(config, 'load_config_readonly', lambda: {'display': {'gateway_notification_f': False}})
    monkeypatch.setattr(fact_source_watch, 'NativeSourceWatch', Watch)
    owner = sources.Sources(threading.Event())
    action = owner.add(lambda: None)
    try:
        owner.step(0, reconcile=True)
        assert owner.watch is None
        assert not owner.ready
        assert not action.due(legacy_due=False)
        owner.step(30, reconcile=True)
        assert owner.watch is None
        assert owner.policy.watch is not None
    finally:
        owner.close()


def test_policy_watch_loss_revokes_native_until_quiet_retry(monkeypatch, tmp_path):
    from hermes_cli import config, fact_source_watch
    from tui_gateway.notification_gateway_policy import Policy
    from tests.tui_gateway.test_gateway_native_source_lifecycle import Watch

    monkeypatch.setattr(config, 'get_config_path', lambda: tmp_path / 'config.yaml')
    monkeypatch.setattr(config, 'load_config_readonly', lambda: {'display': {'gateway_notification_f': True}})
    monkeypatch.setattr(fact_source_watch, 'NativeSourceWatch', Watch)
    policy = Policy(threading.Event())
    try:
        policy.step(0, reconcile=True)
        assert policy.enabled

        def unavailable():
            raise OSError('native control lease lost')

        monkeypatch.setattr(policy.watch, 'check', unavailable)
        policy.changed()
        assert policy.step(1, reconcile=False)
        assert not policy.enabled
        assert policy.retry_at == 2
        monkeypatch.setattr(policy.watch, 'check', lambda: None)
        assert not policy.step(1.5, reconcile=False)
        assert policy.step(2, reconcile=False)
        assert policy.enabled
    finally:
        policy.close()


def test_failed_config_envelope_cannot_authorize_native_work(monkeypatch, tmp_path):
    from hermes_cli import config, fact_source_watch
    from tui_gateway.notification_gateway_policy import Policy
    from tests.tui_gateway.test_gateway_native_source_lifecycle import Watch

    monkeypatch.setattr(config, 'get_config_path', lambda: tmp_path / 'config.yaml')
    monkeypatch.setattr(fact_source_watch, 'NativeSourceWatch', Watch)
    monkeypatch.setattr(config, 'load_config_readonly', lambda: config.FailedConfigRead(
        {'display': {'gateway_notification_f': True}}, error=OSError('unreadable')))
    policy = Policy(threading.Event())
    try:
        policy.step(0, reconcile=True)
        assert not policy.enabled
        assert policy.retry_at == 1
    finally:
        policy.close()


def test_native_live_off_role_transfer_catches_up_before_legacy_timer(monkeypatch, tmp_path):
    from hermes_cli import config, fact_source_watch
    from tui_gateway import kanban_unloaded_schedule as unloaded
    from tests.tui_gateway.test_gateway_native_source_lifecycle import Watch

    state = {'display': {'gateway_notification_f': True}}
    monkeypatch.setattr(config, 'get_config_path', lambda: tmp_path / 'config.yaml')
    monkeypatch.setattr(config, 'load_config_readonly', lambda: state)
    monkeypatch.setattr(fact_source_watch, 'NativeSourceWatch', Watch)
    monkeypatch.setattr(sources, 'inventory', lambda: ((), ()))
    monkeypatch.setenv('HERMES_FACTS_GATEWAY', '1')
    now = [0]
    monkeypatch.setattr(unloaded.time, 'monotonic', lambda: now[0])
    scheduler = loaded.Scheduler(clock=lambda: now[0])
    monkeypatch.setattr(loaded, '_owner', scheduler)
    schedule = unloaded.UnloadedSchedule(lambda: None, 30, native=True)
    calls = []
    try:
        schedule.run('standby', lambda: calls.append('initial'))
        scheduler.step()
        schedule.run('standby', lambda: calls.append('native'))
        assert calls == ['native']
        now[0] = 1
        state['display']['gateway_notification_f'] = False
        scheduler.gateway_sources.policy.changed()
        scheduler.step()
        schedule.run('owner', lambda: calls.append('transferred'))
        assert calls == ['native', 'transferred']
    finally:
        schedule.close()
        scheduler.close()


@pytest.fixture(autouse=True)
def native_policy_opt_in(monkeypatch, request):
    from hermes_cli import config
    monkeypatch.setenv('HERMES_FACTS_GATEWAY', '1')
    if request.node.name != 'test_real_canonical_config_write_is_a_native_live_rollback':
        monkeypatch.setattr(config, 'load_config_readonly',
                            lambda: {'display': {'gateway_notification_f': True}})
