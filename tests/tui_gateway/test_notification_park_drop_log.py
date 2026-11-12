"""A dropped process completion leaves one INFO line naming the process and its session. [daveey]

A chat that never woke for a re-adopted job's exit could not be root-caused: the park, the unowned-event path
and the registry's parked-event box all dropped completions at DEBUG (or silently), so agent.log held no trace.
Progress notices (``watch_*``, ``heartbeat``) stay DEBUG so a chatty watcher cannot flood the log.
"""

from __future__ import annotations

import logging

import pytest

from tools import process_registry_readopt
from tools.process_registry import ProcessRegistry
from tui_gateway import notification_park
from tui_gateway import session_notifications

KEY = "20261103_024624_52dad5"
COMPLETION = {"type": "completion", "session_id": "proc_697954de64e9", "session_key": KEY, "exit_code": 0,
              "park_if_unowned": True}


@pytest.fixture(autouse=True)
def _empty_park():
    notification_park.clear()
    yield
    notification_park.clear()


def _drop_lines(caplog, level=logging.INFO):
    return [r.getMessage() for r in caplog.records if r.levelno == level and r.getMessage().startswith("Dropped ")]


def test_completion_expiring_from_the_park_logs_info_with_process_and_session(caplog):
    caplog.set_level(logging.DEBUG)
    assert notification_park.park(dict(COMPLETION))
    with notification_park._lock:
        expired = notification_park._expire_locked(float("inf"))
    notification_park._release(expired)
    lines = _drop_lines(caplog)
    assert len(lines) == 1, lines
    assert "proc_697954de64e9" in lines[0] and KEY in lines[0] and "retention window" in lines[0]


def test_progress_notice_drop_stays_debug(caplog):
    caplog.set_level(logging.DEBUG)
    notification_park.log_drop({"type": "watch_match", "session_id": "proc_x", "session_key": KEY}, "test")
    assert _drop_lines(caplog) == []
    assert len(_drop_lines(caplog, logging.DEBUG)) == 1


def test_unowned_completion_the_park_refuses_logs_info(caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(notification_park, "park", lambda evt: False)
    session_notifications._notif_unowned_event("poller-sid", dict(COMPLETION), None)
    session_notifications._notif_unowned_event("poller-sid", dict(COMPLETION), [])  # shutdown drain
    lines = _drop_lines(caplog)
    assert len(lines) == 2, lines
    assert all("proc_697954de64e9" in line and KEY in line for line in lines)


def test_registry_parked_box_overflow_logs_the_evicted_exit(caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(process_registry_readopt, "_MAX_PARKED_EVENTS", 1)
    registry = ProcessRegistry()
    registry.park_unowned_event(dict(COMPLETION))
    registry.park_unowned_event({**COMPLETION, "session_id": "proc_newer"})
    lines = _drop_lines(caplog)
    assert len(lines) == 1, lines
    assert "proc_697954de64e9" in lines[0] and KEY in lines[0]
