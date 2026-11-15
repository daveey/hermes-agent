"""model.options warm-cost cuts: no work whose result the picker discards or deep-copies for nothing.

Behavior is pinned against reference implementations of the OLD code, so the cuts stay byte-identical.
"""
import copy

import pytest

from hermes_cli import auth
from hermes_cli.auth import AuthError


def _old_routable(provider_id):
    """The pre-cut ``is_runtime_provider_routable`` body."""
    normalized = (provider_id or "").strip().lower()
    if not normalized:
        return False
    if normalized in {"auto", "openrouter", "custom", "moa"} or normalized.startswith("custom:"):
        return True
    try:
        auth.resolve_provider(normalized)
    except AuthError:
        return False
    return True


_IDS = [
    "", "  ", None, "auto", "openrouter", "custom", "moa", "custom:foo", "OpenRouter", " anthropic ",
    "anthropic", "claude", "deepseek", "no-such-provider", "opencode-free", "gemini", "google",
    "nous", "zai", "glm", "codex", "openai-codex", "bedrock", "vertex", "x" * 40,
]


@pytest.mark.parametrize("pid", _IDS)
def test_routable_matches_old_behavior(pid):
    assert auth.is_runtime_provider_routable(pid) is _old_routable(pid)


def test_unknown_provider_probe_builds_no_error_hint(monkeypatch):
    """A routability probe discards the error text, so it must not validate the whole config for it."""
    from hermes_cli import auth_error_copy

    def refuse(*args, **kwargs):
        raise AssertionError("routability probe built an unknown-provider hint")

    monkeypatch.setattr(auth_error_copy, "unknown_provider_hint", refuse)
    assert auth.is_runtime_provider_routable("no-such-provider-xyz") is False
    # The resolver itself still raises the same hinted error for real callers.
    monkeypatch.undo()
    with pytest.raises(AuthError) as exc:
        auth.resolve_provider("no-such-provider-xyz")
    assert exc.value.code == "invalid_provider"
    assert "Unknown provider 'no-such-provider-xyz'." in str(exc.value)


def test_config_selects_provider_does_not_deepcopy_config(monkeypatch):
    from hermes_cli import config

    calls = []
    real = config.load_config

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(config, "load_config", counting)
    auth._config_selects_provider("anthropic")
    assert calls == [], "pure-read config probe must use load_config_readonly"


def test_config_selects_provider_never_mutates_cached_config(home_config):
    from hermes_cli.config import load_config_readonly
    before = copy.deepcopy(load_config_readonly())
    for pid in ("openrouter", "anthropic", "deepseek", "nope"):
        auth._config_selects_provider(pid)
    assert load_config_readonly() == before


def test_catalog_config_values_unchanged_and_isolated(home_config):
    from hermes_cli import model_catalog
    from hermes_cli.config import load_config_readonly
    cfg = model_catalog._load_catalog_config()
    raw = load_config_readonly().get("model_catalog") or {}
    assert cfg["enabled"] is bool(raw.get("enabled", True))
    cfg["providers"]["__probe__"] = 1  # a caller mutating its copy must not reach the shared cache
    assert "__probe__" not in (load_config_readonly().get("model_catalog") or {}).get("providers", {})


@pytest.fixture
def home_config():
    from hermes_constants import get_hermes_home
    (get_hermes_home() / "config.yaml").write_text(
        "model:\n  default: x\n  provider: openrouter\nmodel_catalog:\n  providers:\n    acme: {url: u}\n")
    return get_hermes_home()


def test_bound_home_layer_follows_home_changes(tmp_path, monkeypatch):
    """The memoized home resolution must re-resolve whenever HERMES_HOME (or an override) changes."""
    import providers

    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(a))
    layer_a, _home_a, key_a = providers._bound_home_layer()
    assert providers._bound_home_layer()[0] is layer_a
    monkeypatch.setenv("HERMES_HOME", str(b))
    layer_b, home_b, key_b = providers._bound_home_layer()
    assert layer_b is not layer_a and home_b == b and key_b != key_a
    monkeypatch.setenv("HERMES_HOME", str(a))
    assert providers._bound_home_layer()[0] is layer_a
