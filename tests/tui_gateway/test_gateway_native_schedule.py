"""Host executors share loaded-f's source owner without a generic facts binding."""
import builtins
import sqlite3
import threading

import pytest

from gateway.kanban_notification_schedule import BoundActions, PassReport
from tui_gateway import notification_loaded_f as loaded

pytestmark = pytest.mark.platforms('linux')


def test_native_gate_catchup_quiet_retry_census_and_off(monkeypatch):
    from tui_gateway import notification_gateway_sources as sources
    now = [0.0]
    scheduler = loaded.Scheduler(clock=lambda: now[0])
    monkeypatch.setattr(loaded, '_owner', scheduler)
    monkeypatch.setattr(sources, 'inventory', lambda: ((), ()))
    monkeypatch.setenv('HERMES_FACTS_GATEWAY', '1')
    gate = BoundActions(lambda: None, native=True)
    try:
        assert not gate.due(0, legacy_due=True)  # waits until native coverage is armed
        scheduler.step()
        assert gate.due(0, legacy_due=False)
        gate.settled(PassReport(), 0, 5)
        with monkeypatch.context() as patch:
            def forbidden(*args, **kwargs):
                pytest.fail('quiet gate read a source')
            patch.setattr(builtins, 'open', forbidden)
            patch.setattr(sqlite3, 'connect', forbidden)
            assert not gate.due(1, legacy_due=True)
        scheduler.gateway_sources.changed()
        scheduler.step()
        assert gate.due(1, legacy_due=False)
        gate.settled(PassReport(pending=True), 1, 5)
        scheduler.gateway_sources.changed()
        scheduler.step()
        assert not gate.due(2, legacy_due=True)
        assert gate.due(6, legacy_due=False)
        gate.settled(PassReport(), 6, 5)
        now[0] = 30
        scheduler.step()  # missed native hint repairs on the same process census
        assert gate.due(30, legacy_due=False)
        monkeypatch.setenv('HERMES_FACTS_GATEWAY', '0')
        assert gate.due(31, legacy_due=True)
        assert not scheduler.gateway_sources.actions
        assert not gate.due(31, legacy_due=False)
    finally:
        gate.close()
        scheduler.close()


def test_native_source_commit_wakes_same_owner(monkeypatch, tmp_path):
    from tui_gateway import notification_gateway_sources as sources
    path = tmp_path / 'hub.db'
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE replica (value TEXT)')
    monkeypatch.setattr(sources, 'inventory', lambda: ((path,), ()))
    scheduler = loaded.Scheduler()
    monkeypatch.setattr(loaded, '_owner', scheduler)
    monkeypatch.setenv('HERMES_FACTS_GATEWAY', '1')
    wake = threading.Event()
    gate = BoundActions(wake.set, native=True)
    try:
        assert not gate.due(0, legacy_due=True)  # waits until native coverage is armed
        scheduler.step()
        assert gate.due(0, legacy_due=False)
        gate.settled(PassReport(), 0, 5)
        wake.clear()
        scheduler.event.clear()
        with sqlite3.connect(path) as conn:
            conn.execute("INSERT INTO replica VALUES ('remote-only terminal/link')")
        assert scheduler.event.wait(5)
        scheduler.step()
        assert wake.wait(5)
        assert gate.due(1, legacy_due=False)
    finally:
        gate.close()
        scheduler.close()


def test_failed_scope_inventory_quiet_recovery_uses_one_shared_retry(monkeypatch):
    from tui_gateway import notification_gateway_sources as sources
    now = [0.0]
    scheduler = loaded.Scheduler(clock=lambda: now[0])
    monkeypatch.setattr(loaded, '_owner', scheduler)
    monkeypatch.setenv('HERMES_FACTS_GATEWAY', '1')
    attempts = []

    def inventory():
        attempts.append(now[0])
        if len(attempts) == 1:
            raise OSError('temporary profile inventory refusal')
        return (), ()

    monkeypatch.setattr(sources, 'inventory', inventory)
    gates = [BoundActions(lambda: None, native=True) for _ in range(3)]
    try:
        assert not any(gate.due(0, legacy_due=True) for gate in gates)
        scheduler.step()
        assert not scheduler.gateway_sources.ready
        assert all(gate.due(0, legacy_due=False) for gate in gates)
        now[0] = 0.5
        scheduler.step()
        assert attempts == [0]
        now[0] = 1
        scheduler.step()
        assert scheduler.gateway_sources.ready
        assert attempts == [0, 1]
        now[0] = 30
        scheduler.step()
        assert attempts == [0, 1, 30]  # one census, not one per subscriber
    finally:
        for gate in gates:
            gate.close()
        scheduler.close()


@pytest.fixture(autouse=True)
def native_policy_opt_in(monkeypatch):
    from hermes_cli import config
    monkeypatch.setenv('HERMES_FACTS_GATEWAY', '1')
    monkeypatch.setattr(config, 'load_config_readonly',
                        lambda: {'display': {'gateway_notification_f': True}})
