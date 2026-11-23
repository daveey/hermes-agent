"""Process participant control; not a distributed activation proof."""
import pytest

from hermes_cli.fact_contract import Fact, FactKey, Scope
from hermes_cli.fact_host import FactHost, acquire
from hermes_cli.fact_replica import Endpoint
from hermes_cli import fact_runtime
from hermes_cli.fact_participant import ProcessLifecycle
from hermes_cli.kanban_annotation_facts import AnnotationFacts
from tests.waiting import wait_until


@pytest.mark.platforms('linux')
def test_scoped_facade_drives_real_annotation_consumer(tmp_path_factory):
    scope = Scope('fixture', 'alpha')
    foreign = Scope('fixture', 'beta')
    token = 'runtime-fixture-only-token-000000000000'
    host = FactHost(tmp_path_factory.mktemp('runtime'), {token: scope}).start()
    raw = acquire(scope, endpoint=Endpoint(str(host.directory / 'facts.sock'), token))
    participant = ProcessLifecycle({(scope, 'annotations')})
    handle = fact_runtime.ScopedHandle(raw, participant)
    lifecycle = []
    consumer = AnnotationFacts(handle, scope, stop_legacy=lambda: lifecycle.append('stop'),
                               start_legacy=lambda: lifecycle.append('start'))
    try:
        key = FactKey(scope, 'SessionFact', 's')
        host.store.publish([Fact(key, {'annotations': {'goal': {'goal_active': True}}}, health='fresh')])
        assert wait_until(lambda: handle.v1.snapshot(scope).get(key) is not None, timeout=5)
        with pytest.raises(ValueError, match='unauthorized_scope'):
            handle.v1.snapshot(foreign)
        with pytest.raises(ValueError, match='unauthorized_scope'):
            handle.v1.report_ready('annotations', foreign)
        with pytest.raises(RuntimeError, match='not ready'):
            participant.transition('facts', 1)
        handle.v1.report_ready('annotations', scope, ready=True)
        participant.transition('facts', 2)
        assert consumer.annotate('goal', [{'id': 's'}], None) == [{'id': 's', 'goal_active': True}]
        participant.transition('legacy', 3)
        assert lifecycle == ['stop', 'start']
    finally:
        consumer.close()
        handle.close()
        host.close()


def test_failed_transition_rolls_back_every_attempt_and_invalidates_readiness():
    scope = Scope('fixture', 'alpha')
    participant = ProcessLifecycle({(scope, 'first'), (scope, 'second')})
    calls = []
    def first(mode, generation):
        calls.append(('first', mode, generation))
    def second(mode, generation):
        calls.append(('second', mode, generation))
        if mode == 'facts':
            raise TimeoutError('drain failed')
    participant.bind_consumer('first', scope, first)
    participant.bind_consumer('second', scope, second)
    for name in ('first', 'second'):
        participant.report_ready(name, scope)
    with pytest.raises(ExceptionGroup):
        participant.transition('facts', 1)
    assert calls[-2:] == [('second', 'legacy', 2), ('first', 'legacy', 2)]
    assert participant.mode == 'legacy'
    with pytest.raises(RuntimeError, match='not ready'):
        participant.transition('facts', 3)


def test_rollback_failure_retains_custody_but_attempts_all_callbacks():
    scope = Scope('fixture', 'alpha')
    participant = ProcessLifecycle({(scope, 'a'), (scope, 'b')})
    calls = []
    fail = [False]
    def a(mode, generation):
        calls.append(('a', mode))
        if fail[0] and mode == 'legacy':
            raise TimeoutError('restart failed')
    registration = participant.bind_consumer('a', scope, a)
    participant.bind_consumer('b', scope, lambda mode, generation: calls.append(('b', mode)))
    for name in ('a', 'b'):
        participant.report_ready(name, scope)
    participant.transition('facts', 1)
    fail[0] = True
    with pytest.raises(ExceptionGroup):
        participant.transition('legacy', 2)
    assert calls[-1] == ('b', 'legacy')
    assert participant.mode == 'failed'
    with pytest.raises(RuntimeError, match='rollback'):
        registration.close()
    assert not registration.closed
    fail[0] = False
    participant.transition('legacy', 3)
    registration.close()
    assert registration.closed


def test_readiness_withdrawal_during_transition_refuses_release():
    scope = Scope('fixture', 'alpha')
    participant = ProcessLifecycle({(scope, 'a')})
    def callback(mode, generation):
        if mode == 'facts':
            participant.report_ready('a', scope, ready=False)
    participant.bind_consumer('a', scope, callback)
    participant.report_ready('a', scope)
    with pytest.raises(ExceptionGroup):
        participant.transition('facts', 1)
    assert participant.mode == 'legacy'


