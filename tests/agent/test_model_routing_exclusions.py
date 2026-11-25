"""Provider exclusions fence actual allocator eligibility and final prepare; no provider traffic."""
import pytest

from agent import credential_pool
from agent import model_routing_runtime as runtime
from agent.model_routing import ModelRoutingError
from agent.model_routing_exclusions import exclude_providers, provider_allowed
from tests.hermes_cli import test_model_routing_runtime as fixtures

routing_home = fixtures.routing_home


def test_nested_exclusions_cannot_relax_and_do_not_leak():
    assert provider_allowed('anthropic')
    with exclude_providers(['ANTHROPIC']):
        assert not provider_allowed('anthropic')
        with exclude_providers(['openai-codex']):
            assert not provider_allowed('anthropic') and not provider_allowed('openai-codex')
        assert provider_allowed('openai-codex') and not provider_allowed('anthropic')
    assert provider_allowed('anthropic')


def test_allocator_never_loads_excluded_pool(routing_home, monkeypatch):
    calls = []
    def forbidden(*a, **kw):
        calls.append(a)
        pytest.fail('excluded pool touched')
    monkeypatch.setattr(credential_pool, 'load_pool', forbidden)
    with exclude_providers(['anthropic']), pytest.raises(ModelRoutingError):
        runtime.resolve_class_runtime('class:deep', 'auto', config=routing_home,
                                      session_id='excluded-fixture')
    assert calls == []


def test_final_prepare_rejects_wrong_provider_even_if_eligibility_bypassed(routing_home, monkeypatch):
    def wrong(*a, **kw):
        kw['prepare']({'selected_provider': 'anthropic', 'selected_model': 'anthropic-deep'})
    monkeypatch.setattr(runtime, 'resolve_model_route', wrong)
    with exclude_providers(['anthropic']), pytest.raises(ModelRoutingError, match='final provider allocation'):
        runtime.resolve_class_runtime('class:deep', 'auto', config=routing_home,
                                      session_id='wrong-adapter-fixture')


def test_ordinary_allocation_and_unrefused_positive_control(routing_home):
    with exclude_providers(['openai-codex']):
        rt = runtime.resolve_class_runtime('class:deep', 'auto', config=routing_home,
                                           session_id='unrefused-fixture')
    assert rt['provider'] == 'anthropic'
