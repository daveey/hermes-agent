"""Unbound legacy request custody survives until projection-owner transfer."""
import threading
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_annotation_facts as annotations
from hermes_cli.fact_contract import Scope
from hermes_cli.fact_fake import FakeStore


def binding(stop=lambda: None, start=lambda: None):
    store = FakeStore()
    handle = SimpleNamespace(v1=store, demand=lambda *args: None)
    return annotations.AnnotationFacts(handle, Scope('install', 'default'),
                                       stop_legacy=stop, start_legacy=start)


def test_activation_drains_unbound_request_before_stop_and_subscribe(monkeypatch):
    entered, release, draining = threading.Event(), threading.Event(), threading.Event()
    calls, errors = [], []
    consumer = binding(lambda: calls.append('stop'), lambda: calls.append('start'))
    wait_for = annotations._active_lock.wait_for

    def observed_wait(predicate, timeout):
        draining.set()
        return wait_for(predicate, timeout)

    monkeypatch.setattr(annotations._active_lock, 'wait_for', observed_wait)

    def legacy(rows):
        entered.set()
        assert release.wait(3)
        return rows

    def activate():
        try:
            consumer.on_mode('facts', 1)
        except BaseException as exc:
            errors.append(exc)

    reader = threading.Thread(target=lambda: annotations.annotate('goal', [], legacy))
    activator = threading.Thread(target=activate)
    reader.start()
    try:
        assert entered.wait(3)
        activator.start()
        assert draining.wait(3)
        with pytest.raises(annotations.AnnotationUnavailable):
            annotations.annotate('goal', [], lambda rows: pytest.fail('late legacy scan'))
        assert calls == []
        assert consumer._subscription is None
    finally:
        release.set()
        reader.join(3)
        if activator.ident is not None:
            activator.join(3)
        consumer.close()
    assert not reader.is_alive() and not activator.is_alive()
    assert errors == []
    assert calls == ['stop', 'start']
    assert annotations._legacy_inflight == 0


def test_drain_timeout_releases_failed_claim_without_starting_owner(monkeypatch):
    consumer = binding(lambda: pytest.fail('stop before drain'))
    monkeypatch.setattr(annotations, '_legacy_inflight', 1)
    try:
        with pytest.raises(TimeoutError, match='requests did not drain'):
            annotations._claim_facts(consumer, timeout=0)
        assert consumer not in annotations._active_bindings
        assert consumer._subscription is None
    finally:
        consumer.close()


@pytest.mark.parametrize('failure', ['stop', 'subscribe'])
def test_failed_transfer_removes_request_fence(monkeypatch, failure):
    calls = []

    def fail(*args):
        raise OSError('transfer failed')

    consumer = binding(fail if failure == 'stop' else lambda: calls.append('stop'),
                       lambda: calls.append('start'))
    if failure == 'subscribe':
        monkeypatch.setattr(consumer.v1, 'subscribe', fail)
    try:
        with pytest.raises(OSError, match='transfer failed'):
            consumer.on_mode('facts', 1)
        assert consumer not in annotations._active_bindings
        assert consumer._subscription is None
        assert annotations.annotate('goal', [], lambda rows: rows) == []
        assert calls == (['start'] if failure == 'stop' else ['stop', 'start'])
    finally:
        consumer.close()


def test_failed_rollback_keeps_custody_until_same_mode_recovery(monkeypatch):
    def fail(*args):
        raise TimeoutError('still draining')

    consumer = binding(fail, fail)
    try:
        with pytest.raises(TimeoutError):
            consumer.on_mode('facts', 1)
        assert consumer in annotations._active_bindings
        with pytest.raises(annotations.AnnotationUnavailable):
            consumer.annotate('goal', [], lambda rows: pytest.fail('legacy read'))
        with pytest.raises(annotations.AnnotationUnavailable):
            annotations.annotate('goal', [], lambda rows: pytest.fail('unbound read'))
        monkeypatch.setattr(consumer, '_start', lambda: None)
        consumer.on_mode('legacy', 2)
        assert consumer not in annotations._active_bindings
        assert consumer.annotate('goal', [], lambda rows: rows) == []
    finally:
        monkeypatch.setattr(consumer, '_start', lambda: None)
        consumer.close()