def test_binding_failure_retains_cleanup_custody():
    scope = Scope('fixture', 'alpha')
    participant = ProcessLifecycle({(scope, 'a')})
    fail = [True]
    def callback(mode, generation):
        if fail[0]:
            raise TimeoutError('partial startup')
    with pytest.raises(TimeoutError):
        participant.bind_consumer('a', scope, callback)
    with pytest.raises(RuntimeError, match='owned'):
        participant.bind_consumer('a', scope, callback)
    assert participant.mode == 'failed'
    fail[0] = False
    participant.transition('legacy', 1)


def test_handle_close_refuses_live_bindings():
    from types import SimpleNamespace
    scope = Scope('fixture', 'alpha')
    closed = []
    participant = ProcessLifecycle({(scope, 'a')})
    handle = fact_runtime.ScopedHandle(SimpleNamespace(scope=scope, close=lambda: closed.append(True)), participant)
    registration = handle.v1.bind_consumer('a', scope, lambda *args: None)
    with pytest.raises(RuntimeError, match='bindings'):
        handle.close()
    assert not closed
    registration.close()
    handle.close()
    handle.close()
    assert closed == [True]
    with pytest.raises(RuntimeError, match='closed'):
        handle.v1.snapshot(scope)


def test_reentrant_transition_cannot_regress_generation():
    scope = Scope('fixture', 'alpha')
    participant = ProcessLifecycle({(scope, 'a')})
    def callback(mode, generation):
        if mode == 'facts':
            participant.transition('legacy', generation + 1)
            participant.report_ready('a', scope)
    participant.bind_consumer('a', scope, callback)
    participant.report_ready('a', scope)
    with pytest.raises(ExceptionGroup):
        participant.transition('facts', 1)
    assert participant.mode == 'legacy'
    assert participant.generation == 2


def test_retired_participant_slot_cannot_reuse_unfenced_readiness_api():
    # v1 reports identify scope/name, not an incarnation. Rebinding therefore
    # requires a NEW participant capability, never recycling an old slot.
    scope = Scope('fixture', 'alpha')
    participant = ProcessLifecycle({(scope, 'a')})
    registration = participant.bind_consumer('a', scope, lambda *args: None)
    registration.close()
    with pytest.raises(ValueError, match='retired'):
        participant.bind_consumer('a', scope, lambda *args: None)
    with pytest.raises(ValueError, match='not bound'):
        participant.report_ready('a', scope)


def test_close_blocks_reentrant_binding_and_failed_close_is_retryable():
    from types import SimpleNamespace
    scope = Scope('fixture', 'alpha')
    participant = ProcessLifecycle({(scope, 'a')})
    attempts = []
    def close():
        attempts.append(True)
        with pytest.raises(RuntimeError, match='clos'):
            handle.v1.bind_consumer('a', scope, lambda *args: None)
        if len(attempts) == 1:
            raise TimeoutError('replica drain')
    handle = fact_runtime.ScopedHandle(SimpleNamespace(scope=scope, close=close), participant)
    with pytest.raises(TimeoutError):
        handle.close()
    with pytest.raises(RuntimeError, match='clos'):
        handle.demand('sessions', {})
    handle.close()
    assert len(attempts) == 2


def test_lifecycle_callbacks_keep_binding_context_not_control_thread_context():
    from contextvars import ContextVar
    current = ContextVar('fixture_profile', default='wrong')
    scope = Scope('fixture', 'alpha')
    participant = ProcessLifecycle({(scope, 'a')})
    seen = []
    token = current.set('alpha')
    participant.bind_consumer('a', scope, lambda *args: seen.append(current.get()))
    current.reset(token)
    participant.report_ready('a', scope)
    participant.transition('facts', 1)
    participant.transition('legacy', 2)
    assert seen == ['alpha', 'alpha', 'alpha']


def test_callback_base_exception_keeps_retryable_failed_state():
    scope = Scope('fixture', 'alpha')
    participant = ProcessLifecycle({(scope, 'a')})
    def callback(mode, generation):
        if mode == 'facts':
            raise SystemExit('fixture interrupt')
    participant.bind_consumer('a', scope, callback)
    participant.report_ready('a', scope)
    with pytest.raises(SystemExit):
        participant.transition('facts', 1)
    assert participant.mode == 'failed'
    participant.transition('legacy', 2)
    assert participant.mode == 'legacy'


def test_subscription_callback_preserves_authorized_binding_context():
    from contextvars import ContextVar
    from types import SimpleNamespace
    current = ContextVar('subscription_profile', default='wrong')
    scope = Scope('fixture', 'alpha')
    participant = ProcessLifecycle({(scope, 'a')})
    callbacks = []
    def subscribe(scope, kinds, keys, after, callback):
        callbacks.append(callback)
        return object()
    raw = SimpleNamespace(scope=scope, v1=SimpleNamespace(subscribe=subscribe))
    handle = fact_runtime.ScopedHandle(raw, participant)
    seen = []
    token = current.set('alpha')
    handle.v1.subscribe(scope, (), None, None, lambda delta: seen.append(current.get()))
    current.reset(token)
    callbacks[0](None)
    assert seen == ['alpha']
