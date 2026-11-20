"""``session.create`` refuses a concrete pin this host's managed subscription policy refuses. [daveey]

The Desktop composer started a chat pinned to ``gemini-4-argon`` on ``gemini`` on a host whose
``model_routing.enforce_subscription_only`` registry runs subscription routes only. The session was
minted, then its first turn failed "before the agent was ready" with the policy's refusal: a dead
chat. The create gate now runs the turn's own ``pin_policy`` verdict before any state exists.
"""

import copy

import pytest

from agent.model_routing_managed import allowed_pins, managed_config
from hermes_cli.config import atomic_config_write
from tests.agent.test_model_routing import policy


def _write_policy(home, *, enforced=True):
    cfg = policy()
    cfg["model"] = {"default": "anthropic-deep", "provider": "anthropic"}
    cfg["model_routing"]["enforce_subscription_only"] = enforced
    atomic_config_write(home / "config.yaml", cfg)


@pytest.fixture
def _create(monkeypatch, tmp_path):
    monkeypatch.setattr("hermes_cli.banner.prefetch_update_check", lambda: None)
    # The catalog-coherence half of the gate (#96817) has its own tests: this file judges the registry.
    monkeypatch.setattr("hermes_cli.models_validate.static_model_provider_conflict", lambda *a, **k: None)
    from tui_gateway import server

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_load_cfg", dict)
    monkeypatch.setattr(server, "_profile_home", lambda *a: None)
    monkeypatch.setattr(server, "_enable_gateway_prompts", lambda: None)
    monkeypatch.setattr(server, "_schedule_agent_build", lambda *a: None)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda: None)
    monkeypatch.setattr(server, "_register_session_cwd", lambda *a: None)
    monkeypatch.setattr(server, "_project_info_for_cwd", lambda *a: None)
    return lambda params: (server._methods["session.create"]("r1", {"cols": 80, **params}), server._sessions)


def test_unregistered_pin_is_refused_before_any_state(_create, tmp_path):
    _write_policy(tmp_path)

    response, sessions = _create({"model": "gemini-4-argon", "provider": "gemini"})

    error = response["error"]
    assert error["code"] == -32602
    allowed = allowed_pins(managed_config())
    assert "'gemini-4-argon'" in error["message"] and "'gemini'" in error["message"]
    # The Desktop toast swaps a message over 180 chars for its "Session unavailable" fallback
    # (``summarizeErrorMessage``): the reason must fit, and the allowed pins ride in ``data``.
    assert len(error["message"]) <= 180, error["message"]
    assert error["data"] == {"model": "gemini-4-argon", "provider": "gemini", "allowed": allowed}
    assert sessions == {}


def test_registered_model_on_a_provider_that_does_not_serve_it_is_refused(_create, tmp_path):
    _write_policy(tmp_path)

    response, sessions = _create({"model": "anthropic-deep", "provider": "openai-codex"})

    assert response["error"]["code"] == -32602
    assert sessions == {}


@pytest.mark.parametrize("params", [
    {"model": "anthropic-deep", "provider": "anthropic"},  # a registered pin
    {"model": "openai-codex-fast"},  # registered; no provider matches its registered one, as a turn does
    {"model": "class:deep"},  # a class selector is routed, never pinned
    {},  # no override: the profile default runs
])
def test_runnable_choices_still_mint_a_session(_create, tmp_path, params):
    _write_policy(tmp_path)

    response, sessions = _create(params)

    assert "error" not in response, response
    assert response["result"]["session_id"] in sessions


def test_policy_off_keeps_any_pin(_create, tmp_path):
    _write_policy(tmp_path, enforced=False)

    response, sessions = _create({"model": "gemini-4-argon", "provider": "gemini"})

    assert "error" not in response, response
    session = sessions[response["result"]["session_id"]]
    assert session["model_override"] == {"model": "gemini-4-argon", "provider": "gemini"}


@pytest.mark.parametrize("profile_enforced, refused", [(True, True), (False, False)])
def test_gate_reads_the_policy_of_the_profile_the_session_builds_under(
        _create, tmp_path, monkeypatch, profile_enforced, refused):
    from tui_gateway import server

    profile = tmp_path / "profiles" / "work"
    profile.mkdir(parents=True)
    _write_policy(tmp_path, enforced=not profile_enforced)  # the launch profile says the opposite
    _write_policy(profile, enforced=profile_enforced)
    monkeypatch.setattr(server, "_profile_home", lambda *a: profile)

    response, sessions = _create({"model": "gemini-4-argon", "provider": "gemini", "profile": "work"})

    assert ("error" in response) is refused, response
    assert bool(sessions) is not refused


