"""Heartbeat notifications for long background processes.

A ``heartbeat`` on a background process emits a periodic "still running + output since the last
heartbeat" event on the completion queue so the agent stays current on a long bounded job (merge
train, full suite, deploy) without polling. Invariants: each heartbeat carries only NEW output,
heartbeats stop at exit, and the normal completion notice still fires.
"""
import json
import os
import queue
import shlex
import time

import pytest

import tools.process_registry as pr
from tests.waiting import wait_until
from tools.process_registry import ProcessRegistry


def _drain(q: queue.Queue) -> list:
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            return out


def _wait_until(pred, timeout: float, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return pred()


@pytest.mark.platforms("linux")
def test_heartbeat_carries_only_new_output_and_stops_at_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "HEARTBEAT_MIN_SECONDS", 1)
    monkeypatch.setattr(pr, "HEARTBEAT_TICK_SECONDS", 0.1)
    # The child advances only when released through a fifo, never on a wall-clock sleep: a quiet
    # tick queues nothing, so two lines make two beats only if the first beat is seen before the
    # second line is written. fd 3 is read-write so the non-blocking release always has a reader.
    hold = tmp_path / "hold"
    os.mkfifo(hold)
    registry = ProcessRegistry()
    session = registry.spawn_local(
        f"exec 3<>{shlex.quote(str(hold))}; echo first; read -r _ <&3; echo second; read -r _ <&3",
        cwd=str(tmp_path),
    )
    session.notify_on_complete = True

    def beats_now() -> list:
        return [e for e in list(registry.completion_queue.queue) if e.get("type") == "heartbeat"]

    try:
        assert registry.arm_heartbeat(session, 1) == 1
        wait_until(lambda: len(beats_now()) >= 1, timeout=_HEARTBEAT_WAIT, what="first heartbeat")
        assert registry.poll(session.id)["status"] == "running"
        _release_hold(hold)
        wait_until(lambda: len(beats_now()) >= 2, timeout=_HEARTBEAT_WAIT, what="second heartbeat")
        assert registry.poll(session.id)["status"] == "running"
        _release_hold(hold)
        wait_until(lambda: registry.poll(session.id)["status"] != "running",
                   timeout=_HEARTBEAT_WAIT, what="child exit")
        # The completion event is enqueued shortly after the reader observes EOF.
        wait_until(lambda: any(e.get("type") == "completion" for e in list(registry.completion_queue.queue)),
                   timeout=_HEARTBEAT_WAIT, what="completion event")
    finally:
        _release_hold(hold)
        registry.kill_process(session.id)
    events = _drain(registry.completion_queue)
    beats = [e for e in events if e["type"] == "heartbeat"]
    completion = [e for e in events if e["type"] == "completion"]

    assert len(beats) == 2, events
    assert [b["seq"] for b in beats] == [1, 2]
    assert all(b["session_id"] == session.id and b["interval"] == 1 for b in beats)
    # Output is a delta: every produced line appears in exactly one heartbeat, never twice.
    assert "first" in beats[0]["output"] and "second" not in beats[0]["output"], beats
    assert "second" in beats[1]["output"] and "first" not in beats[1]["output"], beats
    assert len(completion) == 1
    # Heartbeats never outlive the process: nothing after the completion notice.
    assert events.index(completion[0]) > events.index(beats[-1])
    assert not _wait_until(lambda: any(e.get("type") == "heartbeat" for e in list(registry.completion_queue.queue)),
                           timeout=2.5)


def test_a_tick_with_no_new_output_queues_nothing():
    """Every queued heartbeat costs the owning session a full model turn, so a quiet tick is
    skipped rather than delivered as "(no new output)"; the next tick with output still carries
    exactly the delta and the sequence counts delivered beats only."""
    registry = ProcessRegistry()
    session = pr.ProcessSession(id="proc_quiet", command="sleep 600", notify_on_complete=True)
    session._heartbeat_last = 0.0

    registry._emit_heartbeat(session, now=100.0)
    registry._emit_heartbeat(session, now=200.0)
    assert _drain(registry.completion_queue) == []
    assert session._heartbeat_last == 200.0 and session._heartbeat_seq == 0

    session.output_buffer += "first line\n"
    session.total_output_chars += len("first line\n")
    registry._emit_heartbeat(session, now=300.0)
    (beat,) = _drain(registry.completion_queue)
    assert beat["type"] == "heartbeat" and beat["seq"] == 1 and beat["output"] == "first line\n"

    registry._emit_heartbeat(session, now=400.0)
    assert _drain(registry.completion_queue) == []


