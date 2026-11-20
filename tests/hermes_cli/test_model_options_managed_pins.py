"""``model.options`` offers only pins this host's managed subscription policy can run. [daveey]

On a host with ``model_routing.enforce_subscription_only`` the Desktop picker listed every
authenticated catalog (201 ids on the hub, 9 of them runnable), so choosing ``gemini-4-argon``
started a chat whose every turn the policy refused. Rows now drop the ids ``pin_policy`` refuses on
their provider; class selectors and hosts without the policy are unchanged.
"""

import copy
import types
from unittest.mock import Mock

import pytest

from hermes_cli import inventory
from hermes_cli.config import atomic_config_write
from tests.agent.test_model_routing import policy

ROWS = [
    {"slug": "anthropic", "models": ["anthropic-deep", "claude-unregistered", "class:deep"], "total_models": 3,
     "featured_models": ["claude-unregistered", "anthropic-deep"], "is_current": True},
    # a registered id is refused on a provider whose route does not serve it
    {"slug": "openai-codex", "models": ["openai-codex-fast", "anthropic-deep"], "total_models": 40},
    {"slug": "gemini", "models": ["gemini-4-argon"], "total_models": 20, "unavailable_models": ["gemini-4-argon"]},
    # the metered API row also names the ids it shares with the subscription row
    {"slug": "openai", "billing": "metered", "models": ["openai-codex-fast"], "total_models": 1,
     "subscription_overlap_models": ["openai-codex-fast"]},
]
OFFERED = [
    {"slug": "anthropic", "models": ["anthropic-deep", "class:deep"], "total_models": 2,
     "featured_models": ["anthropic-deep"], "is_current": True},
    {"slug": "openai-codex", "models": ["openai-codex-fast"], "total_models": 1},
    {"slug": "gemini", "models": [], "total_models": 0, "unavailable_models": []},
    {"slug": "openai", "billing": "metered", "models": [], "total_models": 0, "subscription_overlap_models": []},
]


def _config(enforced=True):
    cfg = policy()
    cfg["model"] = {"default": "anthropic-deep", "provider": "anthropic"}
    cfg["model_routing"]["enforce_subscription_only"] = enforced
    return cfg


def _ctx():
    return inventory.ConfigContext(current_provider="anthropic", current_model="anthropic-deep",
                                   current_base_url="", user_providers={}, custom_providers=[])


@pytest.mark.parametrize("enforced, expected", [(True, OFFERED), (False, ROWS)])
def test_rows_keep_only_runnable_pins(enforced, expected):
    from hermes_cli.model_options_managed import drop_refused_managed_pins

    rows = copy.deepcopy(ROWS)
    drop_refused_managed_pins(rows, _config(enforced))
    assert rows == expected


def test_a_broken_policy_leaves_the_picker_to_the_turn_s_error(caplog):
    from hermes_cli.model_options_managed import drop_refused_managed_pins

    cfg = _config()
    cfg["model_routing"]["routes"]["deep"][0]["billing"] = "bogus"
    rows = copy.deepcopy(ROWS)
    drop_refused_managed_pins(rows, cfg)
    assert rows == ROWS
    assert "model_routing" in caplog.text


def test_shapes_it_cannot_judge_pass_through():
    """A non-row entry, a non-string id and a ``None`` list neither crash the picker nor change."""
    from hermes_cli.model_options_managed import drop_refused_managed_pins

    rows = [None, {"slug": "gemini", "models": [{"id": "gemini-4-argon"}, "gemini-4-argon"], "featured_models": None}]
    drop_refused_managed_pins(rows, _config())
    assert rows == [None, {"slug": "gemini", "models": [{"id": "gemini-4-argon"}], "featured_models": None,
                           "total_models": 1}]


@pytest.fixture
def managed_home(monkeypatch):
    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    atomic_config_write(home / "config.yaml", _config())
    payload = {"providers": ROWS, "model": "anthropic-deep", "provider": "anthropic"}
    monkeypatch.setattr(inventory, "_build_model_options_payload_uncached", lambda *a, **k: copy.deepcopy(payload))
    monkeypatch.setattr(inventory, "_MEMO_ENABLED", True)
    inventory.reset_model_options_memo()
    yield home
    inventory.reset_model_options_memo()