def test_partial_real_holder_stop_is_rolled_back(monkeypatch):
    from hermes_cli import kanban_annotation_host as host
    from hermes_cli import kanban_annotation_prewarm as warm

    starts = []
    monkeypatch.setattr(host, '_holder', [])
    monkeypatch.setattr(host, '_holder_suspended', False)
    monkeypatch.setattr(host, '_start_holder', lambda role: starts.append(role))

    def fail(**kwargs):
        warm._annotation_suspended = True
        raise TimeoutError('prewarm drain timed out')

    monkeypatch.setattr(warm, 'suspend_annotation_prewarm', fail)
    consumer = binding(host.suspend_holder, lambda: host.resume_holder('serve'))
    try:
        with pytest.raises(TimeoutError, match='prewarm drain'):
            consumer.on_mode('facts', 1)
        assert not host._holder_suspended
        assert not warm._annotation_suspended
        assert starts == ['serve']
        consumer.on_mode('legacy', 2)
        assert starts == ['serve']
    finally:
        consumer.close()
        warm.resume_annotation_prewarm()


def test_legacy_error_releases_request_custody():
    def fail(rows):
        raise OSError('source failed')

    with pytest.raises(OSError, match='source failed'):
        annotations.annotate('goal', [], fail)
    assert annotations._legacy_inflight == 0


@pytest.mark.parametrize('fail_stop', [False, True])
def test_rolled_back_generation_cannot_reactivate(monkeypatch, fail_stop):
    consumer = binding()
    calls = []
    def stop():
        calls.append('stop')
        if fail_stop:
            raise OSError('stop failed')
    monkeypatch.setattr(consumer, '_stop', stop)
    try:
        if fail_stop:
            with pytest.raises(OSError):
                consumer.on_mode('facts', 3)
        else:
            consumer.on_mode('facts', 3)
        consumer.on_mode('legacy', 3)
        consumer.on_mode('facts', 3)
        assert consumer._mode == 'legacy'
        assert consumer._subscription is None
        assert calls == ['stop']
        monkeypatch.setattr(consumer, '_stop', lambda: calls.append('next'))
        consumer.on_mode('facts', 4)
        assert consumer._mode == 'facts'
        assert calls == ['stop', 'next']
    finally:
        consumer.close()


@pytest.mark.parametrize('failed_mode', ['facts', 'legacy'])
def test_failed_transition_still_fences_older_generations(monkeypatch, failed_mode):
    consumer = binding()
    def fail():
        raise OSError('restart failed')
    try:
        if failed_mode == 'legacy':
            consumer.on_mode('facts', 1)
        else:
            monkeypatch.setattr(consumer, '_stop', fail)
        monkeypatch.setattr(consumer, '_start', fail)
        with pytest.raises(OSError):
            consumer.on_mode(failed_mode, 3)
        monkeypatch.setattr(consumer, '_start', lambda: None)
        monkeypatch.setattr(consumer, '_stop', lambda: None)
        consumer.on_mode('facts', 2)
        assert consumer._generation == 3
        assert consumer._mode == 'legacy'
        assert consumer._subscription is None
        assert consumer._needs_restart
        consumer.on_mode('legacy', 3)
        assert not consumer._needs_restart
    finally:
        monkeypatch.setattr(consumer, '_start', lambda: None)
        consumer.close()


def test_activation_drains_other_profile_bound_requests(monkeypatch):
    entered, release, draining = threading.Event(), threading.Event(), threading.Event()
    calls, errors = [], []
    first = binding(lambda: calls.append('stop'))
    store = FakeStore()
    second = annotations.AnnotationFacts(SimpleNamespace(v1=store), Scope('install', 'other'),
                                         stop_legacy=lambda: None, start_legacy=lambda: None)
    wait_for = annotations._active_lock.wait_for
    def observed_wait(predicate, timeout):
        draining.set()
        return wait_for(predicate, timeout)
    monkeypatch.setattr(annotations._active_lock, 'wait_for', observed_wait)
    def legacy(rows):
        entered.set()
        assert release.wait(3)
        return rows
    def read():
        try:
            with annotations.use(second):
                annotations.annotate('goal', [], legacy)
        except BaseException as exc:
            errors.append(exc)
    def activate():
        try:
            first.on_mode('facts', 1)
        except BaseException as exc:
            errors.append(exc)
    reader = threading.Thread(target=read)
    activator = threading.Thread(target=activate)
    reader.start()
    try:
        assert entered.wait(3)
        activator.start()
        assert draining.wait(3)
        assert calls == []
        assert first._subscription is None
        release.set()
        reader.join(3)
        activator.join(3)
        with annotations.use(second), pytest.raises(annotations.AnnotationUnavailable):
            annotations.annotate('goal', [], lambda rows: pytest.fail('late bound legacy scan'))
        assert calls == ['stop']
    finally:
        release.set()
        reader.join(3)
        if activator.ident is not None:
            activator.join(3)
        first.close()
        second.close()
    assert errors == []
    assert not reader.is_alive() and not activator.is_alive()
    assert annotations._legacy_inflight == 0