def test_create_accepts_exactly_what_the_picker_offers(_create, tmp_path):
    """The picker's managed filter and this gate are one predicate: an offered id always runs."""
    from hermes_cli.model_options_managed import drop_refused_managed_pins

    _write_policy(tmp_path)
    rows = [{"slug": "anthropic", "models": ["anthropic-deep", "anthropic-fast", "claude-unregistered"]},
            {"slug": "openai-codex", "models": ["openai-codex-standard", "anthropic-deep"]},
            {"slug": "gemini", "models": ["gemini-4-argon"]}]
    offered = copy.deepcopy(rows)
    drop_refused_managed_pins(offered)

    assert [row["models"] for row in offered] == [["anthropic-deep", "anthropic-fast"],
                                                 ["openai-codex-standard"], []]
    for row, kept in zip(rows, offered):
        for model in row["models"]:
            response, _ = _create({"model": model, "provider": row["slug"]})
            assert ("error" not in response) is (model in kept["models"]), (row["slug"], model, response)


# --- bare ``custom`` / empty providers: the turn re-derives the provider before judging the pin ---------


def _write_routes(home, extra_routes=()):
    """The stock policy plus ``extra_routes`` (level, provider, model) subscription routes."""
    cfg = policy()
    cfg["model"] = {"default": "anthropic-deep", "provider": "anthropic"}
    cfg["model_routing"]["enforce_subscription_only"] = True
    for level, provider, model in extra_routes:
        cfg["model_routing"]["routes"][level].append(
            {**cfg["model_routing"]["routes"][level][0], "provider": provider, "model": model})
    atomic_config_write(home / "config.yaml", cfg)


@pytest.mark.parametrize("routes, params", [
    pytest.param((), {"model": "anthropic-deep", "provider": "custom"}, id="custom-on-stock-registry"),
    pytest.param((("standard", "anthropic", "anthropic-deep"),), {"model": "anthropic-deep"},
                 id="two-levels-no-provider"),
    pytest.param((("deep", "openai-codex", "anthropic-deep"),), {"model": "anthropic-deep"},
                 id="two-providers-no-provider"),
])
def test_pin_the_turn_rejects_after_rederiving_the_provider_is_refused(_create, tmp_path, routes, params):
    _write_routes(tmp_path, routes)

    response, sessions = _create(params)

    assert response["error"]["code"] == -32602, response
    assert sessions == {}


def _turn_rejects(override):
    """True when the agent build's own resolver raises the policy's refusal for the stored override."""
    from agent.model_routing_managed import ManagedPinRejectedError
    from tui_gateway import server  # the resolver runs bound to server's namespace (method_ctx.bind_module)

    try:
        server._resolve_agent_model_runtime(override, None, "agreement-session")
    except ManagedPinRejectedError:
        return True
    except Exception:  # any other failure (credentials, network) is not the pin verdict
        return False
    return False


@pytest.mark.parametrize("provider", ["anthropic", "auto", "", "custom"])
@pytest.mark.parametrize("model", ["anthropic-deep", "gemini-4-argon", "ambiguous-pin"])
def test_gate_refuses_exactly_what_the_turn_refuses_on_the_stored_override(
        _create, tmp_path, monkeypatch, model, provider):
    from hermes_cli.config_effective import load_user_config_effective
    from hermes_cli.model_options_managed import managed_pin_refusal
    from tui_gateway import server
    from tui_gateway.methods_session_model_guard import create_overrides

    _write_routes(tmp_path, (("deep", "anthropic", "ambiguous-pin"), ("fast", "anthropic", "ambiguous-pin")))
    monkeypatch.setattr(server, "_load_cfg", load_user_config_effective)  # the turn reads its config here

    stored, _, _ = create_overrides({"model": model, "provider": provider})
    response, sessions = _create({"model": model, "provider": provider})

    refused = managed_pin_refusal(stored["model"], stored["provider"]) is not None
    assert refused is _turn_rejects(stored), (stored, response)
    assert ("error" in response) is refused
    assert bool(sessions) is not refused