def test_model_options_payload_offers_only_runnable_pins_on_every_open(managed_home):
    first = inventory.build_model_options_payload(_ctx())
    again = inventory.build_model_options_payload(_ctx())  # a memo hit is narrowed too
    refreshed = inventory.build_model_options_payload(_ctx(), refresh=True)

    assert first["providers"] == again["providers"] == refreshed["providers"] == OFFERED
    assert (first["model"], first["provider"]) == ("anthropic-deep", "anthropic")


def test_a_policy_edit_reaches_a_memo_hit(managed_home, monkeypatch):
    """The memo keeps the unnarrowed payload (narrowing a hit never narrows the cached entry), so a
    policy edit shows on the next open without a rebuild, even when nothing in the memo stamp moved."""
    builds = []
    build = inventory._build_model_options_payload_uncached
    monkeypatch.setattr(inventory, "_build_model_options_payload_uncached",
                        lambda *a, **k: builds.append(a) or build(*a, **k))
    monkeypatch.setattr(inventory, "_memo_stamp", lambda: (("pinned", 0),))

    assert inventory.build_model_options_payload(_ctx())["providers"] == OFFERED  # miss: cached whole
    assert inventory.build_model_options_payload(_ctx())["providers"] == OFFERED  # hit: a narrowed copy
    atomic_config_write(managed_home / "config.yaml", _config(enforced=False))
    assert inventory.build_model_options_payload(_ctx())["providers"] == ROWS  # hit: the entry is still whole
    assert len(builds) == 1


def test_save_key_refreshes_the_row_narrowed_as_the_picker(managed_home, monkeypatch):
    """``model.save_key``'s row replaces the picker's row (#129260), so it offers what model.options does."""
    from tui_gateway import server

    monkeypatch.setattr("hermes_cli.auth.PROVIDER_REGISTRY", {"gemini": types.SimpleNamespace(
        name="Google AI Studio", auth_type="api_key", api_key_env_vars=("TEST_GEMINI_API_KEY",))})
    monkeypatch.setattr("hermes_cli.config.is_managed", lambda: False)
    monkeypatch.setattr("hermes_cli.credential_lifecycle.save_provider_env_credential", Mock())
    monkeypatch.setattr(server, "_model_picker_context", Mock())
    monkeypatch.setattr("hermes_cli.inventory.build_models_payload",
                        lambda *a, **k: {"providers": copy.deepcopy(ROWS)})

    resp = server._methods["model.save_key"]("r1", {"slug": "gemini", "api_key": "replacement-" + "value"})

    assert resp["result"]["provider"] == {**OFFERED[2], "authenticated": True}, resp


@pytest.mark.parametrize("slug", ["custom", ""])
@pytest.mark.parametrize("routes", [(), (("standard", "anthropic", "anthropic-deep"),),
                                    (("deep", "openai-codex", "anthropic-deep"),)])
def test_rederived_provider_rows_drop_exactly_the_ids_the_turn_refuses(monkeypatch, slug, routes):
    """A row whose slug is ``custom`` / empty is judged on the provider the turn re-derives for it."""
    from agent.model_routing_managed import ManagedPinRejectedError
    from hermes_cli.model_options_managed import drop_refused_managed_pins
    from tui_gateway import server

    cfg = _config()
    for level, provider, model in routes:
        cfg["model_routing"]["routes"][level].append(
            {**cfg["model_routing"]["routes"][level][0], "provider": provider, "model": model})
    monkeypatch.setattr(server, "_load_cfg", lambda: cfg)  # what the turn reads as its config
    from hermes_constants import get_hermes_home

    atomic_config_write(get_hermes_home() / "config.yaml", cfg)  # and the runtime resolver's policy
    ids = ["anthropic-deep", "openai-codex-fast", "gemini-4-argon"]
    rows = [{"slug": slug, "models": list(ids), "total_models": len(ids)}]

    drop_refused_managed_pins(rows, cfg)

    def turn_refuses(model):
        try:
            server._resolve_agent_model_runtime({"model": model, "provider": slug}, None, "s")
        except ManagedPinRejectedError:
            return True
        except Exception:  # credentials/network are not the pin verdict
            return False
        return False

    assert rows[0]["models"] == [model for model in ids if not turn_refuses(model)]
    assert rows[0]["total_models"] == len(rows[0]["models"])
    if slug == "custom":  # a bare custom pick never matches a subscription route's provider
        assert rows[0]["models"] == []
