"""``/api/status`` reloads the gateway config only when an input moved. [daveey]

David-MBP profile (t_01a199a0a5d38255): every Desktop status poll ran ``load_gateway_config()`` (legacy JSON, config
YAML + managed overlay, plugin YAML hooks, env overrides, bundled manifest scan) for the connected-platform set. The
set is now kept on the inputs' file signatures (``config.yaml``, ``gateway.json``, ``.env``, the managed layer), the
process environment and the home path, with ``_PLATFORMS_MAX_AGE_S`` bounding what no key sees (a platform's
connected check may read its own state).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import gateway.config as gc
from hermes_cli import web_server_gateway as wsg


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    now = {"mono": 100.0}
    monkeypatch.setattr(wsg.time, "monotonic", lambda: now["mono"])
    wsg._reset_platforms_cache_for_tests()
    yield home, now
    wsg._reset_platforms_cache_for_tests()


@pytest.fixture
def loads(monkeypatch):
    calls = []
    real = gc.load_gateway_config

    def spy():
        calls.append(1)
        return real()

    monkeypatch.setattr(gc, "load_gateway_config", spy)
    return calls


def _write(path, text):
    """Rewrite ``path`` and move its mtime past any racy window (a same-tick rewrite is covered by the digest)."""
    old = path.stat() if path.exists() else None
    path.write_text(text, encoding="utf-8")
    if old is not None:
        bumped = max(old.st_mtime_ns, path.stat().st_mtime_ns) + 5_000_000_000
        os.utime(path, ns=(bumped, bumped))


def test_identical_polls_load_the_config_once(home, loads):
    h, now = home
    _write(h / "config.yaml", "platforms:\n  telegram:\n    enabled: true\n    token: abc123real\n")
    first = wsg._load_configured_gateway_platforms()
    assert "telegram" in first
    for _ in range(5):
        now["mono"] += 3
        assert wsg._load_configured_gateway_platforms() == first
    assert len(loads) == 1


def test_a_config_write_shows_on_the_next_poll(home, loads):
    h, _now = home
    _write(h / "config.yaml", "platforms:\n  telegram:\n    enabled: true\n    token: abc123real\n")
    assert "telegram" in wsg._load_configured_gateway_platforms()
    _write(h / "config.yaml", "platforms:\n  telegram:\n    enabled: false\n    token: abc123real\n")
    assert "telegram" not in wsg._load_configured_gateway_platforms()
    assert len(loads) == 2


def test_a_same_tick_same_size_rewrite_is_not_hidden(home, loads):
    h, _now = home
    cfg = h / "config.yaml"
    cfg.write_text("platforms:\n  telegram:\n    enabled: true \n    token: abc123real\n", encoding="utf-8")
    st = cfg.stat()
    assert "telegram" in wsg._load_configured_gateway_platforms()
    cfg.write_text("platforms:\n  telegram:\n    enabled: false\n    token: abc123real\n", encoding="utf-8")
    os.utime(cfg, ns=(st.st_atime_ns, st.st_mtime_ns))  # same size, same mtime: only the racy digest tells
    assert "telegram" not in wsg._load_configured_gateway_platforms()


def test_an_env_write_or_dotenv_edit_shows_on_the_next_poll(home, loads, monkeypatch):
    h, _now = home
    _write(h / "config.yaml", "platforms:\n  discord:\n    enabled: true\n")
    before = wsg._load_configured_gateway_platforms()
    assert "discord" not in before
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "abc123realtokenvalue")
    assert "discord" in wsg._load_configured_gateway_platforms()
    assert len(loads) == 2
    _write(h / ".env", "SOMETHING=1\n")
    wsg._load_configured_gateway_platforms()
    assert len(loads) == 3


def test_a_home_switch_or_the_max_age_reloads(home, loads, tmp_path, monkeypatch):
    _h, now = home
    wsg._load_configured_gateway_platforms()
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(other))
    wsg._load_configured_gateway_platforms()
    assert len(loads) == 2
    now["mono"] += wsg._PLATFORMS_MAX_AGE_S + 1
    wsg._load_configured_gateway_platforms()
    assert len(loads) == 3


def test_a_legacy_gateway_json_write_shows_on_the_next_poll(home, loads):
    h, _now = home
    gateway = h / "gateway.json"
    for enabled, expected_loads in ((True, 1), (False, 2)):
        _write(gateway, json.dumps({"platforms": {"telegram": {
            "enabled": enabled, "token": "abc123real",
        }}}))
        platforms = wsg._load_configured_gateway_platforms()
        assert ("telegram" in platforms) is enabled
        assert len(loads) == expected_loads
        assert wsg._load_configured_gateway_platforms() == platforms
        assert len(loads) == expected_loads


@pytest.fixture
def request_scopes(home, tmp_path, monkeypatch):
    from agent import secret_scope
    from tui_gateway import launch_profile_policy

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", False)
    monkeypatch.setattr(launch_profile_policy, "_snapshot", None)
    monkeypatch.delenv("HERMES_MANAGED_DIR", raising=False)


@pytest.mark.parametrize("filename", ["config.yaml", ".env"])
def test_a_managed_layer_write_shows_on_the_next_request(
    home, loads, tmp_path, monkeypatch, request_scopes, filename,
):
    from hermes_cli.web_server_profiles import _config_profile_scope, _hermes_home_scope

    h, _now = home
    profile = h / "profiles" / "managed-client"
    profile.mkdir(parents=True)
    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    contents = {
        "config.yaml": (
            "platforms:\n  telegram:\n    enabled: true\n    token: abc123real\n",
            "platforms:\n  telegram:\n    enabled: false\n    token: abc123real\n",
        ),
        ".env": ("TELEGRAM_BOT_TOKEN=abc123real\n", "TELEGRAM_BOT_TOKEN=\n"),
    }
    environ = dict(os.environ)
    for index, content in enumerate(contents[filename], start=1):
        _write(managed / filename, content)
        # A native request binds its home before the config scope builds fresh secrets.
        with _hermes_home_scope(profile), _config_profile_scope(None):
            platforms = wsg._load_configured_gateway_platforms()
            assert ("telegram" in platforms) is (index == 1)
            assert len(loads) == index
            assert wsg._load_configured_gateway_platforms() == platforms
            assert len(loads) == index
        assert dict(os.environ) == environ


def test_request_home_switch_with_identical_environ_and_missing_files(
    home, loads, monkeypatch, request_scopes,
):
    from hermes_cli.web_server_profiles import _config_profile_scope, _hermes_home_scope
    from hermes_constants import get_hermes_home

    h, _now = home
    other = h / "profiles" / "empty"
    other.mkdir(parents=True)
    # Only the launch profile owns this injected token; a secondary scope must not borrow it.
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "abc123real")
    environ = dict(os.environ)
    for index, (directory, connected) in enumerate(((h, True), (other, False), (h, True)), start=1):
        assert all(not (directory / name).exists() for name in ("config.yaml", "gateway.json", ".env"))
        with _hermes_home_scope(directory), _config_profile_scope(None):
            assert get_hermes_home() == directory
            assert dict(os.environ) == environ
            platforms = wsg._load_configured_gateway_platforms()
            assert ("telegram" in platforms) is connected
            assert len(loads) == index
            assert wsg._load_configured_gateway_platforms() == platforms
            assert len(loads) == index
        assert dict(os.environ) == environ


def test_a_failed_load_is_not_cached(home, monkeypatch):
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("bad config")

    monkeypatch.setattr(gc, "load_gateway_config", boom)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            wsg._load_configured_gateway_platforms()
    assert len(calls) == 2
