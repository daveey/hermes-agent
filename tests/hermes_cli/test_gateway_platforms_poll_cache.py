"""``/api/status`` reloads the gateway config only when an input moved. [daveey]

David-MBP profile (t_01a199a0a5d38255): every Desktop status poll ran ``load_gateway_config()`` (legacy JSON, config
YAML + managed overlay, plugin YAML hooks, env overrides, bundled manifest scan) for the connected-platform set. The
set is now kept on the inputs' file signatures (``config.yaml``, ``gateway.json``, ``.env``, the managed layer), the
process environment and the home path, with ``_PLATFORMS_MAX_AGE_S`` bounding what no key sees (a platform's
connected check may read its own state).
"""

from __future__ import annotations

import os

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
