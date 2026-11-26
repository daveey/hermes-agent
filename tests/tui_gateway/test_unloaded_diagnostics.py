"""Real scheduler admission and lexical host CPU partition controls."""
import json
from types import SimpleNamespace

import pytest

from gateway.kanban_notification_schedule import PassReport
from hermes_cli import loop_accounting as la
from hermes_cli.fact_contract import Fact, FactKey
from hermes_cli.fact_fake import FakeStore
from tests.tui_gateway import test_notification_facts as helpers
from tui_gateway import kanban_unloaded as unloaded, kanban_unloaded_gates as gates
from tui_gateway import kanban_unloaded_schedule as schedule_module, notification_facts
from tui_gateway.notice_fanout_result import FanoutResult
from tui_gateway import unloaded_diagnostics as diagnostic


def metric(family):
    prefix = 'unloaded.diag.' + family + '.'
    return {k[len(prefix):]: v for k, v in la.counters().items() if k.startswith(prefix)}


@pytest.mark.parametrize(('reset', 'health', 'expected'), [(True, False, 'reset'), (False, True, 'health'),
                                                      (False, False, 'unknown')])
def test_actual_delta_sources_are_bounded(scheduler, monkeypatch, reset, health, expected):
    _store, owner, schedule, _now, _cpu = scheduler
    monkeypatch.setattr(unloaded, '_host_pass', lambda *a, **k: None)
    schedule.run('host', unloaded.host_pass)
    la.reset()
    delta = SimpleNamespace(reset=reset, health=health, upserts=(), deletes=() if reset or health else (SimpleNamespace(kind='private'),))
    owner._signal(owner.token, delta)
    schedule.run('host', unloaded.host_pass)
    assert metric('host.fact') == {expected: metric('host.fact')[expected]}
    assert next(iter(schedule.gate.actions.values())).diagnostic_sources == (expected,)
    assert metric('host.fact')[expected]['runs'] == 1


