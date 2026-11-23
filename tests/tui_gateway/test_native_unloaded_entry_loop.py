"""The production unloaded loop activates shared native coverage without a fact binding."""
import threading
from types import SimpleNamespace

from hermes_cli import host_loop_lease
from tests.tui_gateway import test_kanban_unloaded_fact_schedule as helpers
from tui_gateway import kanban_unloaded as unloaded
from tui_gateway import kanban_unloaded_gates as gates
from tui_gateway import kanban_unloaded_schedule as scheduling
from tui_gateway import notification_gateway_sources as sources
from tui_gateway import notification_loaded_f as loaded
from tui_gateway.notice_fanout_result import FanoutResult


def test_native_unloaded_entry_retry_lease_maintenance_and_off_deadline(monkeypatch):
    now = [0.0]
    scheduler = loaded.Scheduler(clock=lambda: now[0])
    monkeypatch.setattr(loaded, '_owner', scheduler)
    monkeypatch.setattr(sources, 'inventory', lambda: ((), ()))
    monkeypatch.setenv('HERMES_FACTS_GATEWAY', '1')
    monkeypatch.setenv('HERMES_KANBAN_UNLOADED_SWEEP_S', '5')
    monkeypatch.setenv('HERMES_KANBAN_UNLOADED_FIRST_S', '0')
    monkeypatch.setattr(scheduling.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(gates.time, 'time', lambda: 1000 + now[0])
    monkeypatch.setattr(host_loop_lease, 'host_loops_retired', lambda: False)
    monkeypatch.setattr(unloaded.wake_slots, 'set_holder', lambda _: None)
    monkeypatch.setattr(gates, 'probe', lambda *a, **kw: gates.Inputs())
    calls, beats, released = [], [], []
    stop = threading.Event()
    lease = helpers.Lease()
    monkeypatch.setattr(lease, 'beat', lambda: beats.append(now[0]))
    monkeypatch.setattr(lease, 'release', lambda: released.append(True), raising=False)
    monkeypatch.setattr(unloaded, 'make_lease', lambda: lease)

    class Wake:
        flag = False

        def set(self):
            self.flag = True

        def clear(self):
            self.flag = False

        def is_set(self):
            return self.flag

        def wait(self, timeout):
            now[0] += timeout
            if now[0] == 11:
                scheduler.gateway_sources.changed()
            if now[0] == 12:
                monkeypatch.setenv('HERMES_FACTS_GATEWAY', '0')
                self.set()  # explicit rollback observation between legacy deadlines
            if now[0] >= 18:
                stop.set()
            scheduler.step()

    def host(*args, **kwargs):
        calls.append(now[0])
        result = FanoutResult()
        if len(calls) == 1:
            result.incomplete = True
            result.next_deadline = 3
        gates.run('card_change_pass', lambda: result, live=set())

    monkeypatch.setattr(unloaded, 'threading', SimpleNamespace(Event=Wake))
    monkeypatch.setattr(unloaded, 'host_pass', host)
    gates.reset_for_tests()
    try:
        unloaded._loop(stop)
        assert calls == [1, 3, 11, 16]
        assert 0 in beats and 8 in beats  # source quietness does not stop lease maintenance
        assert released == [True]
        assert not scheduler.gateway_sources.actions
    finally:
        gates.reset_for_tests()
        scheduler.close()


def test_native_quiet_role_handoff_and_retirement(monkeypatch):
    from hermes_cli import fact_source_watch
    from tests.tui_gateway import test_gateway_native_source_lifecycle as native_helpers

    scheduler = loaded.Scheduler(clock=lambda: 0)
    monkeypatch.setattr(loaded, '_owner', scheduler)
    monkeypatch.setattr(fact_source_watch, 'NativeSourceWatch', native_helpers.Watch)
    monkeypatch.setattr(sources, 'inventory', lambda: ((), ()))
    monkeypatch.setenv('HERMES_FACTS_GATEWAY', '1')
    retired = [False]
    calls = []
    monkeypatch.setattr(host_loop_lease, 'host_loops_retired', lambda: retired[0])
    monkeypatch.setattr(unloaded.wake_slots, 'set_holder', lambda _: None)
    monkeypatch.setattr(gates, 'probe', lambda *a, **kw: gates.Inputs())
    monkeypatch.setattr(unloaded, 'host_pass', lambda *a, **kw: calls.append('host'))
    monkeypatch.setattr(unloaded, 'residual_pass', lambda: calls.append('residual'))
    lease = helpers.Lease()
    schedule = scheduling.UnloadedSchedule(lambda: None, 60, native=True)
    try:
        unloaded.run_pass(lease, schedule=schedule)
        assert calls == []
        scheduler.step()
        unloaded.run_pass(lease, schedule=schedule)
        unloaded.run_pass(lease, schedule=schedule)
        assert calls == ['host']
        lease.holder = False
        unloaded.run_pass(lease, schedule=schedule)
        assert calls == ['host', 'residual']
        retired[0] = True
        assert unloaded.run_pass(lease, schedule=schedule) == 'retired'
        assert schedule.gate.retry_at == float('inf')
        retired[0] = False
        lease.holder = True
        unloaded.run_pass(lease, schedule=schedule)
        assert calls == ['host', 'residual', 'host']
    finally:
        schedule.close()
        scheduler.close()
