"""Memory-only scope activation and failed-drain custody. [daveey]"""
from contextvars import ContextVar
import pytest
from hermes_cli.fact_scope_activation import ScopeActivation
from hermes_cli.fact_capture import CaptureBudget


def test_want_is_memory_only_deduplicated_scoped_and_context_bound():
    profile = ContextVar('activation-profile', default='foreign')
    seen = []
    source = ScopeActivation(('allowed',), lambda scope: seen.append((scope, profile.get())))
    source.watch(lambda: None)
    token = profile.set('authorized')
    try:
        source.want('allowed')
        source.want('allowed')
        source.want('foreign')
    finally:
        profile.reset(token)
    assert seen == []
    source.capture(None, CaptureBudget())
    source.capture(None, CaptureBudget())
    assert seen == [('allowed', 'authorized')]
    source.close()
    source.want('allowed')
    assert source.capture(None, CaptureBudget()).next_deadline is None


def test_failed_activation_is_not_replaced_by_another_request():
    seen = []
    def fail(scope):
        seen.append(scope)
        raise RuntimeError('partial custody retained')
    source = ScopeActivation(('allowed',), fail)
    source.watch(lambda: None)
    source.want('allowed')
    with pytest.raises(RuntimeError, match='custody retained'):
        source.capture(None, CaptureBudget())
    source.want('allowed')
    source.capture(None, CaptureBudget())
    assert seen == ['allowed']
    source.close()
