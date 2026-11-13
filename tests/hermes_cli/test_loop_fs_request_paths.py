"""Per-request filesystem calls that ran on the gateway event loop (t_01a1d3ac14d0b207).

Under FS/CPU pressure (a ``kanban gc`` rmtree sweep on a loaded Mac) each stat or realpath on the
loop stalled every Desktop request queued behind it for seconds. Each test makes the call slow
after a warm-up and pins that the repeat request path no longer pays for it.
"""
import asyncio
import os
import threading
import time
from pathlib import Path

import pytest

SLOW = 0.2


def _slow(real, calls=None, under=None):
    """``real`` made slow (and its calls' on-main-thread flags recorded); with ``under``, only for
    paths below that dir (config loads stat the test home, which is not what is measured)."""
    def wrapper(*args, **kwargs):
        if under is not None and not str(args[0]).startswith(str(under)):
            return real(*args, **kwargs)
        if calls is not None:
            calls.append(threading.current_thread() is threading.main_thread())
        threading.Event().wait(SLOW)  # health: allow HX013 -- a slow filesystem call IS the behaviour under test
        return real(*args, **kwargs)
    return wrapper


def _plugins_root(cache):
    return Path(cache[0]["_dir"]).parent


@pytest.fixture
def plugin_cache(tmp_path, monkeypatch):
    from hermes_cli import web_server
    dirs = []
    for i in range(5):
        d = tmp_path / "plugins" / f"p{i}"
        d.mkdir(parents=True)
        dirs.append(d)
    cache = [{"name": d.name, "_dir": str(d), "source": "bundled"} for d in dirs]
    monkeypatch.setattr(web_server, "_dashboard_plugins_cache", cache)
    monkeypatch.setattr(web_server, "_discover_dashboard_plugins",
                        lambda: [p for p in cache if Path(p["_dir"]).is_dir()])
    return cache


def test_plugin_gate_does_not_stat_plugin_dirs_per_request(plugin_cache, monkeypatch):
    """Test A: within the stale-check window the gate reads the cached list only."""
    from hermes_cli import web_server
    from hermes_cli.web_server_dashboard import _plugin_api_request_refused
    assert web_server._get_dashboard_plugins() is plugin_cache  # warm: proves the dirs present
    calls = []
    monkeypatch.setattr(Path, "is_dir", _slow(Path.is_dir, calls, _plugins_root(plugin_cache)))

    async def twenty():
        return [await _plugin_api_request_refused("p0") for _ in range(20)]

    started = time.monotonic()
    assert asyncio.run(twenty()) == [False] * 20
    assert time.monotonic() - started < 0.5
    assert calls == []


def test_due_plugin_dir_sweep_runs_off_the_event_loop_and_notices_removal(plugin_cache, monkeypatch):
    from hermes_cli import web_server
    from hermes_cli.web_server_dashboard import _plugin_api_request_refused
    monkeypatch.setattr(web_server, "DASHBOARD_PLUGINS_STALE_CHECK_SECS", 0.0)
    Path(plugin_cache[0]["_dir"]).rmdir()
    calls = []
    monkeypatch.setattr(Path, "is_dir", _slow(Path.is_dir, calls, _plugins_root(plugin_cache)))
    # Gone from disk: rescanned, so it is now unknown (user plugin by default: refused).
    assert asyncio.run(_plugin_api_request_refused("p0")) is True
    assert calls and not any(calls), "the dir sweep must run on a worker thread, not the loop"


def test_stale_check_window_bounds_the_sweep(plugin_cache, monkeypatch):
    from hermes_cli import web_server
    clock = [1000.0]
    monkeypatch.setattr(web_server.time, "monotonic", lambda: clock[0])
    web_server._get_dashboard_plugins()
    Path(plugin_cache[1]["_dir"]).rmdir()
    assert any(p["name"] == "p1" for p in web_server._get_dashboard_plugins())  # within the window
    clock[0] += web_server.DASHBOARD_PLUGINS_STALE_CHECK_SECS
    assert not any(p["name"] == "p1" for p in web_server._get_dashboard_plugins())
    # A list assigned directly is unproven until swept, whatever the clock says.
    fresh = [{"name": "x", "_dir": str(Path(plugin_cache[0]["_dir"]).parent / "gone"), "source": "user"}]
    monkeypatch.setattr(web_server, "_dashboard_plugins_cache", fresh)
    assert web_server._fresh_dashboard_plugins() is None


def test_current_profile_scope_does_not_realpath_per_request(monkeypatch):
    """Test B: ``_config_profile_scope`` for the dashboard's own profile reuses resolved homes."""
    from hermes_cli.web_server_profiles import _config_profile_scope
    with _config_profile_scope(None):
        pass
    # Path.resolve only: the test harness's home-IO guard itself calls os.path.realpath per syscall.
    monkeypatch.setattr(Path, "resolve", _slow(Path.resolve))
    started = time.monotonic()
    with _config_profile_scope(None):
        pass
    assert time.monotonic() - started < 0.05


def test_secret_source_values_reuse_the_resolved_home(tmp_path, monkeypatch):
    from hermes_cli import env_loader
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setitem(env_loader._SECRET_SOURCE_VALUES_BY_HOME, str(home.resolve()), {"K": "v"})
    assert env_loader.get_secret_source_values(home) == {"K": "v"}
    monkeypatch.setattr(Path, "resolve", _slow(Path.resolve))
    started = time.monotonic()
    assert env_loader.get_secret_source_values(home) == {"K": "v"}
    assert time.monotonic() - started < 0.05


def test_missing_home_key_is_not_memoized(tmp_path):
    from hermes_cli import env_loader
    home = tmp_path / "later"
    assert env_loader._home_key(home) == str(home.resolve())
    assert str(home) not in env_loader._HOME_KEYS
    real = tmp_path / "real"
    real.mkdir()
    home.symlink_to(real)
    assert env_loader._home_key(home) == str(real.resolve())
