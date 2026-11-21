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
        assert kwargs == {}
        calls.append(tick[0])
        return [{"ok": False, "scheduled": False}] if mode == "refused" else []

    monkeypatch.setattr(sr, "time", SimpleNamespace(sleep=sleep))
    monkeypatch.setattr(config, "load_config", load)
    monkeypatch.setattr(code_skew, "detect_code_skew", lambda: object() if mode == "skew" and tick[0] < 3 else None)
    monkeypatch.setattr(sr, "_markers_remain", pending)
    monkeypatch.setattr(sr, "resume_interrupted_sessions", resume)
    sr._observe_markers(lambda *a: None, tmp_path, {}, stop)
    assert calls and calls[-1] == 7
    if mode in {"skew", "disabled"}:
        assert min(calls) >= 3
        assert waits[:3] == [5, 30, 30]
    if mode.endswith("error"):
        assert "observer iteration failed" in caplog.text
    if mode == "config_error":
        assert 1 not in calls
    if mode == "refused":
        assert waits == [5, 10, 20, 40, 80, 160, 300, 300]
        assert "remain unscheduled" in caplog.text
    assert 8 not in calls


@pytest.mark.parametrize("failure", ["import", "refused", "deleted"])
def test_observer_recovers_to_fast_cadence(tmp_path, monkeypatch, failure):
    import builtins
    from gateway import code_skew
    from hermes_cli import config

    stop = threading.Event()
    waits = []
    calls = []
    real_import = builtins.__import__

    def importing(name, *args, **kwargs):
        if failure == "import" and name == "gateway.code_skew" and len(waits) == 1:
            raise ImportError("transient import failure")
        return real_import(name, *args, **kwargs)

    def sleep(seconds):
        waits.append(seconds)
        if len(waits) == 4:
            stop.set()

    def resume(*args, **kwargs):
        calls.append(len(waits))
        if len(waits) == 1:
            return [{"scheduled": False, "deleted": failure == "deleted"}]
        return [{"scheduled": True}]

    monkeypatch.setattr(sr, "time", SimpleNamespace(sleep=sleep))
    monkeypatch.setattr(config, "load_config", dict)
    monkeypatch.setattr(code_skew, "detect_code_skew", lambda: None)
    monkeypatch.setattr(sr, "_markers_remain", lambda *a: True)
    monkeypatch.setattr(sr, "resume_interrupted_sessions", resume)
    monkeypatch.setattr(builtins, "__import__", importing)
    sr._observe_markers(lambda *a: None, tmp_path, {}, stop)
    assert waits == [5, {"import": 30, "refused": 10, "deleted": 5}[failure], 5, 5]
    assert calls == ([2, 3] if failure == "import" else [1, 2, 3])


def test_boot_scan_failure_still_starts_observer(tmp_path, monkeypatch):
    from hermes_cli import config, proof_home
    import hermes_constants
    from tui_gateway import server

    stop = threading.Event()
    observed = []
    handler = lambda *a: None
    monkeypatch.setattr(proof_home, "recovery_home_allowed", lambda *a: True)
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(config, "load_config", dict)
    monkeypatch.setattr(server, "_methods", {"session.resume": handler})
    monkeypatch.setattr(sr, "time", SimpleNamespace(sleep=lambda *a: None))

    def fail(*args, **kwargs):
        raise OSError("boot marker scan failed")

    monkeypatch.setattr(sr, "resume_interrupted_sessions", fail)
    monkeypatch.setattr(sr, "_observe_markers", lambda *a: observed.append(a))
    thread = sr.start_startup_resume_sweep(stop=stop)
    assert thread is not None
    thread.join(5)
    assert not thread.is_alive()
    assert observed == [(handler, tmp_path, {}, stop)]
