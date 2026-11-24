"""Unmasked lifecycle and deadline clauses for the native host adapter."""
import logging
import pytest


from gateway.kanban_notification_schedule import BoundActions, PassReport
from tui_gateway import notification_gateway_sources as sources
from tests.tui_gateway import test_gateway_native_source_lifecycle as helpers

owner = helpers.owner


def test_closed_source_action_cannot_consume_pending_hint(owner):
    action = owner.add(lambda: None)
    owner.step(0, reconcile=True)
    action.close()
    assert not action.due(legacy_due=True)


def test_close_releases_native_watch(owner):
    owner.add(lambda: None)
    owner.step(0, reconcile=True)
    watch = owner.watch
    owner.close()
    assert watch.closed


def test_hint_reuses_inventory_and_ready_log_only_on_transition(owner, monkeypatch, caplog):
    captures = []
    inventory = sources.inventory

    def capture():
        captures.append(1)
        return inventory()

    monkeypatch.setattr(sources, 'inventory', capture)
    caplog.set_level(logging.INFO, logger=sources.__name__)
    owner.add(lambda: None)
    owner.step(0, reconcile=True)
    assert sum('native sources ready' in record.message for record in caplog.records) == 1
    owner.changed()
    owner.step(1, reconcile=False)
    assert captures == [1]
    owner.step(30, reconcile=True)
    assert captures == [1, 1]
    assert sum('native sources ready' in record.message for record in caplog.records) == 1


def test_off_retains_refusal_floor_and_earlier_deadline(monkeypatch):
    monkeypatch.setenv('HERMES_FACTS_GATEWAY', '0')
    gate = BoundActions(lambda: None, native=True)
    try:
        gate.settled(PassReport(pending=True), 0, 5)
        assert not gate.due(1, legacy_due=True)
        assert gate.due(5, legacy_due=False)
        gate.settled(PassReport(deadline=6), 5, 5)
        assert gate.due(6, legacy_due=False)
        assert gate.legacy
    finally:
        gate.close()


def test_legacy_role_change_catches_up_before_timer(monkeypatch):
    from tui_gateway import kanban_unloaded_schedule as module
    monkeypatch.setenv('HERMES_FACTS_GATEWAY', '0')
    now = [0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: now[0])
    schedule = module.UnloadedSchedule(lambda: None, 30)
    calls = []
    try:
        schedule.run('standby', lambda: calls.append('first'))
        now[0] = 1
        schedule.run('owner', lambda: calls.append('successor'))
        assert calls == ['first', 'successor']
    finally:
        schedule.close()


def test_inventory_contains_each_board_and_default(monkeypatch, tmp_path):
    from hermes_cli import kanban_db
    from tui_gateway import kanban_unloaded_gates
    monkeypatch.setattr(kanban_unloaded_gates, '_profile_paths', tuple)
    monkeypatch.setattr(kanban_db, 'list_boards', lambda **kw: [{'slug': 'other'}, {'slug': ''}])
    monkeypatch.setattr(kanban_db, 'kanban_db_path', lambda board: tmp_path / (board + '.db'))
    paths, _ = sources.inventory()
    assert tmp_path / 'other.db' in paths
    assert tmp_path / (kanban_db.DEFAULT_BOARD + '.db') in paths


def test_source_action_close_wakes_owner(owner):
    action = owner.add(lambda: None)
    owner.event.clear()
    action.close()
    assert owner.event.is_set()
    assert action not in owner.actions


def test_first_gateway_registration_starts_one_shared_owner(owner, monkeypatch):
    from tui_gateway import notification_loaded_f as loaded
    starts = []
    monkeypatch.setattr(loaded, '_owner', None)
    monkeypatch.setattr(loaded.Scheduler, 'start', lambda self: starts.append(self))
    first = loaded.register_gateway(lambda: None)
    second = loaded.register_gateway(lambda: None)
    scheduler = loaded._owner
    try:
        assert starts == [scheduler]
        assert first.owner is second.owner is scheduler.gateway_sources
        first.owner.step(0, reconcile=False)
        assert scheduler.gateway_sources.watch is not None
        watch = scheduler.gateway_sources.watch
        scheduler.close()
        assert watch.closed
    finally:
        first.close()
        second.close()
        scheduler.close()


