"""The restart drain-timeout reader reads the cached raw config without copying it. [daveey]

Every Desktop ``/api/status`` poll resolves the restart drain timeout (``_fts_rebuild_status`` ->
``web_server_lifecycle._resolve_restart_drain_timeout``); ``read_raw_config()`` deep-copied the whole user config for
one key (~2.6 ms per poll on David-MBP, t_01a19d0bcab45558). It only reads, so it uses the read-only view.
"""

from __future__ import annotations

import os

import pytest

import hermes_cli.config as cfg
from hermes_cli import gateway as gw


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_RESTART_DRAIN_TIMEOUT", raising=False)
    monkeypatch.delenv("HERMES_CRON_DRAIN_TIMEOUT", raising=False)
    return home


@pytest.fixture
def copies(monkeypatch):
    calls = []
    real = cfg.copy.deepcopy

    def spy(obj, *a, **k):
        calls.append(1)
        return real(obj, *a, **k)

    monkeypatch.setattr(cfg.copy, "deepcopy", spy)
    return calls


def _write(path, text):
    old = path.stat() if path.exists() else None
    path.write_text(text, encoding="utf-8")
    if old is not None:
        bumped = max(old.st_mtime_ns, path.stat().st_mtime_ns) + 5_000_000_000
        os.utime(path, ns=(bumped, bumped))


def test_cached_reads_do_not_copy_the_config(home, copies):
    _write(home / "config.yaml", "agent:\n  restart_drain_timeout: 42\n")
    assert gw._get_restart_drain_timeout() == 42
    copies.clear()
    for _ in range(3):
        assert gw._get_restart_drain_timeout() == 42
    assert copies == []


def test_a_config_edit_shows_on_the_next_read(home, copies):
    _write(home / "config.yaml", "agent:\n  restart_drain_timeout: 42\n")
    assert gw._get_restart_drain_timeout() == 42
    _write(home / "config.yaml", "agent:\n  restart_drain_timeout: 7\n")
    assert gw._get_restart_drain_timeout() == 7