def test_schema_minimum_heartbeat_is_disabled_for_foreground(monkeypatch):
    from tools import terminal_tool as tt

    captured = {}

    def fake_terminal_tool(**kwargs):
        captured.update(kwargs)
        return json.dumps({"output": "Background process started", "session_id": "proc_x", "exit_code": 0})

    monkeypatch.setattr(tt, "terminal_tool", fake_terminal_tool)
    heartbeat_schema = tt.TERMINAL_SCHEMA["parameters"]["properties"]["heartbeat"]
    generated = {
        "command": "pwd",
        "background": False,
        "timeout": 20,
        "pty": False,
        "notify": False,
        "heartbeat": heartbeat_schema["minimum"],
    }

    result = json.loads(tt._handle_terminal(generated))

    assert not result.get("error")
    assert captured["background"] is False
    assert captured["heartbeat"] == 0
    assert captured["notify_on_complete"] is False


def _release_hold(path) -> None:
    """Unblock a child reading ``path``. A missing reader is fine: the caller still kills."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        return
    try:
        os.write(fd, b"\n")
    finally:
        os.close(fd)


# Deadline, not a delay: a descheduled runner can sit well past the 1s heartbeat
# interval and still be fine. Only a session the tick loop will never see waits this out.
_HEARTBEAT_WAIT = 60.0


@pytest.mark.platforms("linux")
def test_first_heartbeat_carries_output_produced_before_arming(tmp_path, monkeypatch):
    """Pre-arm output belongs in the first heartbeat, not in nobody.

    The terminal tool arms the heartbeat after spawn bookkeeping, and a fast child has
    already written by then. The child stays blocked on a fifo until that heartbeat is
    observed: ``sleep 3`` is a wall-clock lifetime, so a descheduled runner publishes
    "first" only after the child has exited. ``arm_heartbeat`` then stamps a session the
    tick loop no longer scans (``_running`` only, and not ``exited``), and the wait is
    for an event that will never arrive.
    """
    monkeypatch.setattr(pr, "HEARTBEAT_MIN_SECONDS", 1)
    monkeypatch.setattr(pr, "HEARTBEAT_TICK_SECONDS", 0.1)
    hold = tmp_path / "hold"
    os.mkfifo(hold)
    registry = ProcessRegistry()
    session = registry.spawn_local(
        f"echo first; read -r _ < {shlex.quote(str(hold))}",
        cwd=str(tmp_path),
    )
    session.notify_on_complete = True
    try:
        wait_until(
            lambda: "first" in registry.poll(session.id).get("output_preview", ""),
            timeout=_HEARTBEAT_WAIT,
            what="child output 'first' published",
        )
        status = registry.poll(session.id).get("status")
        assert status == "running", (
            f"child exited before arm ({status}); a fixed sleep cannot cover a descheduled reader"
        )
        registry.arm_heartbeat(session, 1)
        wait_until(
            lambda: any(
                e.get("type") == "heartbeat" for e in list(registry.completion_queue.queue)
            ),
            timeout=_HEARTBEAT_WAIT,
            what="first heartbeat while the child is still held open",
        )
        first_beat = next(
            e for e in list(registry.completion_queue.queue) if e.get("type") == "heartbeat"
        )
        assert "first" in first_beat["output"], first_beat
    finally:
        _release_hold(hold)
        registry.kill_process(session.id)


def test_terminal_dispatch_heartbeat_implies_notify_and_foreground_runs_without_it(monkeypatch):
    """A foreground call carrying an explicit positive heartbeat (the shape models copy from a
    background call) runs once with the heartbeat dropped: refusing it only produced
    identical-call retry loops. Notification intent (`notify=true`) stays refused."""
    from tools import terminal_tool as tt

    captured = {}

    def fake_terminal_tool(**kwargs):
        captured.update(kwargs)
        return json.dumps({"output": "Background process started", "session_id": "proc_x", "exit_code": 0})

    monkeypatch.setattr(tt, "terminal_tool", fake_terminal_tool)
    dispatch = tt._handle_terminal
    fg = json.loads(dispatch({"command": "sleep 1", "background": False, "notify": False, "heartbeat": 120}))
    assert not fg.get("error")
    assert captured["background"] is False and captured["heartbeat"] == 0
    assert captured["notify_on_complete"] is False

    captured.clear()
    fg_notify = json.loads(dispatch({"command": "sleep 1", "notify": True}))
    assert fg_notify.get("error") and "background" in fg_notify["error"] and not captured

    bg = json.loads(dispatch({"command": "sleep 1", "background": True, "heartbeat": 120}))
    assert "error" not in bg or not bg["error"]
    assert captured["heartbeat"] == 120 and captured["notify_on_complete"] is True