@pytest.mark.parametrize('loaded_active,gateway_present,gateway_active,expected', [
    (True, False, False, 30),
    (False, False, False, None),
    (False, True, True, 1),
    (False, True, False, None),
    (True, True, False, 30),
])
def test_real_owner_loop_wait_uses_active_obligations_only(monkeypatch, loaded_active,
                                                          gateway_present, gateway_active, expected):
    from types import SimpleNamespace
    from tui_gateway import notification_loaded_f as loaded
    scheduler = loaded.Scheduler(clock=lambda: 0)
    scheduler.actions = {object()} if loaded_active else set()
    scheduler.gateway_sources = (SimpleNamespace(actions={object()} if gateway_active else set(), retry_at=1)
                                 if gateway_present else None)
    scheduler.next_reconcile = 30
    scheduler.dirty.clear()
    monkeypatch.setattr(scheduler, 'step', lambda: None)
    waits = []

    class Event:
        def clear(self):
            pass

        def wait(self, delay):
            waits.append(delay)
            scheduler.stop.set()

    scheduler.event = Event()
    loaded._run(scheduler)
    assert waits == [expected]


def test_loaded_and_gateway_share_census_without_suppressing_loaded_capture(owner, monkeypatch):
    from tui_gateway import notification_loaded_f as loaded, notification_loaded_local
    now = [0]
    scheduler = loaded.Scheduler(clock=lambda: now[0])
    scheduler.gateway_sources = owner
    owner.add(lambda: None)
    captures = []
    monkeypatch.setattr(scheduler, '_changed_keys', lambda: ())
    monkeypatch.setattr(scheduler, '_capture', lambda *args: ())
    monkeypatch.setattr(scheduler, '_remember', lambda *args: None)

    def capture(group):
        captures.append(now[0])
        return {}, {}

    monkeypatch.setattr(notification_loaded_local, 'capture', capture)
    action = scheduler.add({'session_key': 'loaded-chat', 'profile_home': None}, 'default', lambda: None)
    try:
        scheduler.step()
        assert captures == [0]
        assert action.due(legacy_due=False)
        now[0] = 29
        scheduler.step()
        assert captures == [0]
        now[0] = 30
        scheduler.step()
        assert captures == [0, 30]
        assert action.due(legacy_due=False)
    finally:
        action.close()
        scheduler.close()


def test_retired_legacy_loop_does_not_spin_on_unset_legacy_deadline(monkeypatch):
    import threading
    from types import SimpleNamespace
    from tui_gateway import kanban_unloaded as unloaded

    stop = threading.Event()
    waits = []
    monkeypatch.setenv('HERMES_KANBAN_UNLOADED_FIRST_S', '0')
    monkeypatch.setenv('HERMES_FACTS_GATEWAY', '0')
    monkeypatch.setattr(unloaded, 'make_lease', lambda: SimpleNamespace(release=lambda: None))

    def retired(lease, grace, *, schedule):
        schedule.role = 'retired'

    class Wake:
        def set(self):
            pass

        def clear(self):
            pass

        def is_set(self):
            return False

        def wait(self, timeout):
            waits.append(timeout)
            stop.set()

    monkeypatch.setattr(unloaded, 'run_pass', retired)
    monkeypatch.setattr(unloaded, 'threading', SimpleNamespace(Event=Wake))
    unloaded._loop(stop)
    assert waits == [1.0]


@pytest.fixture(autouse=True)
def native_policy_opt_in(monkeypatch):
    from hermes_cli import config
    monkeypatch.setenv('HERMES_FACTS_GATEWAY', '1')
    monkeypatch.setattr(config, 'load_config_readonly',
                        lambda: {'display': {'gateway_notification_f': True}})