@pytest.fixture
def scheduler(monkeypatch, record_property):
    store = FakeStore()
    helpers.ready_siblings(store)
    owner = notification_facts.bind(store, helpers.SCOPE, '/canonical/r5')
    owner.report_ready()
    store.activate(helpers.SCOPE)
    store.drain()
    now, cpu = [0.0], [0.0]
    monkeypatch.setattr(schedule_module.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(la, '_thread_time', lambda: cpu[0])
    schedule = schedule_module.UnloadedSchedule(lambda: None, 30)
    la.reset()
    yield store, owner, schedule, now, cpu
    record_property('counter_snapshot', json.dumps(la.counters(), sort_keys=True))
    schedule.close()
    owner.close()
    la.reset()


@pytest.mark.parametrize(('fact', 'retry', 'due', 'maintenance', 'primary'), [
    (True, True, True, True, 'fact'),
    (False, True, True, True, 'retry.card_change_pass.top_gt_cursor'),
    (False, False, True, True, 'due.notices.unknown'),
    (False, False, False, True, 'maintenance'),
    (False, False, False, False, 'unknown'),
])
def test_scheduler_overlapping_admission_and_actual_host_cpu(scheduler, monkeypatch, fact, retry, due, maintenance, primary):
    store, _owner, schedule, now, cpu = scheduler
    monkeypatch.setattr(unloaded, '_host_pass', lambda *a, **k: None)
    schedule.run('host', unloaded.host_pass)  # consume membership catch-up
    now[0] = 40
    schedule.next_legacy = 30 if maintenance else 100
    sources = []
    if retry:
        sources.extend([('retry', 39, 'notices', 'pending'),
                        ('retry', 38, 'card_change_pass', 'top_gt_cursor')])
    if due:
        sources.append(('due', 37, 'notices', 'unknown'))
    schedule.gate.deadline_sources = tuple(sources)
    schedule.gate.retry_at = min((source[1] for source in sources), default=float('inf'))
    if fact:
        store.publish([Fact(FactKey(helpers.SCOPE, 'SourceState', 'board'), 1)])
        store.drain()
    elif not retry and not due:
        # A role transition admits a known-legacy branch, not an invented fact.
        schedule.role = 'previous'
    def work(*a, **k):
        cpu[0] += 7
        return 42  # ordinary early return is not GeneratorExit
    monkeypatch.setattr(unloaded, '_host_pass', work)
    la.reset()
    schedule.run('host', unloaded.host_pass)
    assert {k: v['runs'] for k, v in metric('host.primary').items()} == {primary: 1}
    assert metric('host.primary')[primary]['cpu_s'] == 7
    assert la.counters()['unloaded.host_pass']['cpu_s'] == 7
    assert metric('host.exit')['complete']['runs'] == 1
    flags = metric('host.due')
    for label, enabled in [('fact', fact), ('retry', retry), ('due', due), ('maintenance', maintenance)]:
        assert (label in flags) == enabled
    assert sum(row['runs'] for row in metric('host.primary').values()) == la.counters()['unloaded.host_pass']['runs']


@pytest.mark.parametrize('exception', [RuntimeError('host'), KeyboardInterrupt('host')])
def test_host_exceptions_include_cpu_and_preserve_identity(scheduler, monkeypatch, exception):
    _, _, schedule, _, cpu = scheduler
    def fail(*a, **k):
        cpu[0] += 11
        raise exception
    monkeypatch.setattr(unloaded, '_host_pass', fail)
    with pytest.raises(type(exception)) as raised:
        schedule.run('host', unloaded.host_pass)
    assert raised.value is exception
    assert sum(row['cpu_s'] for row in metric('host.primary').values()) == 11
    assert sum(row['runs'] for row in metric('host.primary').values()) == 1
    assert metric('host.exit')['exception']['runs'] == 1
    assert la.counters()['unloaded.host_pass']['error_kinds'] == {type(exception).__name__: 1}


def test_deadlines_propagate_earliest_and_ties_without_changing_behavior(scheduler):
    _, _, schedule, now, _ = scheduler
    now[0] = 50
    result = FanoutResult()
    result.retry(0, subpass='card_change_pass', reason='top_gt_cursor')
    result.retry(2, subpass='card_change_pass', reason='set_cursor_failed')
    other = FanoutResult()
    other.retry(0, subpass='card_change_pass', reason='board_error')
    result.absorb(other)
    assert result.next_deadline == 50
    assert result.retry_origin == (50, 'card_change_pass', 'board_error')
    report = PassReport()
    with gates.scheduling(report):
        assert gates.run('card_change_pass', lambda: result, live=set()) is result
    assert report.pending
    assert report.deadline == 50
    assert report.next_due(50, 30) == 50
    schedule.gate.settled(report, 50, 30)
    assert schedule.gate.retry_at == 50
    admission = diagnostic.Admission(50, deadlines=schedule.gate.deadline_sources)
    assert admission.primary == 'retry.card_change_pass.board_error'
    # Same deadline, lexical subpass first, then reason.
    tied = diagnostic.Admission(50, deadlines=[('retry', 50, 'notices', 'pending'),
                                              ('retry', 50, 'card_change_pass', 'remote_error')])
    assert tied.primary == 'retry.card_change_pass.remote_error'


def test_unknown_deadline_and_direct_host_are_explicit(scheduler, monkeypatch):
    _, _, schedule, now, _ = scheduler
    monkeypatch.setattr(unloaded, '_host_pass', lambda *a, **k: None)
    unloaded.host_pass()
    assert metric('host.primary')['unknown']['runs'] == 1
    la.reset()
    now[0] = 5
    schedule.gate.retry_at = 4
    schedule.next_legacy = 20
    schedule.run('host', unloaded.host_pass)
    assert metric('host.primary')['retry.unknown.unknown']['runs'] == 1


def test_static_label_and_registry_overflow_are_visible(monkeypatch):
    la.reset()
    diagnostic.emit('remote.call', 'private-card-identifier')
    assert metric('remote.call') == {'unknown': la.counters()['unloaded.diag.remote.call.unknown']}
    monkeypatch.setattr(la, 'MAX_NAMES', 1)
    diagnostic.emit('remote.cert', 'certified')
    assert la.counters()['_overflow']['runs'] == 1
    la.reset()


def test_actual_host_work_has_real_thread_cpu_in_the_primary_partition(monkeypatch, record_property):
    la.reset()
    monkeypatch.setattr(unloaded, '_host_pass', lambda *a, **k: sum(range(100_000)))
    unloaded.host_pass()
    counters = la.counters()
    parent = counters['unloaded.host_pass']
    primary = counters['unloaded.diag.host.primary.unknown']
    assert primary['runs'] == parent['runs'] == 1
    assert 0 < primary['cpu_s'] <= parent['cpu_s']
    assert primary['errors'] == parent['errors'] == 0
    assert counters['unloaded.diag.host.exit.complete']['runs'] == 1
    record_property('counter_snapshot', json.dumps(counters, sort_keys=True))
    la.reset()
