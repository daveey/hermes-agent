"""Static vocabulary and compatibility boundaries supplement composed path tests."""
from types import SimpleNamespace

import pytest

from gateway.kanban_notification_schedule import PassReport
from gateway.notification_retry_provenance import earliest, origin
from hermes_cli import loop_accounting as la
from tui_gateway import unloaded_diagnostics as d
from tui_gateway.notice_fanout_result import FanoutResult


@pytest.fixture(autouse=True)
def reset():
    la.reset()
    yield
    la.reset()


@pytest.mark.parametrize('source', ['health', 'TaskFact', 'SourceState', 'SessionFact', 'ChatWaitingFact', 'reset'])
def test_known_fact_kinds_are_distinct_primary_fact_sources(source):
    admission = d.Admission(10, sources=[source, 'unknown'], deadlines=[('retry', 1, 'notices', 'pending')])
    with admission:
        assert d.host_accounted(lambda: 5)() == 5
    counters = la.counters()
    assert counters['unloaded.diag.host.primary.fact']['runs'] == 1
    assert counters['unloaded.diag.host.fact.' + source]['runs'] == 1
    assert counters['unloaded.diag.host.fact.unknown']['runs'] == 1
    assert counters['unloaded.diag.host.due.fact']['runs'] == 1
    assert counters['unloaded.diag.host.due.retry']['runs'] == 1


def test_unknown_fact_and_future_deadline_do_not_invent_fact_or_due_admission():
    admission = d.Admission(10, sources=['private-id'], deadlines=[('due', 11, 'notices', 'pending')])
    assert admission.primary == 'unknown'
    assert admission.facts == ('unknown',)
    assert admission.flags == {'unknown'}
    with admission:
        d.host_accounted(lambda: None)()
    assert 'unloaded.diag.host.primary.unknown' in la.counters()
    assert not any('private-id' in key for key in la.counters())


def test_nested_contexts_and_unobserved_fallback_are_isolated():
    d.fallback_reason('outside')
    with d.Fallback() as outer:
        d.fallback_reason('woken')
        with d.Fallback() as inner:
            d.fallback_reason('other_pending', 'unsettled')
        assert (inner.reason, inner.detail) == ('other_pending', 'unsettled')
        assert (outer.reason, outer.detail) == ('woken', 'unknown')
    with d.Admission(0, sources=['health']):
        with d.Admission(0, maintenance=True):
            d.host_accounted(lambda: None)()
        d.host_accounted(lambda: None)()
    d.host_accounted(lambda: None)()
    for label in ('maintenance', 'fact', 'unknown'):
        assert la.counters()['unloaded.diag.host.primary.' + label]['runs'] == 1


def test_unknown_labels_cannot_expand_registry_or_fabricate_remote_call():
    d.emit('private-family', 'private-label')
    d.emit('host.due', 'private-label')
    assert set(la.counters()) == {'unloaded.diag.instrumentation.unknown', 'unloaded.diag.host.due.unknown'}


def test_default_stage_unknowns_and_chat_exception_conserve():
    with d.RemoteCall():
        pass
    with d.Chat():
        pass
    with pytest.raises(KeyboardInterrupt):
        with d.Chat():
            raise KeyboardInterrupt()
    counters = la.counters()
    for suffix in ('remote.call.unknown', 'remote.cert.not_attempted', 'remote.health.not_reached'):
        assert counters['unloaded.diag.' + suffix]['runs'] == 1
    assert counters['unloaded.diag.remote.chat.result.unknown']['runs'] == 1
    assert counters['unloaded.diag.remote.chat.result.exception']['runs'] == 1
    assert counters['unloaded.diag.remote.chat.trigger.unknown']['runs'] == 2


def test_old_report_and_result_contracts_remain_callable():
    calls = []
    old = SimpleNamespace(retry=lambda: calls.append('retry'), due_at=lambda deadline: calls.append(deadline))
    d.retry_report(old, 'notices', 'pending')
    d.due_report(old, 12, 'notices', 'pending', cause='retry')
    assert calls == ['retry', 12]
    report = PassReport()
    d.retry_report(report, 'notices', 'pending')
    d.retry_report(report, 'card_change_pass', 'remote_error')
    d.due_report(report, 8, 'notices', 'pending')
    d.due_report(report, 7, 'notices', 'pending', cause='retry')
    assert report.retry_origin == ('card_change_pass', 'remote_error')
    assert report.due_deadline == (8, 'notices', 'pending')
    assert report.retry_deadline == (7, 'notices', 'pending')
    result = FanoutResult()
    result += [('session', 1)]
    result.absorb(FanoutResult())
    assert list(result) == [('session', 1)]
    assert result.retry_origin is None
    assert origin('private-id', 'private-id') == ('unknown', 'unknown')
    assert earliest((1, 'notices', 'pending'), 2, 'card_change_pass', 'remote_error') == (1, 'notices', 'pending')
