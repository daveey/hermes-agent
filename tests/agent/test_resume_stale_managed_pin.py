"""A resumed chat whose stored model is not an approved subscription route resumes on the profile default.

With ``enforce_subscription_only`` on, every resume adapter re-requested the session row's stored concrete tuple as
a pin. A chat saved on a retired or unregistered model (``claude-opus-4-6``, an aggregator slug) or on a paid route
therefore failed closed at agent build on every attempt and could never be reopened (hub, 10-28 00:07 PDT:
"agent build failed ... rejects unregistered or ambiguous concrete pin"). The stored tuple is history, not a new
request: each adapter now drops it and the session runs on the configured default. An approved pin still restores
exactly, nothing changes with the policy off, and an explicit request for that model still refuses.
"""
import json

import pytest

from agent.model_routing import ModelRoutingError
from agent.model_routing_restore import stale_managed_pin
from hermes_cli.config import atomic_config_write
from tests.hermes_cli import test_model_routing_runtime as runtime_fixtures

routing_home = runtime_fixtures.routing_home

APPROVED = ("anthropic-deep", "anthropic")
RETIRED = ("claude-opus-4-6", "anthropic")
PAID = ("anthropic-deep", "openrouter")


@pytest.fixture
def enforced(routing_home, tmp_path):
    routing_home["model_routing"]["enforce_subscription_only"] = True
    atomic_config_write(tmp_path / "config.yaml", routing_home)
    return routing_home


def row(model, provider):
    return {"model": model, "model_config": json.dumps({"provider": provider}), "billing_provider": provider,
            "title": "ASG runners Opus takeover"}


def test_only_an_unapproved_stored_tuple_is_stale_under_enforcement(enforced):
    assert stale_managed_pin(*RETIRED)
    assert stale_managed_pin(*PAID)
    assert not stale_managed_pin(*APPROVED)
    assert not stale_managed_pin("class:deep", "auto")
    assert not stale_managed_pin("", None)
    assert not stale_managed_pin(None, None)


def test_nothing_is_stale_with_the_policy_off(routing_home):
    assert not stale_managed_pin(*RETIRED)
    assert not stale_managed_pin(*PAID)


def test_desktop_resume_of_a_stale_model_follows_the_profile(enforced):
    from tui_gateway.server import _stored_session_runtime_overrides
    assert _stored_session_runtime_overrides(row(*RETIRED)) == {}
    assert _stored_session_runtime_overrides(row(*PAID)) == {}
    kept = _stored_session_runtime_overrides(row(*APPROVED))
    assert (kept["model_override"]["model"], kept["provider_override"]) == APPROVED


def test_desktop_resume_is_unchanged_with_the_policy_off(routing_home):
    from tui_gateway.server import _stored_session_runtime_overrides
    kept = _stored_session_runtime_overrides(row(*RETIRED))
    assert (kept["model_override"]["model"], kept["provider_override"]) == RETIRED


def test_cli_resume_of_a_stale_model_keeps_the_launch_default(enforced):
    from tests.hermes_cli.test_resume_model_restore import _make_stub
    stub = _make_stub(model="class:deep", provider="auto", requested_provider="auto", base_url="", api_key="k")
    stub._restore_session_model(row(*RETIRED))
    assert (stub.model, stub.provider) == ("class:deep", "auto")
    stub._restore_session_model(row(*APPROVED))
    assert (stub.model, stub.provider) == APPROVED


def test_api_session_resume_of_a_stale_model_follows_the_default(enforced):
    from gateway.platforms.api_server import APIServerAdapter
    adapter = object.__new__(APIServerAdapter)
    adapter._model_name = "hermes-agent"
    assert adapter._stored_session_model(row(*RETIRED)) is None
    assert adapter._stored_session_model(row(*PAID)) is None
    assert adapter._stored_session_model(row(*APPROVED)) == "anthropic-deep"


def test_an_explicit_request_for_a_stale_model_still_refuses(enforced):
    from hermes_cli.runtime_provider import resolve_runtime_provider
    with pytest.raises(ModelRoutingError, match="rejects"):
        resolve_runtime_provider(requested="anthropic", target_model="claude-opus-4-6", routing_session_id="s")


def pin(model, provider):
    return {"model_routing": {"exact_pin": True, "selected_model": model, "selected_provider": provider}}


def test_a_persisted_exact_pin_the_policy_no_longer_approves_is_not_restored(enforced):
    from agent.model_routing_restore import restore_route
    assert restore_route(RETIRED[0], json.dumps(pin(*RETIRED))) is None
    assert restore_route(PAID[0], pin(*PAID)) is None
    assert restore_route(APPROVED[0], pin(*APPROVED)) == APPROVED


def test_messaging_gateway_resume_drops_a_stale_exact_pin(enforced):
    from types import SimpleNamespace
    from gateway.run_class_routing import stored_route
    rows = {"old": {"model": RETIRED[0], "model_config": json.dumps(pin(*RETIRED))},
            "ok": {"model": APPROVED[0], "model_config": json.dumps(pin(*APPROVED))}}
    entries = {key: SimpleNamespace(session_id=key) for key in rows}
    runner = SimpleNamespace(session_store=SimpleNamespace(_entries=entries),
                             _session_db=SimpleNamespace(get_session=rows.get))
    assert stored_route(runner, "old") is None
    assert stored_route(runner, "ok") == APPROVED
