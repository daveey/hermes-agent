"""The dashboard's browser auto-open must not hand a person's browser the agent markers. [daveey]

main() advertises HERMES_AGENT/AI_AGENT in os.environ for every command; a browser that
``hermes dashboard`` launched in-process would inherit them at start time, and the dashboard
comment endpoint (kanban_http_comment_writer) would then record a person's drawer notes typed
there as an agent's.
"""
from __future__ import annotations

import os
import stat
import sys
import time

import pytest

from hermes_cli import top_level_env

# Captured at import, before the autouse browser guard (tests/conftest.py) replaces them.
REAL_OPEN = top_level_env.open_url_for_person
REAL_CHECKED = top_level_env.open_url_for_person_checked


def test_unchecked_opener_spawns_the_clean_child(monkeypatch):
    spawned = []
    monkeypatch.setattr(top_level_env, "_spawn_clean_url_opener", lambda url, new=0: spawned.append(url))
    REAL_OPEN("http://127.0.0.1:9119")
    assert spawned == ["http://127.0.0.1:9119"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX BROWSER script")
def test_opened_browser_starts_without_agent_markers(tmp_path, monkeypatch):
    dump = tmp_path / "browser-env.txt"
    script = tmp_path / "fake-browser"
    script.write_text(f"#!/bin/sh\nenv > '{dump}.tmp'\necho \"URL=$1\" >> '{dump}.tmp'\nmv '{dump}.tmp' '{dump}'\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("BROWSER", f"{script} %s")
    monkeypatch.setenv("HERMES_AGENT", "true")
    monkeypatch.setenv("AI_AGENT", "hermes-agent")
    monkeypatch.setenv("HERMES_SESSION_ID", "agent-session")

    proc = top_level_env._spawn_clean_url_opener("http://127.0.0.1:9119/?profile=default")
    assert proc.wait(30) == 0
    deadline = time.monotonic() + 30
    while not dump.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    lines = dump.read_text().splitlines()
    env = dict(line.split("=", 1) for line in lines if "=" in line and not line.startswith("URL="))
    assert "URL=http://127.0.0.1:9119/?profile=default" in lines
    assert not {"HERMES_AGENT", "AI_AGENT", "HERMES_SESSION_ID"} & env.keys()
    assert env.get("PATH") == os.environ["PATH"]  # an ordinary environment otherwise


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="BROWSER as the only registered browser")
@pytest.mark.parametrize("status, expected", [(0, True), (3, False)])
def test_checked_opener_reports_a_real_browser_outcome(tmp_path, monkeypatch, status, expected):
    script = tmp_path / "fake-browser"
    script.write_text(f"#!/bin/sh\nexit {status}\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    for name in ("DISPLAY", "WAYLAND_DISPLAY"):
        monkeypatch.delenv(name, raising=False)  # no xdg-open/graphical fallbacks registered
    monkeypatch.setenv("BROWSER", f"{script} %s")
    monkeypatch.setenv("PATH", str(tmp_path))  # no console browsers (w3m, lynx) to fall back to
    assert REAL_CHECKED("http://127.0.0.1:9119/") is expected


class _Proc:
    def __init__(self, result):
        self._result = result

    def wait(self, timeout):
        if isinstance(self._result, BaseException):
            raise self._result
        return self._result


@pytest.mark.parametrize("spawn, expected", [
    (lambda url, new=0: _Proc(0), True),
    (lambda url, new=0: _Proc(1), False),
    (lambda url, new=0: _Proc(__import__("subprocess").TimeoutExpired("opener", 15)), True),
    (lambda url, new=0: (_ for _ in ()).throw(OSError("no interpreter")), False),
])
def test_checked_opener_reports_the_childs_outcome(monkeypatch, spawn, expected):
    monkeypatch.setattr(top_level_env, "_spawn_clean_url_opener", spawn)
    assert REAL_CHECKED("http://127.0.0.1:9119") is expected


def test_webapp_open_uses_clean_opener(monkeypatch, _neutralize_webbrowser):
    from hermes_cli import auth_device_flow, webapp_launcher
    import webbrowser

    monkeypatch.setattr(auth_device_flow, "_can_open_graphical_browser", lambda: True)
    monkeypatch.setattr(webbrowser, "open", lambda url: pytest.fail("in-process browser open"))
    assert webapp_launcher._open("http://127.0.0.1:9120/") is True
    assert _neutralize_webbrowser == ["http://127.0.0.1:9120/"]


def test_dashboard_auto_open_uses_clean_opener(monkeypatch, _neutralize_webbrowser):
    """The autouse guard records ``open_url_for_person``; the in-process opener must not run."""
    from hermes_cli import web_server_lifecycle

    class _ImmediateThread:
        def __init__(self, *, target, daemon):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr(web_server_lifecycle.threading, "Thread", _ImmediateThread)
    monkeypatch.setattr(web_server_lifecycle.time, "sleep", lambda _s: None)
    monkeypatch.setenv("DISPLAY", ":0")
    import webbrowser
    monkeypatch.setattr(webbrowser, "open", lambda url: pytest.fail("in-process browser open"))

    web_server_lifecycle._maybe_open_browser("127.0.0.1", 9119, True, "")

    assert _neutralize_webbrowser == ["http://127.0.0.1:9119"]
