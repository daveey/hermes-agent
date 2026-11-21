"""A warm successor must observe pauses published after its boot scan empties."""
import threading
import time
from types import SimpleNamespace

import pytest

from tui_gateway import startup_resume as sr
from tui_gateway.turn_marker import clear_turn_marker, mark_turn_paused, record_turn_start


@pytest.mark.parametrize("late_at", [60, 720])
@pytest.mark.parametrize("disposition", ["pause", "crash", "stopped", "opted_out", "capped", "shutdown"])
def test_boot_observer_recovers_late_pause_after_writer_exit(tmp_path, monkeypatch, late_at, disposition):
    from agent import memory_provider
    from hermes_cli import config
    from tui_gateway import server
    import hermes_constants

    clock = [1000.0]
    alive = [True]
    resumed = []
    stop = threading.Event()
    waits = []
    cfg = {"desktop": {"auto_continue": {"freshness_minutes": 240,
                                         "resume_on_startup_delay_seconds": 0}}}
    record_turn_start(tmp_path, "chat", "original request")
    mark_turn_paused(tmp_path, "chat", "core_update_pause")
    monkeypatch.setattr(config, "load_config", lambda: cfg)
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(sr, "marker_writer_state", lambda m: "alive" if alive[0] else "dead")

    def sleep(seconds):
        if not seconds:
            return
        waits.append(seconds)
        clock[0] += seconds
        elapsed = clock[0] - 1000
        if elapsed < late_at:
            # Old backend continues its first pause, clearing the pause reason.
            record_turn_start(tmp_path, "chat", "original request", pauses=1)
        elif elapsed < late_at + 30:
            record_turn_start(tmp_path, "chat", "original request", pauses=1,
                              auto_continue=disposition != "opted_out",
                              attempts=2 if disposition == "capped" else 0)
            if disposition != "crash":
                mark_turn_paused(tmp_path, "chat", "core_update_pause")
            if disposition == "stopped":
                clear_turn_marker(tmp_path, "chat")
        else:
            alive[0] = False
            if disposition == "shutdown":
                stop.set()  # shutdown while asleep must not start new work
        if elapsed >= late_at + 90:
            stop.set()

    def resume(rid, params):
        assert not alive[0]
        resumed.append(params["session_id"])
        clear_turn_marker(tmp_path, params["session_id"])
        return {"result": {"auto_continue": {"attempt": 0}}}

    monkeypatch.setattr(sr, "time", SimpleNamespace(sleep=sleep, time=time.time,
                                                   monotonic=lambda: clock[0]))
    monkeypatch.setitem(server._methods, "session.resume", resume)

    class Inline:
        def __init__(self, target):
            self.target = target

        def start(self):
            self.target()

    def spawn(target, **kwargs):
        assert kwargs["passive"] is True, "idle recovery must not retain an active-work reservation"
        return Inline(target)

    monkeypatch.setattr(memory_provider, "spawn_context_thread", spawn)
    sr.start_startup_resume_sweep(stop=stop)
    assert waits[:2] == [5.0, 5.0], "live writers remain eligible for observation, not adoption"
    assert resumed == (["chat"] if disposition in {"pause", "crash"} else [])
