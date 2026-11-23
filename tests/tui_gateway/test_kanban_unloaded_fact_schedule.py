"""Unloaded fact admission preserves lease maintenance and deadline custody."""
import pytest

from hermes_cli import host_loop_lease
from hermes_cli.fact_contract import Fact, FactKey
from hermes_cli.fact_fake import FakeStore
from tests.tui_gateway import test_notification_facts as helpers
from tui_gateway import kanban_unloaded as unloaded
from tui_gateway import kanban_unloaded_gates as gates
from tui_gateway import kanban_unloaded_schedule as scheduling
from tui_gateway import notification_facts
from tui_gateway.notice_fanout_result import FanoutResult


class Lease:
    holder = True
    beats = 0

    def held(self):
        return self.holder

    def exclusive(self):
        return True

    def beat(self):
        self.beats += 1


@pytest.mark.parametrize("pending", [False, True])
def test_fact_quiet_lease_beats_retry_deadline_and_rollback(monkeypatch, pending):
    store = FakeStore()
    helpers.ready_siblings(store)
    owner = notification_facts.bind(store, helpers.SCOPE, "/canonical/unloaded")
    owner.report_ready()
    store.activate(helpers.SCOPE)
    store.drain()
    now = [0.0]
    calls = []
    probes = []
    monkeypatch.setattr(scheduling.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(gates.time, "time", lambda: now[0] + 1000)
    monkeypatch.setattr(host_loop_lease, "host_loops_retired", lambda: False)
    monkeypatch.setattr(unloaded.wake_slots, "set_holder", lambda _: None)

    def probe(*a, **kw):
        probes.append(now[0])
        return gates.Inputs(pending=pending and len(probes) == 1, due=1010)

    def host(*a, **kw):
        calls.append(now[0])
        gates.run("notices", list, live=set())

    monkeypatch.setattr(gates, "probe", probe)
    monkeypatch.setattr(unloaded, "host_pass", host)
    gates.reset_for_tests()
    lease = Lease()
    schedule = scheduling.UnloadedSchedule(lambda: None, 5)
    try:
        unloaded.run_pass(lease, schedule=schedule)
        for tick in range(1, 5):
            now[0] = tick
            unloaded.run_pass(lease, schedule=schedule)
        assert calls == probes == [0]
        assert lease.beats == 5
        now[0] = 5
        unloaded.run_pass(lease, schedule=schedule)
        assert calls == ([0, 5] if pending else [0])
        now[0] = 10
        unloaded.run_pass(lease, schedule=schedule)
        assert calls[-1] == 10  # deadline without a new fact
        # An explicit rollback reuses the same executor and lease, no new owner.
        store.rollback(helpers.SCOPE)
        now[0] = 15
        unloaded.run_pass(lease, schedule=schedule)
        assert calls[-1] == 15
        assert lease.beats == 8
    finally:
        schedule.close()
        owner.close()
        gates.reset_for_tests()


def test_role_transition_and_retirement_never_use_quiet_predecessor(monkeypatch):
    store = FakeStore()
    helpers.ready_siblings(store)
    owner = notification_facts.bind(store, helpers.SCOPE, "/canonical/roles")
    owner.report_ready()
    store.activate(helpers.SCOPE)
    store.drain()
    calls = []
    retired = [False]
    monkeypatch.setattr(host_loop_lease, "host_loops_retired", lambda: retired[0])
    monkeypatch.setattr(unloaded.wake_slots, "set_holder", lambda _: None)
    monkeypatch.setattr(unloaded, "host_pass", lambda *a, **kw: calls.append("host"))
    monkeypatch.setattr(unloaded, "residual_pass", lambda: calls.append("residual"))
    lease = Lease()
    schedule = scheduling.UnloadedSchedule(lambda: None, 60)
    try:
        unloaded.run_pass(lease, schedule=schedule)
        unloaded.run_pass(lease, schedule=schedule)
        assert calls == ["host"]
        lease.holder = False
        unloaded.run_pass(lease, schedule=schedule)
        assert calls == ["host", "residual"]
        retired[0] = True
        schedule.gate.retry_at = 0.0
        schedule.gate.retry_floor = True
        store.publish([Fact(FactKey(helpers.SCOPE, "SourceState", "board"), 1)])
        store.drain()
        assert unloaded.run_pass(lease, schedule=schedule) == "retired"
        assert schedule.gate.retry_at == float("inf")
        assert calls == ["host", "residual"]
        retired[0] = False
        lease.holder = True
        unloaded.run_pass(lease, schedule=schedule)
        assert calls == ["host", "residual", "host"]
        schedule.gate.retry_at = 0.0
        schedule.gate.retry_floor = True
        owner.close()
        unloaded.run_pass(lease, schedule=schedule)
        assert schedule.gate.retry_at == float("inf")
        assert calls == ["host", "residual", "host"]  # missing binding fails closed
    finally:
        schedule.close()
        owner.close()


def test_actual_loop_keeps_lease_cadence_while_sources_are_quiet(monkeypatch):
    import threading
    store = FakeStore()
    helpers.ready_siblings(store)
    owner = notification_facts.bind(store, helpers.SCOPE, "/canonical/unloaded-loop")
    owner.report_ready()
    store.activate(helpers.SCOPE)
    store.drain()
    now = [0.0]
    calls, beats, released = [], [], []
    stop = threading.Event()

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
            if now[0] == 7:
                store.publish([Fact(FactKey(helpers.SCOPE, "SourceState", "board"), 1)])
                store.drain()
            if now[0] >= 12:
                stop.set()

    lease = Lease()
    monkeypatch.setattr(lease, "beat", lambda: beats.append(now[0]))
    monkeypatch.setattr(lease, "release", lambda: released.append(True), raising=False)
    monkeypatch.setattr(unloaded, "make_lease", lambda: lease)
    monkeypatch.setattr(unloaded.threading, "Event", Wake)
    monkeypatch.setattr(scheduling.time, "monotonic", lambda: now[0])
    monkeypatch.setenv("HERMES_KANBAN_UNLOADED_SWEEP_S", "5")
    monkeypatch.setenv("HERMES_KANBAN_UNLOADED_FIRST_S", "0")
    monkeypatch.setattr(unloaded.wake_slots, "set_holder", lambda _: None)
    monkeypatch.setattr(unloaded, "host_pass", lambda *a, **kw: calls.append(now[0]))
    try:
        unloaded._loop(stop)
        assert calls == [0, 7]
        assert beats == [0, 5, 7]
        assert released == [True]
        assert not owner.actions
    finally:
        owner.close()


@pytest.mark.parametrize("bound", [False, True])
def test_early_report_deadline_does_not_postpone_legacy_work(monkeypatch, bound):
    import threading
    from types import SimpleNamespace

    monkeypatch.setenv('HERMES_FACTS_GATEWAY', '0')  # explicitly exercise the legacy rollback path

    owner = None
    if bound:
        store = FakeStore()
        helpers.ready_siblings(store)
        owner = notification_facts.bind(store, helpers.SCOPE, "/canonical/early-deadline")
        owner.report_ready()
        store.activate(helpers.SCOPE)
        store.drain()
    now = [0.0]
    calls, released = [], []
    stop = threading.Event()

    class Wake:
        def set(self):
            pass

        def clear(self):
            pass

        def is_set(self):
            return False

        def wait(self, timeout):
            now[0] += timeout
            if now[0] >= 6.1:
                stop.set()

    def host(*args, **kwargs):
        calls.append(now[0])
        result = FanoutResult()
        result.next_deadline = now[0] + 0.5
        gates.run("card_change_pass", lambda: result, live=set())

    time_wall = [1000.0]
    lease = Lease()
    monkeypatch.setattr(lease, "release", lambda: released.append(True), raising=False)
    monkeypatch.setattr(unloaded, "make_lease", lambda: lease)
    monkeypatch.setattr(unloaded, "threading", SimpleNamespace(Event=Wake))
    monkeypatch.setattr(scheduling.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(gates.time, "time", lambda: time_wall[0] + now[0])
    monkeypatch.setenv("HERMES_KANBAN_UNLOADED_SWEEP_S", "2")
    monkeypatch.setenv("HERMES_KANBAN_UNLOADED_FIRST_S", "0")
    monkeypatch.setattr(unloaded.wake_slots, "set_holder", lambda _: None)
    monkeypatch.setattr(unloaded, "host_pass", host)
    try:
        unloaded._loop(stop)
        # Both the bound action and the unbound legacy executor run each 0.5 s
        # reported deadline, not just the 2 s interval.
        assert calls == [i / 2 for i in range(13)]
        assert released == [True]
    finally:
        if owner:
            owner.close()


def test_empty_fanout_result_is_not_a_quiet_certificate():
    from gateway.kanban_notification_schedule import PassReport
    result = FanoutResult()
    result.incomplete = True
    report = PassReport()
    with gates.scheduling(report):
        assert gates.run("card_change_pass", lambda: result, live=set()) == []
    assert report.pending
