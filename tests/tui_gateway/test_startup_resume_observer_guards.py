"""Lifetime observation must survive failures without busy retries or stale admission."""
import threading
from types import SimpleNamespace

import pytest

from tui_gateway import startup_resume as sr


@pytest.mark.parametrize("mode", ["skew", "disabled", "scan_error", "config_error", "refused"])
def test_observer_refreshes_guards_and_recovers(tmp_path, monkeypatch, caplog, mode):
    from gateway import code_skew
    from hermes_cli import config

    stop = threading.Event()
    tick = [0]
    calls = []
    waits = []

    def sleep(seconds):
        waits.append(seconds)
        tick[0] += 1
        if tick[0] == 8:
            stop.set()

    def load():
        if mode == "config_error" and tick[0] == 1:
            raise OSError("temporary config failure")
        return {"desktop": {"auto_continue": {"enabled": not (mode == "disabled" and tick[0] < 3)}}}

    def pending(*args):
        if mode == "scan_error" and tick[0] == 1:
            raise OSError("temporary directory failure")
        return True

    def resume(*args, **kwargs):
        assert kwargs == {"paused_only": True}
        calls.append(tick[0])
        return [{"ok": False, "scheduled": False}] if mode == "refused" else []

    monkeypatch.setattr(sr, "time", SimpleNamespace(sleep=sleep))
    monkeypatch.setattr(config, "load_config", load)
    monkeypatch.setattr(code_skew, "detect_code_skew", lambda: object() if mode == "skew" and tick[0] < 3 else None)
    monkeypatch.setattr(sr, "_paused_markers_remain", pending)
    monkeypatch.setattr(sr, "resume_interrupted_sessions", resume)
    sr._observe_pauses(lambda *a: None, tmp_path, {}, stop)
    assert calls and calls[-1] == 7
    if mode in {"skew", "disabled"}:
        assert min(calls) >= 3
    if mode.endswith("error"):
        assert "observer iteration failed" in caplog.text
    if mode == "config_error":
        assert 1 not in calls
    if mode == "refused":
        assert waits == [5, 10, 20, 40, 80, 160, 300, 300]
        assert "remain unscheduled" in caplog.text
    assert 8 not in calls
